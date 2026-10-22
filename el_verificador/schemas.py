"""
Core data contracts for El Verificador.

Defines the exact data contracts flowing through the verification pipeline:

DistilledWorkPackage -> Acceptance Test -> Semantic Diff -> [Correction Prompt
x max_retries] -> Commit & Teardown -> EdgeRecord Audit
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    """Timezone-aware UTC now.

    `datetime.utcnow()` is deprecated and returns a naive datetime, which makes
    audit records from different processes hard to order.
    """
    return datetime.now(UTC)


# ============================================================================
# ENUMS
# ============================================================================

class VerificationDecision(str, Enum):
    """Outcome of a verification run."""
    PASS = "pass"           # All obligations satisfied; ready to commit
    FAIL = "fail"           # Hard failure; not safe to retry
    RETRY = "retry"         # Retry with a Correction Prompt (bounded)
    ESCALATE = "escalate"   # max_retries exhausted; flag for human intervention
    INCONCLUSIVE = "inconclusive"  # Checks were not executed; no verdict was reached


class CheckKind(str, Enum):
    """Kind of acceptance check executed in the Docker sandbox."""
    UNIT_TESTS = "unit_tests"
    LINT = "lint"
    SECURITY_SCAN = "security_scan"


class CheckExecution(str, Enum):
    """Where a check's result actually came from.

    `CONTAINER` is the only source that may justify a PASS. `REPLAY` is a
    recorded result from an earlier real run, used by fixture-driven
    development. `SKIPPED` means the command was never run, so the result
    carries no evidence at all.
    """

    CONTAINER = "container"  # ran inside the isolated sandbox
    REPLAY = "replay"        # replayed from a recorded fixture
    SKIPPED = "skipped"      # never ran; carries no verdict



class ChangeType(str, Enum):
    """Kind of AST-level change detected in the semantic diff."""
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"


class SymbolKind(str, Enum):
    """Kind of symbol affected by an AST change."""
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    VARIABLE = "variable"
    IMPORT = "import"


# ============================================================================
# CORE MODELS
# ============================================================================

class CheckResult(BaseModel):
    """Result of a single acceptance check executed in the sandbox."""
    model_config = ConfigDict(extra="forbid")

    kind: CheckKind
    command: str = Field(..., description="Command executed inside the container")
    exit_code: int = Field(..., description="Process exit code (0 = success)")
    stdout: str = Field(default="", description="Captured stdout")
    stderr: str = Field(default="", description="Captured stderr")
    duration_ms: int = Field(default=0, description="Check duration in milliseconds")
    execution: CheckExecution = Field(
        default=CheckExecution.CONTAINER,
        description="Where this result came from; SKIPPED results never pass",
    )

    @property
    def passed(self) -> bool:
        """A check passes only when it actually ran and exited zero."""
        return self.execution is not CheckExecution.SKIPPED and self.exit_code == 0

    @property
    def executed(self) -> bool:
        """True when this result carries the outcome of a real execution."""
        return self.execution is not CheckExecution.SKIPPED

    @property
    def failure_summary(self) -> str:
        """Bounded summary for inclusion in a Correction Prompt."""
        tail = self.stderr.strip() or self.stdout.strip()
        lines = [ln for ln in tail.splitlines() if ln.strip()][-20:]
        return "\n".join(lines)


class ASTChange(BaseModel):
    """A single AST-level change detected between base and head."""
    model_config = ConfigDict(extra="forbid")

    change_type: ChangeType
    symbol_kind: SymbolKind
    name: str = Field(..., description="Symbol name (e.g., 'create_user_route')")
    file_path: str
    line_no: Optional[int] = None
    before: Optional[str] = None  # Source snippet at base (for modified/removed)
    after: Optional[str] = None   # Source snippet at head (for added/modified)


class SemanticDiff(BaseModel):
    """
    Comparison of actual code changes against the DistilledWorkPackage intent.

    Answers: did Taskrivet build what was asked, or just write stubs/TODOs?
    """
    model_config = ConfigDict(extra="forbid")

    repo_path: str
    base_commit: str
    head_commit: str
    files_changed: List[str] = Field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    ast_changes: List[ASTChange] = Field(default_factory=list)
    intent_coverage: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Fraction of the distilled intent obligations that are satisfied",
    )
    has_todo_stubs: bool = Field(
        default=False,
        description="Whether changed code contains TODO/pass stubs",
    )
    stub_symbols: List[str] = Field(
        default_factory=list,
        description="Qualified names of symbols that are structurally stubs",
    )
    deleted_files: List[str] = Field(
        default_factory=list,
        description="Files removed by the diff",
    )
    weakened_tests: List[str] = Field(
        default_factory=list,
        description="Tests that were removed, skipped, or de-asserted by the diff",
    )

    @property
    def modified_symbols(self) -> List[str]:
        """Qualified names of symbols whose body was rewritten."""
        return [
            f"{c.file_path}::{c.name}"
            for c in self.ast_changes
            if c.change_type == ChangeType.MODIFIED
        ]

    @property
    def added_symbols(self) -> List[str]:
        return [
            f"{c.file_path}::{c.name}"
            for c in self.ast_changes
            if c.change_type == ChangeType.ADDED
        ]

    @property
    def aligned(self) -> bool:
        """Intent alignment: meaningful changes, no stubs, no weakened tests."""
        return (
            bool(self.files_changed)
            and not self.has_todo_stubs
            and not self.weakened_tests
        )


class IntentPackage(BaseModel):
    """
    Lightweight view of a DistilledWorkPackage consumed by El Verificador.

    Constructed from El Destilador's DistilledWorkPackage (dict or model).
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="DistilledWorkPackage id (dwp-...)")
    true_goal: str = Field(..., min_length=10)
    true_goal_summary: str = Field(..., max_length=200)
    related_files: List[str] = Field(default_factory=list)
    context_packet: Optional[Dict[str, Any]] = None
    estimated_tokens: int = Field(default=8000, ge=1000)
    # The acceptance contract, when the work package carries one. Previously
    # dropped on the floor, which is why the verifier ran a hardcoded
    # three-command list regardless of what the work actually asked for.
    acceptance_criteria: Optional[str] = None
    acceptance_commands: List[str] = Field(default_factory=list)
    suggested_kind: str = "task"
    scope: List[str] = Field(default_factory=list)
    ticket: Optional[str] = Field(
        default=None, description="The board ticket this work package belongs to"
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IntentPackage":
        return cls(
            id=data.get("id", f"dwp-{uuid4().hex[:12]}"),
            true_goal=data.get("true_goal", ""),
            true_goal_summary=data.get("true_goal_summary", "")[:200],
            related_files=data.get("related_files", []),
            context_packet=data.get("context_packet"),
            estimated_tokens=data.get("estimated_tokens", 8000),
            acceptance_criteria=data.get("acceptance_criteria")
            or data.get("acceptance"),
            acceptance_commands=list(data.get("acceptance_commands", []) or []),
            suggested_kind=data.get("suggested_kind", "task"),
            scope=list(data.get("scope", []) or data.get("suggested_scope", []) or []),
            ticket=data.get("ticket") or data.get("ticket_id"),
        )


class CorrectionPrompt(BaseModel):
    """
    A bounded, strict Correction Prompt handed back to Taskrivet on failure.

    Contains exactly the evidence needed to fix the failure: the obligations
    that were violated, stack traces, failed test output, semantic diff, and
    the remaining attempt budget.

    The prompt is derived from the *resolved obligations*, not from a model's
    opinion. `narrative` carries a locally-generated explanation of the same
    evidence; it is additive, and the deterministic sections below stand on
    their own without it.
    """
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(..., ge=1, description="Retry attempt number (1-based)")
    max_retries: int = Field(..., ge=1, description="Hard iteration limit")
    remaining_attempts: int = Field(..., ge=0)

    task_goal: str
    task_goal_summary: str
    failed_checks: List[CheckResult] = Field(default_factory=list)
    stack_traces: List[str] = Field(default_factory=list)
    test_output: str = Field(default="")
    semantic_diff: Optional[SemanticDiff] = None
    violations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Obligations that blocked a pass: kind, statement, detail",
    )
    narrative: Optional[str] = Field(
        default=None,
        description="Optional locally-generated explanation of the evidence",
    )

    def render(self) -> str:
        """Render the full Correction Prompt text for Taskrivet."""
        lines = [
            "# Correction Prompt (El Verificador)",
            "",
            "## Task",
            f"Goal: {self.task_goal}",
            f"Summary: {self.task_goal_summary}",
            "",
            f"## Attempt {self.attempt} of {self.max_retries}",
            f"Remaining retries: {self.remaining_attempts}",
            "",
        ]
        if self.violations:
            lines.append("## Obligations Not Satisfied")
            for violation in self.violations:
                kind = violation.get("kind", "obligation")
                statement = violation.get("statement", "")
                detail = violation.get("detail", "")
                lines.append(f"- **{kind}** — {statement}")
                if detail:
                    lines.append(f"  - {detail}")
            lines.append("")
        if self.narrative:
            lines.append("## Analysis")
            lines.append(self.narrative.strip()[:4000])
            lines.append("")
        lines.append("## Failed Checks")
        for check in self.failed_checks:
            lines.append(f"### {check.kind.value}")
            lines.append(f"Command: `{check.command}`")
            lines.append(f"Exit code: {check.exit_code}")
            if not check.executed:
                lines.append("This command did not run, so it carries no result.")
            if check.failure_summary:
                lines.append("```")
                lines.append(check.failure_summary)
                lines.append("```")
            lines.append("")
        if self.stack_traces:
            lines.append("## Stack Traces")
            for trace in self.stack_traces:
                lines.append("```")
                lines.append(trace[-3000:])  # bound trace size
                lines.append("```")
                lines.append("")
        if self.test_output:
            lines.append("## Failed Test Output")
            lines.append("```")
            lines.append(self.test_output[-3000:])
            lines.append("```")
            lines.append("")
        if self.semantic_diff:
            lines.append("## Semantic Diff (Intent Alignment)")
            lines.append(
                f"Files changed: {', '.join(self.semantic_diff.files_changed) or '(none)'}"
            )
            lines.append(
                f"Intent coverage: {self.semantic_diff.intent_coverage:.2f}"
            )
            if self.semantic_diff.has_todo_stubs:
                lines.append(
                    f"Stub symbols detected: {', '.join(self.semantic_diff.stub_symbols)}"
                )
            if self.semantic_diff.weakened_tests:
                lines.append(
                    f"Weakened tests: {', '.join(self.semantic_diff.weakened_tests)}"
                )
            lines.append("")
        lines.append("## Required Action")
        lines.append(
            "Fix the failures above, re-run acceptance tests, and report back. "
            "Do not add new TODO stubs, and do not weaken or delete tests. "
            "Every change must map to the task goal."
        )
        lines.append(
            "Change the code under test only. The configuration, the check "
            "fixture, the acceptance commands and the thresholds are not yours "
            "to adjust, and a check that did not run is not a failure to work "
            "around."
        )
        return "\n".join(lines)


