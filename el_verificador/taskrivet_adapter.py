"""
Taskrivet adapter for El Verificador.

Closes the loop through Taskrivet's real coordination ledger instead of
leaving the Correction Prompt as an operator-facing printout.

The adapter drives Taskrivet's JSON CLI (`python -m taskrivet`) so that every
verification outcome is recorded as a first-class Taskrivet lifecycle event:

    ensure_task -> start_session -> (verify)
                                  -> fail + context-add + retry + start_session
                                  -> checkpoint + finish + accept

Taskrivet remains the owner of task state. This adapter never decides
acceptance on its own: it records evidence, and Taskrivet's `accept` remains
an explicit operator-grade transition.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .schemas import CorrectionPrompt, IntentPackage

# Bounded summary limits so correction prompts never carry unbounded payloads.
MAX_SUMMARY_CHARS = 400
MAX_REASON_CHARS = 300
MAX_EVIDENCE_CHARS = 1000


class TaskrivetError(RuntimeError):
    """Raised when a Taskrivet CLI invocation fails."""


class TaskrivetAdapter:
    """
    Thin, subprocess-based adapter over Taskrivet's JSON CLI.

    The CLI is invoked as `python -m taskrivet --db <path> <command> ...` so the
    adapter inherits Taskrivet's own validation, conflict rules, and event log
    rather than reimplementing them.
    """

    def __init__(
        self,
        db_path: str = "~/.taskrivet/ledger.sqlite",
        task_id: str = "",
        owner: str = "el-verificador",
        scope: Optional[List[str]] = None,
        token_budget: int = 8000,
        lease_seconds: int = 900,
        python_executable: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        self.db_path = str(Path(db_path).expanduser())
        self.task_id = task_id
        self.owner = owner
        self.scope = list(scope) if scope else ([task_id] if task_id else [])
        self.token_budget = token_budget
        self.lease_seconds = lease_seconds
        self.python_executable = python_executable or os.environ.get(
            "EL_VERIFICADOR_PYTHON", "python3"
        )
        # Optional environment override, used to point at a specific
        # Taskrivet installation without exporting anything globally.
        self.env = env
        self.session_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Low-level CLI
    # ------------------------------------------------------------------ #
    def _run(self, args: List[str], check: bool = True) -> Dict[str, Any]:
        cmd = [self.python_executable, "-m", "taskrivet", "--db", self.db_path] + args
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=self.env,
            )
        except FileNotFoundError as exc:
            raise TaskrivetError(f"taskrivet CLI not runnable: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TaskrivetError("taskrivet CLI timed out") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if check:
                raise TaskrivetError(
                    f"taskrivet {' '.join(args[:2])} failed: {detail[:500]}"
                )
            return {"error": detail}

        payload = proc.stdout.strip()
        if not payload:
            return {}
        try:
            return dict(json.loads(payload))
        except json.JSONDecodeError as exc:
            raise TaskrivetError(
                f"taskrivet returned non-JSON output: {payload[:200]}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Task lifecycle
    # ------------------------------------------------------------------ #
    def ensure_task(
        self,
        objective: str,
        acceptance: str,
        scope: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create the Taskrivet task if it does not already exist."""
        keys = list(scope) if scope else self.scope
        if not self.task_id:
            raise TaskrivetError("task_id is required to ensure a Taskrivet task")
        if not keys:
            keys = [self.task_id]

        args = [
            "task-add",
            self.task_id,
            "--objective",
            objective,
            "--acceptance",
            acceptance,
            "--token-budget",
            str(self.token_budget),
        ]
        for key in keys:
            args += ["--scope", key]

        result = self._run(args, check=False)
        if "error" in result:
            # Already exists: treat as success (idempotent bootstrap).
            return {"task_id": self.task_id, "created": False}
        return {"task_id": self.task_id, "created": True, **result}
    def start_session(self, owner: Optional[str] = None) -> str:
        """Start a Taskrivet session for the task. Returns the session id."""
        result = self._run(
            [
                "start",
                self.task_id,
                "--owner",
                owner or self.owner,
                "--lease",
                str(self.lease_seconds),
            ]
        )
        session_id = result.get("session_id")
        if not session_id:
            raise TaskrivetError(f"taskrivet start returned no session id: {result}")
        self.session_id = str(session_id)
        return str(session_id)

    def record_failure(self, category: str, summary: str) -> None:
        """Record a verification failure against the active session."""
        if not self.session_id:
            return
        self._run(
            [
                "fail",
                self.session_id,
                "--category",
                category,
                "--summary",
                summary[:MAX_SUMMARY_CHARS],
            ],
            check=False,
        )

    def attach_context(self, body: str, source: str) -> None:
        """Attach the Correction Prompt to the task as bounded context."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(body)
            temp_path = handle.name
        try:
            self._run(
                [
                    "context-add",
                    self.task_id,
                    "--file",
                    temp_path,
                    "--source",
                    source,
                ],
                check=False,
            )
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def request_retry(self, reason: str) -> None:
        """Move the task back to 'ready' so Taskrivet can re-execute it."""
        self._run(
            ["retry", self.task_id, "--reason", reason[:MAX_REASON_CHARS]],
            check=False,
        )

    def record_checkpoint(
        self,
        summary: str,
        evidence: str,
        next_step: str,
        tokens: int = 0,
    ) -> None:
        """Record a checkpoint on the active session."""
        if not self.session_id:
            return
        self._run(
            [
                "checkpoint",
                self.session_id,
                "--summary",
                summary[:MAX_SUMMARY_CHARS],
                "--evidence",
                evidence[:MAX_EVIDENCE_CHARS],
                "--next",
                next_step[:MAX_SUMMARY_CHARS],
                "--tokens",
                str(max(0, int(tokens))),
                "--lease",
                str(self.lease_seconds),
            ],
            check=False,
        )

    def record_finish(self, evidence: str) -> None:
        """Finish the active session, moving the task to review."""
        if not self.session_id:
            return
        self._run(
            ["finish", self.session_id, "--evidence", evidence[:MAX_EVIDENCE_CHARS]],
            check=False,
        )

    def accept(self, reason: str) -> None:
        """Accept the task. This is the explicit final transition."""
        self._run(
            ["accept", self.task_id, "--reason", reason[:MAX_REASON_CHARS]],
            check=False,
        )

    # ------------------------------------------------------------------ #
    # Verification-driven helpers
    # ------------------------------------------------------------------ #
    def ensure_task_from_intent(
        self, intent: Union[IntentPackage, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Create the Taskrivet task from a distilled work package.

        The acceptance criteria carried by the work package become the
        Taskrivet task's acceptance contract, so the two systems agree on
        what "done" means.

        Fields are read defensively: the work-package schema evolves, and a
        verifier that crashes on an older or newer schema would report a
        broken gate rather than a broken build.
        """
        if isinstance(intent, dict):
            intent = IntentPackage.from_dict(intent)

        acceptance = (
            getattr(intent, "acceptance_criteria", None)
            or "; ".join(getattr(intent, "acceptance_commands", None) or [])
            or intent.true_goal_summary
            or intent.true_goal
        )
        scope = (
            getattr(intent, "scope", None)
            or getattr(intent, "related_files", None)
            or None
        )
        return self.ensure_task(
            objective=intent.true_goal,
            acceptance=acceptance,
            scope=scope,
        )

    @staticmethod
    def _violations(prompt: CorrectionPrompt) -> List[Dict[str, Any]]:
        """Obligation violations, when the prompt schema carries them."""
        return list(getattr(prompt, "violations", None) or [])

    @staticmethod
    def describe_failure(prompt: CorrectionPrompt) -> str:
        """Build a bounded one-line description of why an attempt failed."""
        parts: List[str] = []
        violations = TaskrivetAdapter._violations(prompt)
        if violations:
            kinds = ", ".join(
                str(v.get("kind", "obligation")) for v in violations[:3]
            )
            parts.append(f"obligations({kinds})")
        if prompt.failed_checks:
            parts.append(
                ", ".join(
                    f"{c.kind.value}(exit={c.exit_code})" for c in prompt.failed_checks
                )
            )
        if not parts:
            parts.append("intent_alignment")
        return ": ".join(parts)

    def apply_correction(self, prompt: CorrectionPrompt) -> str:
        """
        Hand a Correction Prompt back to Taskrivet.

        Records the failure, attaches the bounded Correction Prompt as task
        context, moves the task back to 'ready', and opens a fresh session for
        the next attempt. Returns the new session id.
        """
        rendered = prompt.render()
        reason_parts = [self.describe_failure(prompt)]
        violations = self._violations(prompt)
        if violations:
            statements = [
                str(v.get("statement", "")).strip()
                for v in violations[:2]
                if v.get("statement")
            ]
            if statements:
                reason_parts.append("; ".join(statements))

        # A failure must have an owning session. If the caller never opened
        # one, open it now rather than silently dropping the failure record.
        if not self.session_id:
            self.start_session()

        self.record_failure(
            category="verification_failed",
            summary=(
                f"Attempt {prompt.attempt}/{prompt.max_retries}: "
                f"{reason_parts[0]}"
            ),
        )
        self.attach_context(rendered, source="el-verificador:correction-prompt")
        self.request_retry(
            reason=(
                f"Verification attempt {prompt.attempt} failed "
                f"({reason_parts[0]}); {prompt.remaining_attempts} retries remaining"
            )
        )
        return self.start_session()

    def apply_pass(self, evidence: str, tokens: int = 0) -> None:
        """Record a passing verification and accept the task."""
        self.record_checkpoint(
            summary="Verification passed: all acceptance checks and intent alignment",
            evidence=evidence,
            next_step="Operator review of committed change",
            tokens=tokens,
        )
        self.record_finish(evidence=evidence)
        self.accept(reason="El Verificador: acceptance checks and intent alignment passed")

    def apply_escalation(self, summary: str) -> None:
        """
        Flag the task for human intervention.

        The retry budget is exhausted. The last failure is recorded against
        the active session so Taskrivet blocks the task and shows the
        evidence, instead of leaving an abandoned session and a task that
        looks like it is still running.
        """
        self.record_failure(
            category="verification_escalated",
            summary=summary[:MAX_SUMMARY_CHARS],
        )

    def retry_callback(self) -> Callable[[CorrectionPrompt], None]:
        """
        Build the `taskrivet_retry` callback expected by `ElVerificador.verify`.

        The returned callable records the failure in Taskrivet's ledger,
        attaches the Correction Prompt as task context, and reopens a session
        for the next attempt.
        """
        adapter = self

        def _retry(prompt: CorrectionPrompt) -> None:
            adapter.apply_correction(prompt)

        return _retry


