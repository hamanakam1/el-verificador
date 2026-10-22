"""End-to-end test of the verification cycle.

The unit tests cover each mechanism. This one runs the whole thing — a real
git repository, the real sandbox runner, the real commit path, and the real
audit ledger — and asserts the properties that matter:

- a passing verification commits only the files it reviewed, and leaves the
  operator's untracked files alone;
- the audit trail verifies, and stops verifying when a record is edited;
- a failing verification escalates with the evidence attached;
- a repository that tries to write to the host does not, unless the operator
  allowed it.
"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from el_verificador.docker_runner import HOST_EXECUTION_REFUSED, DockerRunner
from el_verificador.engine import ElVerificador, VerifierDependencies
from el_verificador.git_ops import GitOps
from el_verificador.schemas import (
    EdgeRecordLogger,
    VerificationConfig,
    VerificationDecision,
)

BASE = "searchservice/routes.py"
IMPLEMENTED = '''"""Search routes."""


def create_search_route(query: str, results: list[str]) -> dict:
    """Build the search response payload for a query."""
    trimmed = query.strip()
    if not trimmed:
        return {"query": "", "results": [], "message": "Type a search term to begin."}
    if not results:
        return {"query": trimmed, "results": [], "message": "No matches. Try a broader term."}
    return {"query": trimmed, "results": list(results), "message": ""}
'''

STUB = '''"""Search routes."""


def create_search_route(query: str, results: list[str]) -> dict:
    """Build the search response payload for a query."""
    # TODO: implement route
    pass
'''

WORK_PACKAGE = {
    "id": "dwp-e2e-0001",
    "true_goal": (
        "Implement create_search_route in searchservice/routes.py so a query "
        "returns results and an empty result set returns useful guidance."
    ),
    "true_goal_summary": "Implement create_search_route with an empty-state message.",
    "related_files": ["searchservice/routes.py"],
    "acceptance_commands": ["python3 -m pytest -q"],
}


class _EndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.ledger = self.work / "ledger.sqlite"
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "e2e@example.invalid")
        self._git("config", "user.name", "e2e")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "searchservice").mkdir()
        (self.repo / "searchservice" / "__init__.py").write_text("")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_routes.py").write_text(
            "from searchservice.routes import create_search_route\n"
            "\n"
            "\n"
            "def test_returns_results():\n"
            "    payload = create_search_route('x', ['a'])\n"
            "    assert payload['results'] == ['a']\n"
        )
        (self.repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\npythonpath = ['.']\ntestpaths = ['tests']\n"
        )
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "scaffold")
        # The stub lands in its own commit, so the diff has a real
        # before-and-after pair to compare.
        (self.repo / "searchservice" / "routes.py").write_text(STUB)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "add the route as a stub")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        )

    def _implement(self) -> None:
        (self.repo / "searchservice" / "routes.py").write_text(IMPLEMENTED)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "implement the route")

    def _verifier(self, allow_host: bool = True):
        config = VerificationConfig(
            dry_run=False,
            allow_host_execution=allow_host,
            max_retries=1,
            edgerecord_db=str(self.ledger),
        )
        deps = VerifierDependencies(
            docker=DockerRunner(config),
            git=GitOps(str(self.repo)),
            ollama=None,
            audit_logger=EdgeRecordLogger(str(self.ledger)),
        )
        return ElVerificador(config, deps=deps)

    def _verify(self, allow_host: bool = True):
        verifier = self._verifier(allow_host)
        # The sandbox is forced unavailable so the test behaves the same on a
        # machine with Docker and one without. The host path is what the
        # opt-in governs, and that is the path under test.
        with patch.object(DockerRunner, "_docker_available", return_value=False):
            return asyncio.run(
                verifier.verify(WORK_PACKAGE, str(self.repo), "HEAD~1", "HEAD")
            )


class TestEndToEnd(_EndToEnd):
    def test_a_correct_implementation_passes_and_leaves_the_operator_files_alone(self):
        self._implement()
        # The operator's own untracked work, which the verifier does not own.
        (self.repo / "operator-notes.md").write_text("private notes\n")
        (self.repo / ".env").write_text("SECRET=hunter2\n")

        result = self._verify()

        self.assertEqual(result.decision, VerificationDecision.PASS, result.check_results)
        self.assertTrue(result.passed)
        # Nothing the verifier did not review was committed.
        committed = self._git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertNotIn(".env", committed)
        self.assertNotIn("operator-notes.md", committed)
        # The operator's files are still theirs.
        status = self._git("status", "--porcelain").stdout
        self.assertIn(".env", status)
        self.assertIn("operator-notes.md", status)

    def test_the_audit_trail_verifies_and_detects_an_edited_record(self):
        self._implement()
        result = self._verify()
        self.assertEqual(result.decision, VerificationDecision.PASS)

        logger = EdgeRecordLogger(str(self.ledger))
        ok, reason = logger.chain_status()
        self.assertTrue(ok, reason)
        logger.close()

        # Edit a stored record: the chain must notice.
        import sqlite3

        conn = sqlite3.connect(self.ledger)
        try:
            conn.execute(
                "UPDATE verification_audits SET rationale = ?", ("something else",)
            )
            conn.commit()
        finally:
            conn.close()

        logger = EdgeRecordLogger(str(self.ledger))
        ok, reason = logger.chain_status()
        self.assertFalse(ok)
        self.assertIn("no longer hashes", reason)
        logger.close()

    def test_a_stub_fails_with_the_stub_named(self):
        result = self._verify()
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertTrue(result.semantic_diff.has_todo_stubs)
        self.assertIn("create_search_route", result.semantic_diff.stub_symbols)

    def test_the_run_records_the_contract_it_used(self):
        self._implement()
        result = self._verify()
        self.assertEqual(result.acceptance_contract_source, "work-package")
        self.assertEqual(result.acceptance_commands, WORK_PACKAGE["acceptance_commands"])

    def test_a_repository_that_writes_to_the_host_is_not_run(self):
        """A conftest.py is arbitrary code, and it stays on its own side."""
        self._implement()
        marker = self.work / "pwned.txt"
        (self.repo / "conftest.py").write_text(
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('executed on the host')\n"
        )
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "add a conftest")

        result = self._verify(allow_host=False)

        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertFalse(marker.exists())
        for check in result.check_results:
            self.assertEqual(check.exit_code, HOST_EXECUTION_REFUSED)
            self.assertIn("allow-host-execution", check.stderr)

    def test_the_audit_record_names_the_evidence(self):
        self._implement()
        self._verify()
        logger = EdgeRecordLogger(str(self.ledger))
        chain = logger.get_audit_chain(WORK_PACKAGE["id"])
        self.assertTrue(chain)
        evidence = json.loads(chain[-1]["key_evidence"])
        self.assertTrue(any(e.startswith("acceptance_contract=") for e in evidence))
        self.assertTrue(any(e.startswith("coverage_basis=") for e in evidence))
        logger.close()


if __name__ == "__main__":
    unittest.main()