class VerificationAttempt(BaseModel):
    """A single verification attempt (initial run or retry)."""
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(..., ge=1)
    decision: VerificationDecision
    check_results: List[CheckResult] = Field(default_factory=list)
    semantic_diff: Optional[SemanticDiff] = None
    correction_prompt: Optional[CorrectionPrompt] = None
    obligations: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_hashes: List[str] = Field(default_factory=list)
    duration_ms: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)


class VerificationResult(BaseModel):
    """Complete result of verifying one work package in one repo."""
    model_config = ConfigDict(extra="forbid")

    work_package_id: str
    repo_path: str
    base_commit: str
    head_commit: str
    decision: VerificationDecision
    attempts: List[VerificationAttempt] = Field(default_factory=list)
    check_results: List[CheckResult] = Field(default_factory=list)
    semantic_diff: Optional[SemanticDiff] = None
    obligations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Resolved obligations for the final attempt",
    )
    evidence_hashes: List[str] = Field(
        default_factory=list,
        description="Content hashes of the evidence the decision rests on",
    )
    evidence_artifacts: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="The stored evidence artifacts: path, bytes and hash",
    )
    passed: bool = False
    committed: bool = False
    edge_record_payload_hash: Optional[str] = None
    receipt_id: Optional[str] = Field(
        default=None, description="The EdgeRecord receipt for this decision"
    )
    receipt_fingerprint: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utcnow)

    @property
    def final_attempt(self) -> Optional[VerificationAttempt]:
        return self.attempts[-1] if self.attempts else None

    @property
    def blocking_obligations(self) -> List[Dict[str, Any]]:
        """Obligations that prevented a pass."""
        return [
            o
            for o in self.obligations
            if o.get("status") in ("violated", "unproven")
        ]


