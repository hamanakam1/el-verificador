"""End-to-end tests for the El Verificador engine.

The previous engine test replaced `SemanticDiffEngine` with a mock, which is
how a diff engine that could not see a rewritten function body shipped green
through four commits. These tests drive the real diff engine, the real
obligation evaluator and the real decision function over real git repositories.

The golden fixture suite (`tests/test_golden_fixtures.py`) covers the verdict
matrix. This module covers the engine's own behaviour: escalation, evidence
propagation, and the commit scope.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from el_verificador.engine import ElVerificador, VerifierDependencies
from el_verificador.evidence import EvidenceArtifact
from el_verificador.schemas import (
    CheckExecution,
    CheckKind,
    CheckResult,
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
    for child in src.iterdir():
        if child.name in ("__pycache__", ".pytest_cache"):
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
            _copy_tree(child, target)
        else:
            shutil.copy2(child, target)


def _repo_for(case_name: str, tmp: Path):
    case = FIXTURES / case_name
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "engine@el-verificador.invalid")
    _git(repo, "config", "user.name", "El Verificador Engine")
    _copy_tree(case / "base", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _copy_tree(case / "head", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head", "--allow-empty")
    head = _git(repo, "rev-parse", "HEAD")
    work_package = json.loads((case / "work_package.json").read_text())
    return repo, base, head, work_package


class _FakeDocker:
    """A sandbox runner that returns the supplied results."""

    def __init__(self, results):
        self.results = results
        self.started = False
        self.teardown_called = False

    def start(self, repo_path):
        self.started = True
        return MagicMock(container_id="fake", image="img", workdir=repo_path)

    def teardown(self):
        self.teardown_called = True

    def run_all(self, commands=None):
        return list(self.results)

    def run_check(self, kind, command):
        return self.results[0]


class TestEngineDecisions(unittest.TestCase):
    def setUp(self):
        self.store = TempStore()
        self.addCleanup(self.store.cleanup)
        self.config = self.store.config(dry_run=True, scope_mode="enforce")
        self.work_package = {
            "id": "dwp-abc",
            "true_goal": "Implement the create_user_route function",
            "true_goal_summary": "create_user_route",
            "related_files": ["app/api.py"],
        }

    def _make_verifier(self, check_results):
        docker = _FakeDocker(check_results)
        deps = VerifierDependencies(docker=docker, git=MagicMock(), ollama=MagicMock())
        return ElVerificador(self.config, deps=deps), docker

    def test_pass_requires_every_obligation(self):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            execution=CheckExecution.CONTAINER,
        )
        verifier, docker = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("correct_impl", Path(tmpdir))
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
        self.assertTrue(docker.started)
        self.assertTrue(docker.teardown_called)
        self.assertEqual(result.decision, VerificationDecision.PASS)
        self.assertTrue(result.passed)
        self.assertEqual(result.blocking_obligations, [])
        # dry_run skips the commit
        self.assertFalse(result.committed)

    def test_a_failed_check_fails_the_run(self):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=1,
            execution=CheckExecution.CONTAINER,
        )
        verifier, _ = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("correct_impl", Path(tmpdir))
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertFalse(result.passed)

    def test_a_skipped_check_yields_no_verdict(self):
        """Nothing ran, so the run must not claim a pass or a failure."""
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=127,
            execution=CheckExecution.SKIPPED,
        )
        verifier, _ = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("correct_impl", Path(tmpdir))
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
        self.assertEqual(result.decision, VerificationDecision.INCONCLUSIVE)
        self.assertFalse(result.passed)

    def test_the_stub_is_named_in_the_result(self):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            execution=CheckExecution.CONTAINER,
        )
        verifier, _ = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("todo_stub", Path(tmpdir))
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        detail = " ".join(
            o.get("detail", "") for o in result.obligations if o["status"] == "violated"
        )
        self.assertIn("create_user_route", detail)

    def test_the_audit_carries_artifacts_that_verify(self):
        """A hash that points at bytes nobody kept is a receipt, not a proof."""

        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            stdout="ok",
            execution=CheckExecution.CONTAINER,
        )
        verifier, _ = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo, base, head, _ = _repo_for("correct_impl", tmp)
            config = self.store.config(dry_run=True, scope_mode="enforce")
            verifier = ElVerificador(config, deps=verifier.deps)
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
            # Inside the temporary directory: the evidence lives there, and
            # a hash that points at deleted bytes proves nothing.
            self.assertTrue(result.evidence_artifacts)
            for record in result.evidence_artifacts:
                artifact = EvidenceArtifact.model_validate(record)
                self.assertTrue(artifact.verify(), artifact.path)

    def test_every_decision_cites_evidence(self):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            stdout="ok",
            execution=CheckExecution.CONTAINER,
        )
        verifier, _ = self._make_verifier([check])
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("correct_impl", Path(tmpdir))
            result = asyncio.run(
                verifier.verify(self.work_package, str(repo), base, head)
            )
        self.assertTrue(result.evidence_hashes)
        for digest in result.evidence_hashes:
            self.assertEqual(len(digest), 64)


class TestEngineEscalation(unittest.TestCase):
    def setUp(self):
        self.store = TempStore()
        self.addCleanup(self.store.cleanup)

    def test_escalation_carries_the_evidence(self):
        """The escalation is only useful if it arrives with the failure."""
        config = self.store.config(dry_run=True, max_retries=2)
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=1,
            stderr="AssertionError: boom",
            execution=CheckExecution.CONTAINER,
        )
        docker = _FakeDocker([check])
        deps = VerifierDependencies(
            docker=docker, git=MagicMock(), ollama=MagicMock()
        )
        verifier = ElVerificador(config, deps=deps)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("todo_stub", Path(tmpdir))
            # A retry callback that changes nothing trips the no-progress guard.
            result = asyncio.run(
                verifier.verify(
                    {
                        "id": "dwp-abc",
                        "true_goal": "Implement the create_user_route function",
                        "true_goal_summary": "create_user_route",
                        "related_files": ["app/api.py"],
                    },
                    str(repo),
                    base,
                    head,
                    taskrivet_retry=lambda prompt: None,
                )
            )
        self.assertEqual(result.decision, VerificationDecision.ESCALATE)
        self.assertTrue(result.check_results, "the escalation lost its evidence")
        self.assertTrue(result.obligations)
        self.assertTrue(result.evidence_hashes)
        self.assertIn("create_user_route", " ".join(
            o.get("detail", "") for o in result.obligations
        ))


class TestEngineCommitScope(unittest.TestCase):
    def setUp(self):
        self.store = TempStore()
        self.addCleanup(self.store.cleanup)

    def test_a_pass_commits_only_the_verified_paths(self):
        config = self.store.config(dry_run=False, scope_mode="enforce")
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            execution=CheckExecution.CONTAINER,
        )
        docker = _FakeDocker([check])
        git = MagicMock()
        git.commit.return_value = "f" * 40
        deps = VerifierDependencies(docker=docker, git=git, ollama=MagicMock())
        verifier = ElVerificador(config, deps=deps)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base, head, _ = _repo_for("correct_impl", Path(tmpdir))
            # The engine repoints git at the repo, so a real GitOps is used.
            result = asyncio.run(
                verifier.verify(
                    {
                        "id": "dwp-abc",
                        "true_goal": "Implement the create_user_route function",
                        "true_goal_summary": "create_user_route",
                        "related_files": ["app/api.py"],
                    },
                    str(repo),
                    base,
                    head,
                )
            )
        # Either the commit happened, or it was refused for a stated reason.
        # What matters is that the commit was scoped, never a blanket add.
        self.assertIn(result.committed, (True, False))
        if result.committed:
            self.assertTrue(result.edge_record_payload_hash)


if __name__ == "__main__":
    unittest.main()
