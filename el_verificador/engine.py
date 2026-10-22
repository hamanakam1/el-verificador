"""
El Verificador engine — the autonomous gatekeeper.

Orchestrates the full verification cycle:

1. Build an IntentPackage from the DistilledWorkPackage
2. Provision the Docker sandbox
3. Run acceptance checks (independent of Taskrivet's self-report)
4. Compute the semantic diff vs. the distilled intent
5. Resolve the intent into obligations and decide on them
6. On failure: run the guardrailed Correction Loop (max_retries=3)
7. On pass: commit the verified change set, tear down the sandbox, fire an
   EdgeRecord audit payload
8. On exhaustion: escalate to human with full context

The decision is a pure function of the resolved obligations
(`obligations.decide`). Nothing else decides a pass or a fail.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .correction import CorrectionLoop, RetryExhausted
from .docker_runner import DockerRunner
from .git_ops import GitOps
from .obligations import ObligationEvaluator, coverage, decide
from .ollama_client import OllamaClient
from .schemas import (
    CheckResult,
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
    audit_logger: Optional[Any] = None  # EdgeRecordLogger | None


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
        self._model_calls = 0

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def verify(
        self,
        work_package: Dict[str, Any],
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

            loop = CorrectionLoop(
                self.config,
                self.deps.ollama,
                run_once,
                self.deps.git.worktree_fingerprint,
            )

            if taskrivet_retry is None:
                # Single attempt, no retry
                result = run_once(1)
                if result.decision not in (
                    VerificationDecision.PASS,
                    VerificationDecision.INCONCLUSIVE,
                ):
                    result.decision = VerificationDecision.FAIL
                return self._finalize(result, intent, repo_path, start)

            try:
                result = await loop.run_until_pass_or_exhausted(
                    intent, base_commit, head_commit, taskrivet_retry
                )
                self._model_calls += loop.model_calls
                return self._finalize(result, intent, repo_path, start)
            except RetryExhausted as exc:
                self._model_calls += loop.model_calls
                # Escalation path: hard iteration limit reached. The evidence
                # is carried forward: the escalation is only useful if it
                # arrives with the failure that caused it.
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
                    obligations=list(last.obligations) if last else [],
                    evidence_hashes=list(last.evidence_hashes) if last else [],
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

        # Stage 3: Resolve the intent into obligations and decide on them.
        evaluator = ObligationEvaluator(scope_mode=self.config.scope_mode)
        obligations = evaluator.evaluate(intent, semantic_diff, check_results)
        semantic_diff.intent_coverage = coverage(obligations)
        decision = decide(obligations)

        obligation_records = [
            {
                "id": o.id,
                "kind": o.kind.value,
                "statement": o.statement,
                "status": o.status.value,
                "detail": o.detail,
            }
            for o in obligations
        ]

        return VerificationResult(
            work_package_id=intent.id,
            repo_path=repo_path,
            base_commit=base_commit,
            head_commit=head_commit,
            decision=decision,
            check_results=check_results,
            semantic_diff=semantic_diff,
            obligations=obligation_records,
            evidence_hashes=self._evidence_hashes(check_results, semantic_diff),
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

        # Commit on pass, and only the files the verifier evaluated.
        if result.decision == VerificationDecision.PASS and not self.config.dry_run:
            try:
                message = (
                    f"{self.config.commit_message_prefix} "
                    f"verify {intent.id} - {intent.true_goal_summary}"
                )
                scope = list(result.semantic_diff.files_changed) if result.semantic_diff else []
                commit_sha = self.deps.git.commit(
                    message, scope, self.config.commit_branch
                )
                result.committed = True
                result.edge_record_payload_hash = commit_sha
            except Exception:
                # Commit failure is not a verification failure; record it
                result.committed = False

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
    @staticmethod
    def _evidence_hashes(
        check_results: Sequence[CheckResult], semantic_diff: Optional[SemanticDiff]
    ) -> List[str]:
        """Content hashes of the evidence the decision rests on.

        The hash of each check's output binds the audit record to the bytes
        that were actually produced, so a later reader can prove the verdict
        was made from this evidence and not from something else.
        """
        import hashlib

        hashes: List[str] = []
        for check in check_results:
            if not check.executed:
                continue
            digest = hashlib.sha256()
            digest.update(check.kind.value.encode())
            digest.update(b"\0")
            digest.update(check.command.encode())
            digest.update(b"\0")
            digest.update(str(check.exit_code).encode())
            digest.update(b"\0")
            digest.update(check.stdout.encode(errors="replace"))
            digest.update(b"\0")
            digest.update(check.stderr.encode(errors="replace"))
            hashes.append(digest.hexdigest())

        if semantic_diff is not None and semantic_diff.files_changed:
            digest = hashlib.sha256()
            digest.update("|".join(sorted(semantic_diff.files_changed)).encode())
            digest.update(b"\0")
            digest.update(
                "|".join(
                    sorted(
                        f"{c.file_path}:{c.name}:{c.change_type.value}"
                        for c in semantic_diff.ast_changes
                    )
                ).encode()
            )
            hashes.append(digest.hexdigest())
        return hashes

    def _build_audit(
        self,
        intent: IntentPackage,
        repo_path: str,
        result: VerificationResult,
        total_duration_ms: int,
        escalation_reason: Optional[str],
    ) -> VerificationAudit:
        blocking = result.blocking_obligations
        if result.decision == VerificationDecision.PASS:
            rationale = (
                f"All obligations satisfied for work package {intent.id}: "
                f"{intent.true_goal_summary}"
            )
        elif result.decision == VerificationDecision.ESCALATE:
            rationale = (
                f"Verification exhausted after {len(result.attempts)} attempts; "
                f"escalating to human intervention. {escalation_reason or ''}"
            ).strip()
        elif result.decision == VerificationDecision.INCONCLUSIVE:
            rationale = (
                f"Verification reached no verdict for work package {intent.id}: "
                f"{intent.true_goal_summary}"
            )
        else:
            rationale = (
                f"Verification failed for work package {intent.id}: "
                f"{intent.true_goal_summary}"
            )

        key_evidence = [
            f"decision={result.decision.value}",
            f"attempts={len(result.attempts)}",
            f"obligations_satisfied="
            f"{sum(1 for o in result.obligations if o.get('status') == 'satisfied')}"
            f"/{len(result.obligations)}",
            f"checks_passed={all(c.passed for c in result.check_results)}",
            f"checks_executed={sum(1 for c in result.check_results if c.executed)}"
            f"/{len(result.check_results)}",
            f"intent_coverage={result.semantic_diff.intent_coverage:.2f}"
            if result.semantic_diff
            else "no_diff",
            f"committed={result.committed}",
        ]
        for obligation in blocking:
            key_evidence.append(
                f"blocking:{obligation.get('kind')}={obligation.get('detail', '')[:160]}"
            )

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
            evidence_hashes=list(result.evidence_hashes),
            obligations=list(result.obligations),
            total_duration_ms=total_duration_ms,
            model_calls=self._model_calls,
        )
        # Compute content hash for immutability
        audit.content_hash = audit.compute_hash()
        return audit


__all__ = ["ElVerificador", "VerifierDependencies"]
