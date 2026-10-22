"""Tests for the no-progress check in the correction loop.

The loop used to re-run the same checks on the same tree until the retry
budget ran out. If the correction prompt was not acted on, every one of
those runs failed the same way, and the budget was spent to learn nothing.
"""
import asyncio
import unittest

from el_verificador.correction import CorrectionLoop, NoProgress, RetryExhausted
from el_verificador.ollama_client import OllamaClient
from el_verificador.schemas import (
    CheckKind,
    CheckResult,
    IntentPackage,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)


class _FakeOllama(OllamaClient):
    async def generate(self, prompt="", system="", temperature=None, max_tokens=4096):
        return "Fix the failing tests and re-run."


class TestNoProgress(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(max_retries=3, dry_run=True)
        self.ollama = _FakeOllama()
        self.intent = IntentPackage.from_dict({
            "id": "dwp-abc",
            "true_goal": "Implement login route",
            "true_goal_summary": "login route",
            "related_files": ["app/auth.py"],
        })
        self.runs = 0

    def _failing_result(self, attempt):
        check = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=1,
            stdout="1 failed",
        )
        return VerificationResult(
            work_package_id=self.intent.id,
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.FAIL,
            check_results=[check],
            passed=False,
        )

    def _loop(self, probe):
        def run(attempt):
            self.runs += 1
            return self._failing_result(attempt)

        return CorrectionLoop(
            self.config, self.ollama, run, progress_probe=probe
        )

    def _run(self, loop):
        return asyncio.run(
            loop.run_until_pass_or_exhausted(
                self.intent, "a" * 40, "b" * 40, lambda prompt: None
            )
        )

    def test_an_unchanged_repository_stops_the_loop_early(self):
        loop = self._loop(lambda: "same")
        with self.assertRaises(NoProgress) as caught:
            self._run(loop)
        self.assertIn("did not change", str(caught.exception))
        # One run happened; the second was refused before it started.
        self.assertEqual(self.runs, 1)

    def test_a_changed_repository_uses_the_whole_budget(self):
        counter = {"n": 0}

        def probe():
            counter["n"] += 1
            return f"state-{counter['n']}"

        loop = self._loop(probe)
        with self.assertRaises(RetryExhausted):
            self._run(loop)
        self.assertEqual(self.runs, self.config.max_retries)

    def test_without_a_probe_the_check_is_disabled(self):
        loop = self._loop(None)
        with self.assertRaises(RetryExhausted):
            self._run(loop)
        self.assertEqual(self.runs, self.config.max_retries)

    def test_a_probe_that_raises_does_not_stop_the_loop(self):
        def probe():
            raise RuntimeError("git is unavailable")

        loop = self._loop(probe)
        with self.assertRaises(RetryExhausted):
            self._run(loop)
        self.assertEqual(self.runs, self.config.max_retries)


if __name__ == "__main__":
    unittest.main()
