"""
Guardrailed feedback loop for El Verificador.

When Taskrivet fails acceptance checks or violates an intent obligation, El
Verificador does not blindly say "try again". It packages the exact stack
trace, semantic diff, failed test output and the *list of violated obligations*
into a bounded Correction Prompt and hands it back to Taskrivet with a hard
iteration limit (max_retries=3). On exhaustion the loop is broken and the
ticket is flagged for human intervention.

Three properties this loop guarantees:

* **The prompt is derived from evidence.** The obligations that blocked the
  pass are listed explicitly. The locally-generated narrative is additive; the
  prompt stands on its own without it.
* **A no-op retry is not a retry.** If the working tree has not changed since
  the last attempt, another attempt cannot produce a different result, so the
  loop escalates instead of spending the remaining budget.
* **An inconclusive run is not retried.** When the checks never executed there
  is nothing for the agent to fix, so the loop stops and reports.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .ollama_client import OllamaClient
from .schemas import (
    CheckResult,
    CorrectionPrompt,
    IntentPackage,
    SemanticDiff,
    VerificationAttempt,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)

CORRECTION_COMPOSER_SYSTEM = """You are El Verificador's correction-prompt analyst.

You are given the verified evidence for a failed attempt: the obligations that
were not satisfied, the failed acceptance checks with their exit codes and the
tail of their output, and the semantic diff against the original intent.

Write a short, precise analysis for the agent that has to fix this. State what
broke, in what order it matters, and what a correct fix looks like.

Rules:

- Do not invent evidence that is not given to you.
- Do not suggest writing TODO stubs, and do not suggest weakening, skipping or
  deleting tests.
- Do not suggest changing the verification environment: the configuration, the
  check fixture, the acceptance commands, the thresholds, or the verifier
  itself. If a check did not run, say so and tell the agent to fix the code;
  the gate is not the agent's to adjust.
- Every change must map back to the task goal.

Return only the analysis text: no markdown fences, no preamble, no sign-off."""

CORRECTION_COMPOSER_USER_TEMPLATE = """Task goal: {task_goal}
Task summary: {task_goal_summary}

Attempt {attempt} of {max_retries}. Remaining retries: {remaining}.

Obligations not satisfied:
{obligations}

Failed checks:
{failed_checks}

Stack traces:
{stack_traces}

Failed test output:
{test_output}

Semantic diff:
{semantic_diff}

