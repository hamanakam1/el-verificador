"""
Golden fixture suite — the regression harness for the whole verifier.

Each fixture is a small synthetic repository with a *known correct verdict*.
Together they encode the question the component exists to answer: did the
agent actually build what was asked, or did it make the build go green?

The fixtures are the tests that were missing. `semantic_diff.py` — the module
that answers that question — had no test file at all, and the engine test
replaced it with a mock, which is how a diff engine that could not see a
rewritten function body shipped green through four commits.

Every fixture is built into a real git repository and verified end to end, so
the diff engine, the obligation evaluator and the decision function are all
exercised against real git output.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.acceptance import resolve_acceptance
from el_verificador.engine import ElVerificador
from el_verificador.schemas import (
    IntentPackage,
    VerificationConfig,
    VerificationDecision,
)
from tests.support import TempStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "golden"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a fixture tree, skipping build artefacts."""
    for child in src.iterdir():
        if child.name in ("__pycache__", ".pytest_cache"):
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
            _copy_tree(child, target)
        else:
            shutil.copy2(child, target)


def _build_repo(tmp: Path, case: Path) -> tuple:
    """Materialise a fixture as a real git repository with two commits."""
    repo = tmp / "repo"
    repo.mkdir()

    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "fixture@el-verificador.invalid")
    _git(repo, "config", "user.name", "El Verificador Fixture")

    _copy_tree(case / "base", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _copy_tree(case / "head", repo)
    _git(repo, "add", "-A")
    # `--allow-empty` so a fixture whose head equals its base is still a
    # valid two-commit range.
    _git(repo, "commit", "-qm", "head", "--allow-empty")
    head = _git(repo, "rev-parse", "HEAD")

    return repo, base, head


def _check_fixture(
    config: VerificationConfig, case: Path, tmp: Path, work_package: dict
) -> str:
    """Recorded acceptance results for this fixture.

    The commands recorded are the ones the verifier will actually run, which
    for a work package that carries its own acceptance contract are not the
    configured defaults.
    """
    expected = json.loads((case / "expected.json").read_text())
    overrides = expected.get("checks", {})

    intent = IntentPackage.from_dict(work_package)
    contract = resolve_acceptance(intent, config)

    checks = []
    for command in contract.commands:
        record = overrides.get(command, {})
        checks.append(
            {
                "command": command,
                "exit_code": record.get("exit_code", 0),
                "stdout": record.get("stdout", ""),
                "stderr": record.get("stderr", ""),
            }
        )
    path = tmp / "checks.json"
    path.write_text(json.dumps({"checks": checks}, indent=2))
    return str(path)


class TestGoldenFixtures(unittest.TestCase):
    """Every fixture must reach its known verdict, for the right reason."""

    def _run_case(self, name: str):
        case = FIXTURES / name
        expected = json.loads((case / "expected.json").read_text())
        work_package = json.loads((case / "work_package.json").read_text())

        store = TempStore()
        self.addCleanup(store.cleanup)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo, base, head = _build_repo(tmp, case)

            config = store.config(
                dry_run=True,
                scope_mode=expected.get("scope_mode", "enforce"),
            )
            config.check_fixture = _check_fixture(
                config, case, tmp, work_package
            )

            import asyncio

            verifier = ElVerificador(config)
            result = asyncio.run(
                verifier.verify(work_package, str(repo), base, head)
            )
        return result, expected

    def _cases(self):
        for case in sorted(FIXTURES.iterdir()):
            if case.is_dir():
                yield case.name

    def test_fixture_suite_is_present(self):
        names = list(self._cases())
        self.assertGreaterEqual(len(names), 8, "the golden suite lost fixtures")

    def test_each_fixture_reaches_its_verdict(self):
        for name in self._cases():
            with self.subTest(fixture=name):
                result, expected = self._run_case(name)

                self.assertEqual(
                    result.decision.value,
                    expected["decision"],
                    f"{name}: expected {expected['decision']}, "
                    f"got {result.decision.value}; "
                    f"obligations={result.obligations}",
                )

                violated = {
                    o["id"] for o in result.obligations if o["status"] == "violated"
                }
                for expected_kind in expected.get("violations", []):
                    self.assertIn(
                        expected_kind,
                        violated,
                        f"{name}: expected {expected_kind} to be violated; "
                        f"violated={sorted(violated)}",
                    )

    def test_passing_fixture_reports_no_blocking_obligations(self):
        result, _ = self._run_case("correct_impl")
        self.assertEqual(result.decision, VerificationDecision.PASS)
        self.assertEqual(result.blocking_obligations, [])
        self.assertTrue(result.evidence_hashes, "a pass must cite evidence")

    def test_stub_fixture_names_the_stub(self):
        result, _ = self._run_case("todo_stub")
        detail = " ".join(
            o.get("detail", "") for o in result.obligations if o["status"] == "violated"
        )
        self.assertIn("create_user_route", detail)

    def test_failing_fixture_still_cites_evidence(self):
        result, _ = self._run_case("broken_build")
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertTrue(result.evidence_hashes, "a failure must cite evidence too")

    def test_the_work_packages_own_acceptance_contract_is_run(self):
        """A work package's command is run, not the configured default."""
        result, _ = self._run_case("contract_honoured")
        commands = [
            o["statement"]
            for o in result.obligations
            if o["id"].startswith("acceptance_command")
        ]
        self.assertTrue(commands)
        self.assertTrue(
            any("npm test" in statement for statement in commands),
            commands,
        )
        self.assertFalse(
            any("pytest -q" in statement for statement in commands),
            "the configured default ran instead of the work package's contract",
        )

    def test_empty_diff_is_not_a_pass(self):
        result, _ = self._run_case("empty_diff")
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        ids = {o["id"] for o in result.blocking_obligations}
        self.assertIn("change_present", ids)


if __name__ == "__main__":
    unittest.main()
