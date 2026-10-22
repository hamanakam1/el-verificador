"""
Guardrailed feedback loop for El Verificador.

When Taskrivet fails acceptance checks, El Verificador does not blindly say
"try again". It packages the exact stack trace, semantic diff, and failed
test output into a bounded Correction Prompt and hands it back to Taskrivet
with a hard iteration limit (max_retries=3). On exhaustion the loop is
broken and the ticket is flagged for human intervention.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

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

CORRECTION_COMPOSER_SYSTEM = """You are El Verificador's correction-prompt composer.

Your job: write a precise, bounded Correction Prompt that Taskrivet will use
to fix its own failed output. Include only the evidence Taskrivet needs:
the failed checks (exit codes, command, tail of stderr/stdout), stack traces,
the semantic diff vs. the original intent, and the remaining attempt budget.

Tone: strict, factual, no fluff. Tell Taskrivet exactly what broke and what
to fix. Do not suggest writing TODO stubs. Every change must map back to the
task goal."""

CORRECTION_COMPOSER_USER_TEMPLATE = """Task goal: {task_goal}
Task summary: {task_goal_summary}

Attempt {attempt} of {max_retries}. Remaining retries: {remaining}.

Failed checks:
{failed_checks}

Stack traces:
{stack_traces}

Failed test output:
{test_output}

Semantic diff:
{semantic_diff}

Write a Correction Prompt for Taskrivet that tells it exactly what to fix.
Return ONLY the correction prompt text, no markdown fences, no preamble."""


class RetryExhausted(Exception):
    """Raised when max_retries is reached without a passing verification."""


class NoProgress(RetryExhausted):
    """Raised when the repository did not change between two attempts.

    The correction prompt was handed back and nothing was done about it.
    Re-running the same checks on the same tree produces the same failure,
    so the loop stops early and says why. Burning the rest of the retry
    budget on an unchanged repository buys nothing.
    """


class CorrectionLoop:
    """
    Guardrailed retry loop between El Verificador and Taskrivet.

    - On failure: compose a Correction Prompt (via local Qwen), hand to Taskrivet
    - Retries are bounded by max_retries; on exhaustion, escalate.
    - The loop stops early if the repository has not changed since the last
      attempt, because another identical run would fail identically.
    """

    def __init__(
        self,
        config: VerificationConfig,
        ollama: OllamaClient,
        run_verification: Callable[[int], VerificationResult],
        progress_probe: Optional[Callable[[], str]] = None,
    ):
        self.config = config
        self.ollama = ollama
        self._run_verification = run_verification
        self._progress_probe = progress_probe
        self.attempts: List[VerificationAttempt] = []

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
        last_signature: Optional[str] = None
        for attempt in range(1, self.config.max_retries + 1):
            signature = self._progress_signature()
            if attempt > 1 and signature is not None and signature == last_signature:
                raise NoProgress(
                    "the repository did not change between attempt "
                    f"{attempt - 1} and attempt {attempt}; the correction "
                    "prompt was not acted on, so another identical run would "
                    "fail identically"
                )
            last_signature = signature

            start = time.time()
            result = self._run_verification(attempt)
            duration_ms = int((time.time() - start) * 1000)

            attempt_record = VerificationAttempt(
                attempt=attempt,
                decision=result.decision,
                check_results=getattr(result, "check_results", []) or [],
                semantic_diff=getattr(result, "semantic_diff", None),
                duration_ms=duration_ms,
            )

            if result.decision == VerificationDecision.PASS:
                self.attempts.append(attempt_record)
                result.attempts = self.attempts
                return result

            correction_prompt = await self._compose_correction_prompt(
                attempt, intent, result
            )
            attempt_record.correction_prompt = correction_prompt
            self.attempts.append(attempt_record)

            if attempt >= self.config.max_retries:
                final = VerificationResult(
                    work_package_id=intent.id,
                    repo_path=getattr(result, "repo_path", ""),
                    base_commit=base_commit,
                    head_commit=head_commit,
                    decision=VerificationDecision.ESCALATE,
                    attempts=self.attempts,
                    passed=False,
                    committed=False,
                )
                raise RetryExhausted(
                    f"Verification exhausted after {self.config.max_retries} attempts; escalating to human"
                )

            taskrivet_retry(correction_prompt)

        raise RetryExhausted("Retry loop exited without a pass or escalation")

    async def _compose_correction_prompt(
        self,
        attempt: int,
        intent: IntentPackage,
        result: VerificationResult,
    ) -> CorrectionPrompt:
        failed_checks = getattr(result, "check_results", []) or []
        semantic_diff = getattr(result, "semantic_diff", None)
        stack_traces: List[str] = []
        test_output = ""

        for check in failed_checks:
            if not check.passed:
                if check.stderr:
                    stack_traces.append(check.stderr)
                test_output += check.failure_summary + "\n"

        rendered: Optional[str] = None
        try:
            user = CORRECTION_COMPOSER_USER_TEMPLATE.format(
                task_goal=intent.true_goal,
                task_goal_summary=intent.true_goal_summary,
                attempt=attempt,
                max_retries=self.config.max_retries,
                remaining=self.config.max_retries - attempt,
                failed_checks=self._format_checks(failed_checks),
                stack_traces="\n".join(stack_traces) or "(none)",
                test_output=test_output or "(none)",
                semantic_diff=self._format_diff(semantic_diff) or "(none)",
            )
            rendered = await self.ollama.generate(
                prompt=user,
                system=CORRECTION_COMPOSER_SYSTEM,
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception:
            rendered = None

        correction = CorrectionPrompt(
            attempt=attempt,
            max_retries=self.config.max_retries,
            remaining_attempts=self.config.max_retries - attempt,
            task_goal=intent.true_goal,
            task_goal_summary=intent.true_goal_summary,
            failed_checks=failed_checks,
            stack_traces=stack_traces,
            test_output=test_output,
            semantic_diff=semantic_diff,
            llm_text=rendered,
        )

        return correction

    def _progress_signature(self) -> Optional[str]:
        """A fingerprint of the repository's current state.

        None when no probe is configured, which disables the check rather
        than guessing.
        """
        if self._progress_probe is None:
            return None
        try:
            return self._progress_probe()
        except Exception:
            # A probe that cannot answer is not evidence of no progress.
            return None

    @staticmethod
    def _format_checks(checks: List[CheckResult]) -> str:
        if not checks:
            return "(none)"
        lines = []
        for check in checks:
            lines.append(
                f"- {check.kind.value}: `{check.command}` exit={check.exit_code} "
                f"passed={check.passed}"
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
            f"has_stubs={diff.has_todo_stubs}"
        )


__all__ = ["CorrectionLoop", "RetryExhausted"]