Write the analysis for the agent that must fix this."""


class RetryExhausted(Exception):
    """Raised when max_retries is reached without a passing verification."""


class CorrectionLoop:
    """
    Guardrailed retry loop between El Verificador and Taskrivet.

    - On failure: compose a Correction Prompt (via local Qwen), hand to Taskrivet
    - Retries are bounded by max_retries; on exhaustion, escalate.
    - A retry that changed nothing is treated as exhaustion.
    """

    def __init__(
        self,
        config: VerificationConfig,
        ollama: OllamaClient,
        run_verification: Callable[[int], VerificationResult],
        worktree_fingerprint: Optional[Callable[[], str]] = None,
    ):
        self.config = config
        self.ollama = ollama
        self._run_verification = run_verification
        self._fingerprint = worktree_fingerprint
        self.attempts: List[VerificationAttempt] = []
        self.model_calls = 0
        self._last_fingerprint: Optional[str] = None

    async def run_until_pass_or_exhausted(
        self,
        intent: IntentPackage,
        base_commit: str,
        head_commit: str,
        taskrivet_retry: Callable[[CorrectionPrompt], None],
    ) -> VerificationResult:
        """
        Execute the retry loop.

        taskrivet_retry receives the Correction Prompt and is expected to
        have Taskrivet re-execute its work inside the sandbox before the
        next verification attempt.
        """
        budget_started = time.time()
        for attempt in range(1, self.config.max_retries + 1):
            start = time.time()
            result = self._run_verification(attempt)
            duration_ms = int((time.time() - start) * 1000)

            attempt_record = VerificationAttempt(
                attempt=attempt,
                decision=result.decision,
                check_results=getattr(result, "check_results", []) or [],
                semantic_diff=getattr(result, "semantic_diff", None),
                obligations=getattr(result, "obligations", []) or [],
                evidence_hashes=getattr(result, "evidence_hashes", []) or [],
                duration_ms=duration_ms,
            )

            if result.decision == VerificationDecision.PASS:
                self.attempts.append(attempt_record)
                result.attempts = self.attempts
                return result

            if result.decision == VerificationDecision.INCONCLUSIVE:
                # Nothing ran, so there is nothing the agent can fix.
                self.attempts.append(attempt_record)
                result.attempts = self.attempts
                return result

            if attempt >= self.config.max_retries:
                # Record the final attempt before escalating: the escalation
                # is only useful if it arrives with the evidence that caused
                # it. No prompt is composed here, because none is delivered.
                self.attempts.append(attempt_record)
                raise RetryExhausted(
                    f"Verification exhausted after {self.config.max_retries} attempts; "
                    f"escalating to human"
                )

            # The wall-clock budget is a second bound on the loop, beside the
            # attempt count. A cheap ticket that burns ten minutes on three
            # attempts has spent more than it was worth.
            elapsed_ms = int((time.time() - budget_started) * 1000)
            if elapsed_ms >= self.config.max_retry_duration_ms:
                self.attempts.append(attempt_record)
                raise RetryExhausted(
                    f"the retry budget of {self.config.max_retry_duration_ms}ms was "
                    f"spent after {attempt} attempt(s); escalating to human"
                )

            correction_prompt = await self._compose_correction_prompt(
                attempt, intent, result
            )
            attempt_record.correction_prompt = correction_prompt
            # This attempt failed and another one is coming: say so, so the
            # attempt log reads as a sequence rather than three identical
            # failures.
            attempt_record.decision = VerificationDecision.RETRY
            self.attempts.append(attempt_record)

            # No-progress guard: the agent was asked to fix something. If the
            # working tree did not change, another attempt is a no-op.
            before = self._current_fingerprint()
            taskrivet_retry(correction_prompt)
            after = self._current_fingerprint()
            if before is not None and before == after:
                raise RetryExhausted(
                    "the working tree did not change after the correction; "
                    "another attempt cannot produce a different result"
                )

        raise RetryExhausted("Retry loop exited without a pass or escalation")

    # ------------------------------------------------------------------ #
    async def _compose_correction_prompt(
        self,
        attempt: int,
        intent: IntentPackage,
        result: VerificationResult,
    ) -> CorrectionPrompt:
        failed_checks = getattr(result, "check_results", []) or []
        semantic_diff = getattr(result, "semantic_diff", None)
        violations = [
            o
            for o in (getattr(result, "obligations", []) or [])
            if o.get("status") in ("violated", "unproven")
        ]
        stack_traces: List[str] = []
        test_output = ""

        for check in failed_checks:
            if check.passed:
                # A check that passed is not a failure and does not belong in
                # the prompt's failure section.
                continue
            if check.stderr and "Traceback (most recent call last)" in check.stderr:
                stack_traces.append(check.stderr)
            test_output += check.failure_summary + "\n"

        # The prompt names what failed, not what happened.
        failed_checks = [check for check in failed_checks if not check.passed]

        narrative: Optional[str] = None
        try:
            user = CORRECTION_COMPOSER_USER_TEMPLATE.format(
                task_goal=intent.true_goal,
                task_goal_summary=intent.true_goal_summary,
                attempt=attempt,
                max_retries=self.config.max_retries,
                remaining=self.config.max_retries - attempt,
                obligations=self._format_violations(violations),
                failed_checks=self._format_checks(failed_checks),
                stack_traces="\n".join(stack_traces) or "(none)",
                test_output=test_output or "(none)",
                semantic_diff=self._format_diff(semantic_diff) or "(none)",
            )
            narrative = await self.ollama.generate(
                prompt=user,
                system=CORRECTION_COMPOSER_SYSTEM,
                temperature=0.1,
                max_tokens=2048,
            )
            self.model_calls += 1
        except Exception:
            # A missing local model must not stop the correction loop; the
            # deterministic evidence below is the part that matters.
            narrative = None

        return CorrectionPrompt(
            attempt=attempt,
            max_retries=self.config.max_retries,
            remaining_attempts=self.config.max_retries - attempt,
            task_goal=intent.true_goal,
            task_goal_summary=intent.true_goal_summary,
            failed_checks=failed_checks,
            stack_traces=stack_traces,
            test_output=test_output,
            semantic_diff=semantic_diff,
            violations=violations,
            narrative=(narrative or "").strip() or None,
        )

    def _current_fingerprint(self) -> Optional[str]:
        if self._fingerprint is None:
            return None
        try:
            return self._fingerprint()
        except Exception:
            return None

    @staticmethod
    def _format_violations(violations: List[Dict[str, Any]]) -> str:
        if not violations:
            return "(none)"
        lines = []
        for violation in violations:
            lines.append(
                f"- {violation.get('kind', 'obligation')}: "
                f"{violation.get('statement', '')}"
            )
            detail = violation.get("detail", "")
            if detail:
                lines.append(f"  {detail}")
        return "\n".join(lines)

    @staticmethod
    def _format_checks(checks: List[CheckResult]) -> str:
        if not checks:
            return "(none)"
        lines = []
        for check in checks:
            state = "passed" if check.passed else (
                "not executed" if not check.executed else "failed"
            )
            lines.append(
                f"- {check.kind.value}: `{check.command}` exit={check.exit_code} "
                f"({state})"
            )
            if check.failure_summary:
                lines.append(f"  {check.failure_summary[:400]}")
        return "\n".join(lines)

    @staticmethod
    def _format_diff(diff: Optional[SemanticDiff]) -> str:
        if not diff:
            return "(none)"
        return (
            f"files={', '.join(diff.files_changed) or '(none)'} "
            f"added={diff.added_lines} removed={diff.removed_lines} "
            f"intent_coverage={diff.intent_coverage:.2f} "
            f"has_stubs={diff.has_todo_stubs} "
            f"weakened_tests={', '.join(diff.weakened_tests) or '(none)'}"
        )


__all__ = ["CorrectionLoop", "RetryExhausted"]
