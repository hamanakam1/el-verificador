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
from uuid import uuid4

from .acceptance import AcceptanceContract, resolve_acceptance
from .correction import CorrectionLoop, RetryExhausted
from .docker_runner import DockerRunner, SandboxAbortedError
from .evidence import (
    EvidenceArtifact,
    EvidenceKind,
    EvidenceStore,
    summarise_diff,
)
from .git_ops import GitOps
from .obligations import ObligationEvaluator, coverage, decide
from .ollama_client import OllamaClient
from .receipt import (
    ReceiptChain,
    artifact_records,
    build_receipt,
    outcome_for_decision,
)
from .schemas import (
    CheckResult,
    CorrectionPrompt,
    IntentPackage,
    SemanticDiff,
    VerificationAttempt,
    VerificationAudit,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)
from .semantic_diff import SemanticDiffEngine
from .ticket_board import FlagOutcome, TicketBoardAdapter


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
        self.acceptance_contract: Optional[AcceptanceContract] = None
        self.verified_state = "worktree"

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def verify(
        self,
        work_package: Dict[str, Any],
        repo_path: str,
        base_commit: str,
        head_commit: str = "WORKTREE",
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
                    VerificationDecision.ESCALATE,
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
        # Stage 0: resolve what "done" means for this work package. The
        # commands come from the work package's own acceptance contract when
        # it has one, and from the configured default when it does not.
        contract = resolve_acceptance(intent, self.config)
        self.acceptance_contract = contract

        # Stage 1: Independent acceptance testing inside the sandbox
        try:
            check_results = self.deps.docker.run_all(commands=contract.commands)
        except SandboxAbortedError as exc:
            # No semantic read or correction is safe while termination may be
            # unconfirmed. Preserve the lifecycle evidence and end this run.
            records: List[Dict[str, Any]] = [{
                "id": "sandbox_lifecycle",
                "kind": "acceptance_command",
                "statement": "Acceptance execution completed inside its time budget",
                "status": "unproven",
                "detail": str(exc),
                "termination_confirmed": exc.termination_confirmed,
            }]
            artifacts = self._seal_evidence(exc.checks, None, records, contract)
            return VerificationResult(
                work_package_id=intent.id, repo_path=repo_path,
                base_commit=base_commit, head_commit=head_commit,
                decision=VerificationDecision.ESCALATE, check_results=exc.checks,
                obligations=records, evidence_hashes=[a.sha256 for a in artifacts],
                evidence_artifacts=[a.model_dump(mode="json") for a in artifacts],
                attempts=[VerificationAttempt(
                    attempt=attempt, decision=VerificationDecision.ESCALATE,
                    check_results=exc.checks, obligations=records,
                    evidence_hashes=[a.sha256 for a in artifacts],
                )],
                passed=False, committed=False,
            )

        # Stage 2: Semantic diff / intent alignment
        diff_engine = SemanticDiffEngine(repo_path, base_commit, head_commit)
        semantic_diff = diff_engine.compute(intent)
        self.verified_state = "worktree" if diff_engine.covers_worktree else "head"

        # Stage 3: Resolve the intent into obligations and decide on them.
        evaluator = ObligationEvaluator(
            scope_mode=self.config.scope_mode,
            goal_terms_enforced=self.config.goal_terms_enforced,
        )
        obligations = evaluator.evaluate(
            intent,
            semantic_diff,
            check_results,
            acceptance_source=contract.source.value,
        )
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

        # Stage 4: seal the evidence. The decision rests on these bytes, so
        # they are written to the content-addressed store before the verdict
        # is recorded, and the record cites their hashes.
        artifacts = self._seal_evidence(
            check_results, semantic_diff, obligation_records, contract
        )

        return VerificationResult(
            work_package_id=intent.id,
            repo_path=repo_path,
            base_commit=base_commit,
            head_commit=head_commit,
            decision=decision,
            check_results=check_results,
            semantic_diff=semantic_diff,
            obligations=obligation_records,
            evidence_hashes=[a.sha256 for a in artifacts],
            evidence_artifacts=[a.model_dump(mode="json") for a in artifacts],
            passed=decision == VerificationDecision.PASS,
        )

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    def _seal_evidence(
        self,
        check_results: Sequence[CheckResult],
        semantic_diff: Optional[SemanticDiff],
        obligations: List[Dict[str, Any]],
        contract: Any,
    ) -> List[EvidenceArtifact]:
        """Write the evidence the decision rests on, and return the artifacts.

        The store is content-addressed, so identical evidence lands in the
        same place rather than accumulating copies, and every artifact can be
        re-hashed to prove it is the bytes the decision was made from.
        """
        artifacts: List[EvidenceArtifact] = []
        store = EvidenceStore(self.config.evidence_root)

        for check in check_results:
            label = check.command
            if check.stdout:
                artifacts.append(
                    store.write_text(
                        EvidenceKind.CHECK_STDOUT, check.stdout, label=label
                    )
                )
            if check.stderr:
                artifacts.append(
                    store.write_text(
                        EvidenceKind.CHECK_STDERR, check.stderr, label=label
                    )
                )
            artifacts.append(
                store.write_json(
                    EvidenceKind.CHECK_RESULT,
                    {
                        "kind": check.kind.value,
                        "command": check.command,
                        "exit_code": check.exit_code,
                        "execution": check.execution.value,
                        "duration_ms": check.duration_ms,
                        "passed": check.passed,
                    },
                    label=label,
                )
            )

        if semantic_diff is not None:
            artifacts.append(
                store.write_json(
                    EvidenceKind.AST_SUMMARY,
                    summarise_diff(semantic_diff),
                    label="semantic diff",
                )
            )

        artifacts.append(
            store.write_json(
                EvidenceKind.OBLIGATION_TRACE,
                {"obligations": obligations},
                label="resolved obligations",
            )
        )

        artifacts.append(
            store.write_json(
                EvidenceKind.SANDBOX_META,
                {
                    "image": self.config.docker_image,
                    "workdir": self.config.docker_workdir,
                    "network_enabled": self.config.docker_network_enabled,
                    "memory_limit_mb": self.config.docker_memory_limit_mb,
                    "cpu_limit": self.config.docker_cpu_limit,
                    "pids_limit": self.config.docker_pids_limit,
                    "check_timeout_s": self.config.docker_timeout,
                    "acceptance_commands": list(contract.commands),
                    "acceptance_source": contract.source.value,
                },
                label="sandbox",
            )
        )
        return artifacts

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
        if escalation_reason is None and result.decision == VerificationDecision.ESCALATE:
            escalation_reason = next(
                (str(o["detail"]) for o in result.obligations
                 if o.get("id") == "sandbox_lifecycle" and o.get("detail")), None)

        # Commit on pass, and only the files the verifier evaluated.
        #
        # The commit happens only when the verification judged the working
        # tree, because that is the content the commit carries. A
        # verification of a commit range did not judge the working tree, so
        # committing it would put unverified content in the record.
        if (
            result.decision == VerificationDecision.PASS
            and not self.config.dry_run
            and self.verified_state == "worktree"
        ):
            try:
                message = (
                    f"{self.config.commit_message_prefix} "
                    f"verify {intent.id} - {intent.true_goal_summary}"
                )
                scope = list(result.semantic_diff.files_changed) if result.semantic_diff else []
                commit_sha = self.deps.git.commit(
                    message,
                    scope,
                    self.config.commit_branch,
                    verified_state=self.verified_state,
                )
                result.committed = True
                result.edge_record_payload_hash = commit_sha
            except Exception:
                # Commit failure is not a verification failure; record it
                result.committed = False

        # The durable record: an EdgeRecord receipt, chained to the previous
        # one. Written whatever the verdict, because a decision that nobody
        # can find later is a decision that never happened.
        self._write_receipt(intent, repo_path, result, escalation_reason)

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
    # The durable record
    # ------------------------------------------------------------------ #
    def _flag_ticket(
        self,
        intent: IntentPackage,
        result: VerificationResult,
        escalation_reason: Optional[str],
    ) -> Optional[FlagOutcome]:
        """Flag the ticket for human attention, if that is switched on.

        Only an escalation is flagged: a failure that is still inside its
        retry budget is the pipeline's business, not the operator's.
        """
        if not self.config.ticket_board_enabled:
            return None
        if result.decision != VerificationDecision.ESCALATE:
            return None
        if not intent.ticket:
            return FlagOutcome(
                delivered=False,
                error="the work package named no ticket, so there is nothing to flag",
            )

        reason = (
            f"Verification escalated after {len(result.attempts)} attempt(s) for {intent.id} "
            f"({intent.true_goal_summary}). "
            f"{escalation_reason or ''} "
            f"{len(result.evidence_hashes)} evidence artifact(s); "
            f"receipt {result.receipt_id or 'pending'}."
        ).strip()
        try:
            adapter = TicketBoardAdapter(
                base_url=self.config.ticket_board_url,
                timeout=self.config.ticket_board_timeout,
            )
            return adapter.flag(intent.ticket, reason)
        except Exception as exc:  # noqa: BLE001
            return FlagOutcome(
                delivered=False,
                ticket=intent.ticket,
                error=f"the ticket board could not be reached: {exc}",
            )

    def _write_receipt(
        self,
        intent: IntentPackage,
        repo_path: str,
        result: VerificationResult,
        escalation_reason: Optional[str],
    ) -> None:
        """Append a receipt for this decision to the receipt chain."""
        flag_outcome = self._flag_ticket(intent, result, escalation_reason)
        try:
            chain = ReceiptChain(self.config.receipt_dir)
            receipt = build_receipt(
                receipt_id=result.receipt_id
                or f"va-audit-{uuid4().hex[:16]}",
                kind="verification",
                outcome=outcome_for_decision(result.decision.value),
                ticket=intent.ticket,
                actor="el-verificador",
                work_package_id=intent.id,
                artifacts=artifact_records(result.evidence_artifacts),
                notes=(
                    f"work package {intent.id}: {intent.true_goal_summary}. "
                    f"Decision {result.decision.value}. "
                    f"{len(result.evidence_hashes)} evidence artifact(s). "
                    f"{escalation_reason or ''}"
                ).strip(),
                before_state={
                    "base_commit": result.base_commit,
                    "head_commit": result.head_commit,
                    "repo_path": repo_path,
                },
                after_state={
                    "decision": result.decision.value,
                    "committed": result.committed,
                    "obligations_satisfied": sum(
                        1
                        for o in result.obligations
                        if o.get("status") == "satisfied"
                    ),
                    "obligations_total": len(result.obligations),
                },
                detected_changes=[
                    f"{o.get('kind')}: {o.get('detail', '')[:200]}"
                    for o in result.blocking_obligations
                ],
                summary=(
                    f"El Verificador {result.decision.value} for work package "
                    f"{intent.id} ({intent.true_goal_summary})."
                ),
            )
            if flag_outcome is not None:
                receipt["ticket_board"] = flag_outcome.as_dict()
            chain.append(receipt)
            result.receipt_id = receipt["receipt_id"]
            result.receipt_fingerprint = receipt["proof_fingerprint"]
            result.edge_record_payload_hash = receipt["full_hash"]
        except Exception:
            # A failure to record must never mask the decision itself.
            pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
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
            f"acceptance_source={self.acceptance_contract.source.value}"
            if self.acceptance_contract
            else "acceptance_source=config",
            f"acceptance_commands={len(self.acceptance_contract.commands)}"
            if self.acceptance_contract
            else "",
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
            evidence_artifacts=list(result.evidence_artifacts),
            obligations=list(result.obligations),
            total_duration_ms=total_duration_ms,
            model_calls=self._model_calls,
        )
        # Compute content hash for immutability
        audit.content_hash = audit.compute_hash()
        return audit


__all__ = ["ElVerificador", "VerifierDependencies"]
