"""End-to-end tests for the El Verificador engine (dry-run mode)."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from el_verificador.engine import ElVerificador, VerifierDependencies
from el_verificador.schemas import (
    CheckKind,
    CheckResult,
    IntentPackage,
    SemanticDiff,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)


class _FakeDocker:
    """Fake sandbox runner that returns results supplied by the test."""

    def __init__(self, results):
        self.results = results
        self.started = False
        self.teardown_called = False

    def start(self, repo_path):
        self.started = True
        return MagicMock(container_id="fake", image="img", workdir=repo_path)

    def teardown(self):
        self.teardown_called = True

    def run_all(self):
        return list(self.results)

    def run_check(self, kind, command):
        return self.results[0]


class TestEngineDryRun(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(dry_run=True)
        self.work_package = {
            "id": "dwp-abc",
            "true_goal": "Implement the login API route",
            "true_goal_summary": "login route",
            "related_files": ["app/auth.py"],
        }
        self.pass_check = CheckResult(
            kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=0
        )
        self.aligned_diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/auth.py"],
            added_lines=10,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.95,
            has_todo_stubs=False,
            stub_symbols=[],
        )

    def _make_verifier(self, check_results, aligned=True):
        docker = _FakeDocker(check_results)
        deps = VerifierDependencies(docker=docker, git=MagicMock(), ollama=MagicMock())
        verifier = ElVerificador(self.config, deps=deps)
        return verifier, docker

    def _run_verify(self, verifier, work_package, repo, base, head):
        return asyncio.run(
            verifier.verify(work_package, repo, base, head)
        )

    def test_pass_single_attempt(self):
        verifier, docker = self._make_verifier([self.pass_check])
        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = self.aligned_diff
            result = self._run_verify(
                verifier, self.work_package, "/tmp/repo", "a" * 40, "b" * 40
            )
        self.assertTrue(docker.started)
        self.assertTrue(docker.teardown_called)
        self.assertEqual(result.decision, VerificationDecision.PASS)
        self.assertTrue(result.passed)
        # dry_run skips actual commits
        self.assertFalse(result.committed)

    def test_fail_when_checks_fail(self):
        fail_check = CheckResult(
            kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=1
        )
        verifier, docker = self._make_verifier([fail_check])
        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = self.aligned_diff
            result = self._run_verify(
                verifier, self.work_package, "/tmp/repo", "a" * 40, "b" * 40
            )
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertFalse(result.passed)

    def test_fail_when_diff_not_aligned(self):
        bad_diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=[],
            added_lines=0,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.0,
            has_todo_stubs=True,
            stub_symbols=["login"],
        )
        verifier, docker = self._make_verifier([self.pass_check])
        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = bad_diff
            result = self._run_verify(
                verifier, self.work_package, "/tmp/repo", "a" * 40, "b" * 40
            )
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertFalse(result.passed)

    def test_fail_when_the_goal_names_identifiers_the_change_does_not_provide(self):
        """A change that provides none of the named things is not an attempt.

        The acceptance commands can all exit zero while the agent edits
        something else entirely. The names the goal asked for are the one
        signal that catches it.
        """
        unrelated = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/other.py"],
            added_lines=3,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.0,
            has_todo_stubs=False,
            stub_symbols=[],
            goal_tokens=["login", "route"],
            covered_tokens=[],
            uncovered_tokens=["login", "route"],
            named_identifiers=["login_route"],
        )
        verifier, docker = self._make_verifier([self.pass_check])
        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = unrelated
            result = self._run_verify(
                verifier, self.work_package, "/tmp/repo", "a" * 40, "b" * 40
            )
        self.assertEqual(result.decision, VerificationDecision.FAIL)
        self.assertFalse(result.passed)

    def test_fail_on_todo_stubs_can_be_switched_off(self):
        """The config option is honoured rather than ignored."""
        config = VerificationConfig(dry_run=True, fail_on_todo_stubs=False)
        stubbed = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/auth.py"],
            added_lines=3,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.5,
            has_todo_stubs=True,
            stub_symbols=["login"],
        )
        docker = _FakeDocker([self.pass_check])
        deps = VerifierDependencies(docker=docker, git=MagicMock(), ollama=MagicMock())
        verifier = ElVerificador(config, deps=deps)
        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = stubbed
            result = self._run_verify(
                verifier, self.work_package, "/tmp/repo", "a" * 40, "b" * 40
            )
        self.assertEqual(result.decision, VerificationDecision.PASS)

    def test_escalation_carries_the_evidence_it_exists_to_deliver(self):
        """An exhausted retry budget must not arrive with the evidence removed.

        The escalation exists so a human can pick the work up. If the check
        results and the semantic diff are dropped on the way out, the human
        gets a count and nothing else.
        """
        fail_check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=1,
            stdout="1 failed",
            stderr="TypeError: not subscriptable",
        )
        stubbed = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/auth.py"],
            added_lines=3,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.2,
            has_todo_stubs=True,
            stub_symbols=["login"],
        )
        docker = _FakeDocker([fail_check])
        deps = VerifierDependencies(docker=docker, git=MagicMock(), ollama=MagicMock())
        verifier = ElVerificador(self.config, deps=deps)
        prompts = []

        def retry(prompt):
            prompts.append(prompt)

        with patch(
            "el_verificador.engine.SemanticDiffEngine"
        ) as mock_engine:
            instance = mock_engine.return_value
            instance.compute.return_value = stubbed
            result = asyncio.run(
                verifier.verify(
                    self.work_package, "/tmp/repo", "a" * 40, "b" * 40,
                    taskrivet_retry=retry,
                )
            )
        self.assertEqual(result.decision, VerificationDecision.ESCALATE)
        # The budget is spent, and the correction prompts were handed over.
        self.assertEqual(len(prompts), self.config.max_retries - 1)
        # The evidence survives.
        self.assertTrue(result.check_results)
        self.assertFalse(result.check_results[0].passed)
        self.assertIn("TypeError", result.check_results[0].stderr)
        self.assertIsNotNone(result.semantic_diff)
        self.assertTrue(result.semantic_diff.has_todo_stubs)
        self.assertIn("login", result.semantic_diff.stub_symbols)


if __name__ == "__main__":
    unittest.main()