# ============================================================================
# EDGERECORD AUDIT MODELS
# ============================================================================

class VerificationAudit(BaseModel):
    """
    Immutable audit record of a verification decision.

    Written to EdgeRecord for an immutable audit trail of why El Verificador
    passed, failed, or escalated a work package.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"va-audit-{uuid4().hex[:16]}")
    timestamp: datetime = Field(default_factory=_utcnow)

    work_package_id: str
    work_package_preview: str = Field(max_length=500)
    repo_path: str

    decision: VerificationDecision
    attempts: int
    passed: bool
    committed: bool

    rationale: str = Field(..., min_length=10)
    key_evidence: List[str] = Field(default_factory=list)

    # The evidence this decision rests on. Every entry is a content hash of a
    # sealed artifact, so the audit record attests to bytes and not to prose.
    evidence_hashes: List[str] = Field(default_factory=list)
    evidence_artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    obligations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Resolved obligations: kind, status and detail",
    )

    # Cryptographic immutability
    content_hash: Optional[str] = Field(
        default=None, description="SHA256 of the decision content"
    )
    previous_audit_hash: Optional[str] = None  # Hash chain for tamper evidence

    # Metrics
    total_duration_ms: int = 0
    model_calls: int = 0

    @model_validator(mode="after")
    def _compute_content_hash(self) -> "VerificationAudit":
        """Compute the content hash if not supplied, for immutability."""
        if not self.content_hash:
            self.content_hash = self.compute_hash()
        return self

    def hashable_body(self) -> str:
        """The canonical body the content hash covers.

        Everything that constitutes the decision is included: the verdict, the
        rationale, the evidence hashes, the obligation outcomes, the repository,
        and the commit. The previous implementation hashed a single concatenated
        string that excluded all of it, so the hash attested to a sentence
        rather than to the evidence behind it.
        """
        import json

        return json.dumps(
            {
                "work_package_id": self.work_package_id,
                "repo_path": self.repo_path,
                "decision": self.decision.value,
                "attempts": self.attempts,
                "passed": self.passed,
                "committed": self.committed,
                "rationale": self.rationale,
                "key_evidence": list(self.key_evidence),
                "evidence_hashes": list(self.evidence_hashes),
                "evidence_artifacts": list(self.evidence_artifacts),
                "obligations": list(self.obligations),
                "timestamp": self.timestamp.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def compute_hash(self) -> str:
        """Compute SHA256 of the decision content for immutability."""
        import hashlib

        return hashlib.sha256(self.hashable_body().encode("utf-8")).hexdigest()


class EdgeRecordLogger:
    """
    A queryable index over the verification record.

    The durable record is the EdgeRecord receipt chain (`receipt.py`), which is
    append-only, file-based and verifiable by anyone holding the bytes. This
    SQLite table is a **derived cache** over it: convenient to query, rebuilt
    from the receipts, and never the source of truth.

    It exists because the receipts are written to files and a query over a
    year of them is slow. It is not authoritative, and `verify_chain()` on it
    is a second opinion, not the verdict.
    """

    def __init__(self, db_path: str = "~/.edgerecord/ledger.sqlite"):
        from pathlib import Path
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        import sqlite3
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS verification_audits (
                id TEXT PRIMARY KEY,
                seq INTEGER,
                timestamp TEXT NOT NULL,
                work_package_id TEXT NOT NULL,
                work_package_preview TEXT,
                repo_path TEXT NOT NULL,
                decision TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                committed INTEGER NOT NULL,
                rationale TEXT,
                key_evidence TEXT,  -- JSON array
                evidence_hashes TEXT,  -- JSON array
                obligations TEXT,  -- JSON array
                content_hash TEXT NOT NULL,
                previous_audit_hash TEXT,
                total_duration_ms INTEGER,
                model_calls INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        self._migrate()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verif_audits_wp ON verification_audits(work_package_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verif_audits_timestamp "
            "ON verification_audits(timestamp)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verif_audits_hash "
            "ON verification_audits(content_hash)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verif_audits_seq ON verification_audits(seq)"
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release, in place.

        An existing ledger keeps its rows; the new columns are added with
        defaults and the `seq` column is backfilled so the chain stays
        contiguous and verifiable.
        """
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(verification_audits)")
        existing = {str(row[1]) for row in cursor.fetchall()}

        for column, ddl in (
            ("seq", "ALTER TABLE verification_audits ADD COLUMN seq INTEGER"),
            (
                "evidence_hashes",
                "ALTER TABLE verification_audits ADD COLUMN evidence_hashes TEXT",
            ),
            (
                "obligations",
                "ALTER TABLE verification_audits ADD COLUMN obligations TEXT",
            ),
        ):
            if column not in existing:
                cursor.execute(ddl)

        # Backfill a contiguous sequence for rows written before `seq` existed.
        cursor.execute(
            "SELECT id FROM verification_audits WHERE seq IS NULL ORDER BY timestamp"
        )
        rows = [row[0] for row in cursor.fetchall()]
        if rows:
            cursor.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM verification_audits WHERE seq IS NOT NULL"
            )
            next_seq = cursor.fetchone()[0]
            for row_id in rows:
                next_seq += 1
                cursor.execute(
                    "UPDATE verification_audits SET seq=? WHERE id=?", (next_seq, row_id)
                )
        self.conn.commit()

    def log_decision(self, audit: 'VerificationAudit') -> str:
        """Write an immutable audit record. Returns the audit ID.

        The predecessor is selected and the row inserted inside one immediate
        transaction, so two concurrent writers cannot pick the same predecessor
        and fork the chain. The chain is ordered by `seq`, a monotonic counter,
        not by `timestamp`, which is not unique and can move with the clock.
        """
        import json

        # Recompute from the canonical body so the stored hash always matches
        # the stored evidence.
        audit.content_hash = audit.compute_hash()

        cursor = self.conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM verification_audits"
            )
            seq = cursor.fetchone()[0]

            cursor.execute(
                "SELECT content_hash FROM verification_audits "
                "WHERE seq = ? ORDER BY seq DESC LIMIT 1",
                (seq - 1,),
            )
            row = cursor.fetchone()
            if row:
                audit.previous_audit_hash = row[0]

            cursor.execute("""
                INSERT INTO verification_audits
                (id, seq, timestamp, work_package_id, work_package_preview,
                 repo_path, decision, attempts, passed, committed, rationale,
                 key_evidence, evidence_hashes, obligations, content_hash,
                 previous_audit_hash, total_duration_ms, model_calls, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                audit.id,
                seq,
                audit.timestamp.isoformat(),
                audit.work_package_id,
                audit.work_package_preview[:500],
                audit.repo_path,
                audit.decision.value,
                audit.attempts,
                int(audit.passed),
                int(audit.committed),
                audit.rationale,
                json.dumps(audit.key_evidence, ensure_ascii=False),
                json.dumps(audit.evidence_hashes, ensure_ascii=False),
                json.dumps(audit.obligations, ensure_ascii=False),
                audit.content_hash,
                audit.previous_audit_hash,
                audit.total_duration_ms,
                audit.model_calls,
                datetime.now(UTC).isoformat(),
            ))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return audit.id

    def get_audit_chain(self, work_package_id: str) -> List[Dict[str, Any]]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM verification_audits "
            "WHERE work_package_id=? ORDER BY seq",
            (work_package_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def verify_chain(self) -> bool:
        """Verify the integrity of the audit hash chain."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, seq, content_hash, previous_audit_hash "
            "FROM verification_audits ORDER BY seq"
        )
        rows = cursor.fetchall()

        prev_hash = None
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq:
                return False
            if row["previous_audit_hash"] != prev_hash:
                return False
            prev_hash = row["content_hash"]
            expected_seq += 1
        return True

    def close(self) -> None:
        if hasattr(self, 'conn'):
            self.conn.close()


