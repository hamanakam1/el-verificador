"""Tests for the guardrailed correction/retry loop."""
import asyncio
import unittest
from unittest.mock import MagicMock

from el_verificador.correction import CorrectionLoop, RetryExhausted
from el_verificador.ollama_client import OllamaClient
from el_verificador.schemas import (
    CheckKind,
    CheckResult,
    CorrectionPrompt,
    IntentPackage,
    SemanticDiff,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)


class _FakeOllama(OllamaClient):
    async def generate(self, prompt="", system="", temperature=None, max_tokens=4096):
        return "Fix the failing tests and re-run."


class TestCorrectionLoop(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(max_retries=3, dry_run=True)
        self.ollama = _FakeOllama()
        self.intent = IntentPackage.from_dict({
            "id": "dwp-abc",
            "true_goal": "Implement login route",
            "true_goal_summary": "login route",
            "related_files": ["app/auth.py"],
        })

    def _passing_result(self, attempt):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=0
        )
        return VerificationResult(
            work_package_id=self.intent.id,
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.PASS,
            check_results=[check],
            passed=True,
        )

    def _failing_result(self, attempt):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=1,
            stderr="AssertionError: expected 200, got 500",
        )
        diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/auth.py"],
            added_lines=5,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.5,
            has_todo_stubs=True,
            stub_symbols=["login"],
        )
        return VerificationResult(
            work_package_id=self.intent.id,
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.FAIL,
            check_results=[check],
            semantic_diff=diff,
            passed=False,
        )

    def test_pass_first_attempt(self):
        calls = {"n": 0}

        def run_once(attempt):
            calls["n"] += 1
            return self._passing_result(attempt)

        loop = CorrectionLoop(self.config, self.ollama, run_once)

        def retry(prompt):  # noqa: ARG001
            pass

        result = asyncio.run(
            loop.run_until_pass_or_exhausted(
                self.intent, "a" * 40, "b" * 40, retry
            )
        )
        self.assertEqual(result.decision, VerificationDecision.PASS)
        self.assertEqual(calls["n"], 1)

    def test_retry_then_pass(self):
        sequence = [self._failing_result(1), self._passing_result(2)]
        calls = {"n": 0}

        def run_once(attempt):
            calls["n"] += 1
            return sequence[calls["n"] - 1]

        loop = CorrectionLoop(self.config, self.ollama, run_once)
        retries = []

        def retry(prompt):
            self.assertIsInstance(prompt, CorrectionPrompt)
            self.assertEqual(prompt.attempt, 1)
            retries.append(prompt)

        result = asyncio.run(
            loop.run_until_pass_or_exhausted(
                self.intent, "a" * 40, "b" * 40, retry
            )
        )
        self.assertEqual(result.decision, VerificationDecision.PASS)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(retries), 1)

    def test_exhaustion_escalates(self):
        calls = {"n": 0}

        def run_once(attempt):
            calls["n"] += 1
            return self._failing_result(attempt)

        loop = CorrectionLoop(self.config, self.ollama, run_once)
        retries = []

        def retry(prompt):
            retries.append(prompt)

        with self.assertRaises(RetryExhausted):
            asyncio.run(
                loop.run_until_pass_or_exhausted(
                    self.intent, "a" * 40, "b" * 40, retry
                )
            )
        # Exactly max_retries attempts, then escalation
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(retries), 2)  # retries happen on attempts 1 and 2

    def test_correction_prompt_fields(self):
        prompt = CorrectionPrompt(
            attempt=2,
            max_retries=3,
            remaining_attempts=1,
            task_goal="Implement login route",
            task_goal_summary="login route",
            failed_checks=[
                CheckResult(
                    kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=1
                )
            ],
            stack_traces=["Traceback ..."],
            test_output="FAILED",
            semantic_diff=None,
        )
        self.assertEqual(prompt.attempt, 2)
        self.assertEqual(prompt.remaining_attempts, 1)
        rendered = prompt.render()
        self.assertIn("Attempt 2 of 3", rendered)
        self.assertIn("Remaining retries: 1", rendered)


if __name__ == "__main__":
    unittest.main()
