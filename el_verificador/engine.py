"""
El Verificador engine — the autonomous gatekeeper.

Orchestrates the full verification cycle:

1. Build an IntentPackage from the DistilledWorkPackage
2. Provision the Docker sandbox
3. Run acceptance checks (independent of Taskrivet's self-report)
4. Compute the semantic diff vs. the distilled intent
5. On failure: run the guardrailed Correction Loop (max_retries=3)
6. On pass: commit code, teardown the sandbox, fire an EdgeRecord audit payload
7. On exhaustion: escalate to human with full context
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .correction import CorrectionLoop, RetryExhausted
from .docker_runner import DockerRunner
from .git_ops import GitOps
from .ollama_client import OllamaClient
from .schemas import (
    CorrectionPrompt,
    IntentPackage,
    SemanticDiff,
    VerificationAudit,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)
from .semantic_diff import SemanticDiffEngine


@dataclass
class VerifierDependencies:
    """Injected dependencies (allows monkeypatching in tests)."""

    docker: DockerRunner
    git: GitOps
    ollama: OllamaClient
    audit_logger = None  # EdgeRecordLogger | None


class ElVerificador:
    def __init__(
        self,
        config: Optional[VerificationConfig] = None,
        deps: Optional[VerifierDependencies] = None,
    ):
        self.config = config or VerificationConfig()
        self.deps = deps or VerifierDependencies(
            docker=DockerRunner(self.config),
            git=GitOps("."),  # repo path set per verify() call
            ollama=OllamaClient(
                self.config.ollama_base_url,
                self.config.model,
                self.config.model_temperature,
                self.config.model_timeout,
            ),
        )

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def verify(
        self,
        work_package: dict,
        repo_path: str,
        base_commit: str,
        head_commit: str = "HEAD",
        taskrivet_retry: Optional[Callable[[CorrectionPrompt], None]] = None,
    ) -> VerificationResult:
        """
        Verify one work package against one repository.

        taskrivet_retry is called with each Correction Prompt; it should have
        Taskrivet re-execute its work inside the sandbox before the next
        verification attempt. If omitted, failures are not retried.
        """
        start = time.time()
        intent = IntentPackage.from_dict(work_package)
        repo_path = str(Path(repo_path).resolve())

        # Repoint git ops to the target repo
        self.deps.git = GitOps(repo_path)

        # Provision the sandbox once; keep it alive across retries
        self.deps.docker.start(repo_path)

        try:
            # Single-pass verification used by the retry loop
            def run_once(attempt: int) -> VerificationResult:
                return self._verify_once(
                    attempt, intent, repo_path, base_commit, head_commit
                )

            loop = CorrectionLoop(self.config, self.deps.ollama, run_once)

            if taskrivet_retry is None:
                # Single attempt, no retry
                result = run_once(1)
                if result.decision != VerificationDecision.PASS:
                    result.decision = VerificationDecision.FAIL
                return self._finalize(result, intent, repo_path, start)

            try:
                result = await loop.run_until_pass_or_exhausted(
                    intent, base_commit, head_commit, taskrivet_retry
                )
                return self._finalize(result, intent, repo_path, start)
            except RetryExhausted as exc:
                # Escalation path: hard iteration limit reached.
                #
                # The escalation exists to deliver the evidence a human needs
                # to fix the ticket. The last attempt's check results and
                # semantic diff are carried forward, so the record that
                # reaches the human is the record of what actually failed.
                last = loop.attempts[-1] if loop.attempts else None
                result = VerificationResult(
                    work_package_id=intent.id,
                    repo_path=repo_path,
                    base_commit=base_commit,
                    head_commit=head_commit,
                    decision=VerificationDecision.ESCALATE,
                    attempts=loop.attempts,
                    check_results=list(last.check_results) if last else [],
                    semantic_diff=last.semantic_diff if last else None,
                    passed=False,
                    committed=False,
                )
                return self._finalize(result, intent, repo_path, start, str(exc))
        finally:
            # Always release compute resources
            self.deps.docker.teardown()

    # ------------------------------------------------------------------ #
    # Single verification pass (checks + semantic diff, no retry logic)
    # ------------------------------------------------------------------ #
    def _verify_once(
        self,
        attempt: int,
        intent: IntentPackage,
        repo_path: str,
        base_commit: str,
        head_commit: str,
    ) -> VerificationResult:
        # Stage 1: Independent acceptance testing inside the sandbox
        check_results = self.deps.docker.run_all()

        # Stage 2: Semantic diff / intent alignment
        diff_engine = SemanticDiffEngine(repo_path, base_commit, head_commit)
        semantic_diff = diff_engine.compute(intent)

        all_checks_pass = all(c.passed for c in check_results)
        intent_aligned = self._intent_aligned(semantic_diff)
        if all_checks_pass and intent_aligned:
            decision = VerificationDecision.PASS
        else:
            decision = VerificationDecision.FAIL

        return VerificationResult(
            work_package_id=intent.id,
            repo_path=repo_path,
            base_commit=base_commit,
            head_commit=head_commit,
            decision=decision,
            check_results=check_results,
            semantic_diff=semantic_diff,
            passed=decision == VerificationDecision.PASS,
        )

    # ------------------------------------------------------------------ #
    # Finalization: commit, teardown, EdgeRecord audit payload
    # ------------------------------------------------------------------ #
    def _finalize(
        self,
        result: VerificationResult,
        intent: IntentPackage,
        repo_path: str,
        start: float,
        escalation_reason: Optional[str] = None,
    ) -> VerificationResult:
        total_duration_ms = int((time.time() - start) * 1000)

        # Commit on pass
        if result.decision == VerificationDecision.PASS and not self.config.dry_run:
            try:
                message = (
                    f"{self.config.commit_message_prefix} "
                    f"verify {intent.id} - {intent.true_goal_summary}"
                )
                # Only the files the verification actually reviewed may be
                # staged. The commit path refuses anything else; see
                # GitOps.commit.
                reviewed = list(result.semantic_diff.files_changed) if result.semantic_diff else []
                outcome = self.deps.git.commit(
                    message,
                    self.config.commit_branch,
                    paths=reviewed,
                    expected_head=result.head_commit,
                )
                result.committed = outcome.status == "committed"
                result.edge_record_payload_hash = outcome.sha
                result.commit_note = f"{outcome.status}: {outcome.message}"
            except Exception as exc:
                # A refused commit is not a verification failure. The reason
                # is recorded, because "nothing was committed" without a
                # reason is not an audit trail.
                result.committed = False
                result.commit_note = f"not committed: {exc}"

        # Build the EdgeRecord audit payload (cryptographically secure)
        audit = self._build_audit(
            intent, repo_path, result, total_duration_ms, escalation_reason
        )
        if not self.config.dry_run and self.deps.audit_logger is not None:
            try:
                self.deps.audit_logger.log_decision(audit)
                result.edge_record_payload_hash = audit.content_hash
            except Exception:
                # Never let audit failure mask the verification outcome
                pass

        result.attempts = list(result.attempts)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _intent_aligned(self, diff: SemanticDiff) -> bool:
        """Did the change actually address the intent?

        Three rules, none of which depends on the coverage score:

        1. Something must have changed.
        2. There must be no stub left behind.
        3. If the goal names identifiers, the change must provide at least
           one of them. A change that contains none of the things the goal
           asked for is not an attempt at the goal, whatever else it does.

        The coverage score is reported with its inputs but is not yet a gate.
        The decision model that replaces it is specified in ROADMAP.md
        (Phase 2, the obligation model); until that lands, a threshold that
        is not calibrated against real work packages would fail correct
        implementations that spell a name differently.
        """
        if not diff.files_changed:
            return False
        if self.config.fail_on_todo_stubs and diff.has_todo_stubs:
            return False
        if diff.named_identifiers and not diff.covered_tokens:
            return False
        return True

    def _coverage_evidence(self, diff: Optional[SemanticDiff]) -> list[str]:
        """Publish the inputs of the coverage score alongside the score.

        A coverage number with no inputs cannot be audited, and a reviewer
        cannot tell a correct implementation from a stub by looking at 0.30.
        """
        if diff is None:
            return []
        evidence = [f"coverage_basis={diff.coverage_basis}"]
        if diff.goal_tokens:
            evidence.append(
                f"goal_tokens={len(diff.goal_tokens)} "
                f"covered={len(diff.covered_tokens)} "
                f"uncovered={len(diff.uncovered_tokens)}"
            )
        if diff.uncovered_tokens:
            evidence.append(
                "uncovered_goal_words=" + ",".join(diff.uncovered_tokens[:12])
            )
        if diff.named_identifiers:
            evidence.append(
                "named_identifiers=" + ",".join(diff.named_identifiers[:12])
            )
        if diff.related_files_touched:
            evidence.append(
                "related_files_touched=" + ",".join(diff.related_files_touched[:12])
            )
        return evidence

    def _build_audit(
        self,
        intent: IntentPackage,
        repo_path: str,
        result: VerificationResult,
        total_duration_ms: int,
        escalation_reason: Optional[str],
    ) -> VerificationAudit:
        if result.decision == VerificationDecision.PASS:
            rationale = (
                f"All acceptance checks passed and semantic intent aligned "
                f"for work package {intent.id}: {intent.true_goal_summary}"
            )
            key_evidence = [
                f"checks_passed={all(c.passed for c in result.check_results)}",
                f"intent_coverage={result.semantic_diff.intent_coverage:.2f}"
                if result.semantic_diff
                else "no_diff",
                f"attempts={len(result.attempts)}",
                f"committed={result.committed}",
            ]
        elif result.decision == VerificationDecision.ESCALATE:
            rationale = (
                f"Verification exhausted after {len(result.attempts)} attempts; "
                f"escalating to human intervention. {escalation_reason or ''}"
            ).strip()
            key_evidence = [
                f"attempts={len(result.attempts)}",
                f"max_retries={self.config.max_retries}",
                "escalation=true",
                # The evidence the escalation exists to deliver.
                f"checks_passed={all(c.passed for c in result.check_results)}"
                if result.check_results
                else "no_check_results",
                "failed_checks="
                + ",".join(
                    f"{c.kind.value}:{c.command}"
                    for c in result.check_results
                    if not c.passed
                )
                if any(not c.passed for c in result.check_results)
                else "no_failed_checks",
                f"intent_coverage={result.semantic_diff.intent_coverage:.2f}"
                if result.semantic_diff
                else "no_diff",
            ]
        else:
            rationale = (
                f"Verification failed for work package {intent.id}: "
                f"{intent.true_goal_summary}"
            )
            key_evidence = [
                f"checks_passed={all(c.passed for c in result.check_results)}",
                f"intent_coverage={result.semantic_diff.intent_coverage:.2f}"
                if result.semantic_diff
                else "no_diff",
                f"attempts={len(result.attempts)}",
            ]
        key_evidence.extend(self._coverage_evidence(result.semantic_diff))
        if result.commit_note:
            key_evidence.append(f"commit={result.commit_note}")

        audit = VerificationAudit(
            work_package_id=intent.id,
            work_package_preview=intent.true_goal[:500],
            repo_path=repo_path,
            decision=result.decision,
            attempts=len(result.attempts),
            passed=result.passed,
            committed=result.committed,
            rationale=rationale,
            key_evidence=key_evidence,
            total_duration_ms=total_duration_ms,
            model_calls=0,
        )
        # Compute content hash for immutability
        audit.content_hash = audit.compute_hash()
        return audit


__all__ = ["ElVerificador", "VerifierDependencies"]