# ============================================================================
# CLI INTEGRATION
# ============================================================================

def add_taskrivet_arguments(parser: Any) -> None:
    """Register the Taskrivet integration flags on an argument parser."""
    parser.add_argument(
        "--taskrivet-db",
        default=None,
        help="Path to the Taskrivet ledger (enables the Taskrivet integration)",
    )
    parser.add_argument(
        "--taskrivet-task",
        default=None,
        help="Taskrivet task id that records this verification",
    )
    parser.add_argument(
        "--taskrivet-owner",
        default="el-verificador",
        help="Owner recorded on the Taskrivet session",
    )
    parser.add_argument(
        "--taskrivet-scope",
        action="append",
        default=None,
        help="Resource key for conflict detection (repeatable)",
    )


def adapter_from_args(args: Any) -> Optional[TaskrivetAdapter]:
    """
    Build a Taskrivet adapter from parsed CLI arguments.

    Returns None when the integration was not requested, so the caller keeps
    its existing behaviour unchanged.
    """
    db_path = getattr(args, "taskrivet_db", None)
    task_id = getattr(args, "taskrivet_task", None)
    if not db_path or not task_id:
        return None

    scope = list(getattr(args, "taskrivet_scope", None) or [])
    return TaskrivetAdapter(
        db_path=db_path,
        task_id=task_id,
        owner=getattr(args, "taskrivet_owner", None) or "el-verificador",
        scope=scope or None,
    )


__all__ = [
    "TaskrivetAdapter",
    "TaskrivetError",
    "add_taskrivet_arguments",
    "adapter_from_args",
]