# ============================================================================
# CONFIGURATION
# ============================================================================

class VerificationConfig(BaseModel):
    """Configuration for El Verificador."""
    model_config = ConfigDict(extra="forbid")

    # Ollama / Local LLM (used to compose Correction Prompts)
    ollama_base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.8:latest"
    model_temperature: float = 0.1
    model_timeout: int = 60

    # EdgeRecord
    edgerecord_db: str = "~/.edgerecord/ledger.sqlite"

    # Content-addressed evidence store. A hash that points at bytes nobody
    # kept is a receipt, not a proof.
    evidence_root: str = "~/.el-verificador/evidence"

    # The durable record: an append-only chain of EdgeRecord receipts.
    receipt_dir: str = "~/.el-verificador/receipts"

    # Ticket Board flagging. Off by default: this component writes to
    # somebody else's board, and that is a decision for the operator, not
    # for a default. When it is on, the write adds the `needs-human` tag
    # and never changes a status.
    ticket_board_enabled: bool = False
    ticket_board_url: str = "http://127.0.0.1:7878"
    ticket_board_timeout: int = 10

    # Docker sandbox
    docker_image: str = "el-verificador/sandbox:local"
    docker_workdir: str = "/workspace"
    docker_timeout: int = 300
    docker_remove_on_teardown: bool = True
    # Resource caps for the sandbox. Untrusted code runs here, so the limits
    # are part of the isolation contract and not a convenience.
    docker_memory_limit_mb: int = 2048
    docker_cpu_limit: float = 2.0
    docker_pids_limit: int = 512
    # The sandbox has no network. A test that genuinely needs one must say so
    # explicitly, and the exception is recorded in the receipt.
    docker_network_enabled: bool = False

    # Acceptance test commands (run inside the container, in order)
    test_commands: List[str] = Field(
        default_factory=lambda: ["pytest -q", "ruff check .", "bandit -r . -q"]
    )

    # Recorded acceptance results, replayed instead of executing commands.
    # Only used when the checks cannot run (dry run, no Docker daemon).
    check_fixture: Optional[str] = None

    # Guardrails
    max_retries: int = 3
    max_retry_duration_ms: int = 600_000

    # Intent alignment
    intent_coverage_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    fail_on_todo_stubs: bool = True
    # "off" | "warn" | "enforce" — how a change outside the work package's
    # related_files is treated.
    scope_mode: str = "warn"
    # Whether a change that shares no vocabulary with the distilled goal
    # blocks a pass. The check is lexical, so a goal written in abstractions
    # ("refactor the boundaries for clarity") can fail a correct change.
    # Turn it off without a code change if that starts happening.
    goal_terms_enforced: bool = True
    # "off" | "warn" | "enforce" — how a new public symbol with no test
    # alongside it is treated.
    behavior_mode: str = "warn"

    # Git
    commit_branch: str = "main"
    commit_message_prefix: str = "feat(el-verificador):"

    # Dry-run: skip Docker/git/EdgeRecord writes for offline development.
    # Acceptance commands are NOT executed on the host in any mode.
    dry_run: bool = False

    def model_post_init(self, __context: Any) -> None:
        """Normalise paths after validation.

        pydantic v2 does not call `__post_init__`; `model_post_init` is the
        supported hook, so the previous `__post_init__` never ran and the
        `~` in `edgerecord_db` was never expanded here.
        """
        import os

        self.edgerecord_db = os.path.expanduser(self.edgerecord_db)
        if self.check_fixture:
            self.check_fixture = os.path.expanduser(self.check_fixture)


__all__ = [
    # Enums
    "VerificationDecision", "CheckKind", "CheckExecution", "ChangeType",
    "SymbolKind",
    # Core models
    "CheckResult", "ASTChange", "SemanticDiff", "IntentPackage",
    "CorrectionPrompt", "VerificationAttempt", "VerificationResult",
    # Audit
    "VerificationAudit", "EdgeRecordLogger",
    # Config
    "VerificationConfig",
]
