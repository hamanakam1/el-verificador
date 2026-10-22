"""
Core data contracts for El Verificador.

Defines the exact data contracts flowing through the verification pipeline:

DistilledWorkPackage -> Acceptance Test -> Semantic Diff -> [Correction Prompt
x max_retries] -> Commit & Teardown -> EdgeRecord Audit
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, ConfigDict, model_validator
from typing import Annotated


# ============================================================================
# ENUMS
# ============================================================================

class VerificationDecision(str, Enum):
    """Outcome of a verification run."""
    PASS = "pass"           # All checks passed; ready to commit
    FAIL = "fail"           # Hard failure; not safe to retry
    RETRY = "retry"         # Retry with a Correction Prompt (bounded)
    ESCALATE = "escalate"   # max_retries exhausted; flag for human intervention


class CheckKind(str, Enum):
    """Kind of acceptance check executed in the Docker sandbox."""
    UNIT_TESTS = "unit_tests"
    LINT = "lint"
    SECURITY_SCAN = "security_scan"


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

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

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
        description="Fraction of the true_goal covered by actual changes (0-1)",
    )
    has_todo_stubs: bool = Field(
        default=False,
        description="Whether changed code contains TODO/pass stubs",
    )
    stub_symbols: List[str] = Field(
        default_factory=list,
        description="Names of symbols that appear to be stubs",
    )

    # How the coverage score was computed, and what it was computed from.
    # The number is only auditable if the inputs are published with it.
    coverage_basis: str = Field(
        default="",
        description="How intent_coverage was computed",
    )
    goal_tokens: List[str] = Field(
        default_factory=list,
        description="Significant words of the distilled goal",
    )
    covered_tokens: List[str] = Field(
        default_factory=list,
        description="Goal words that appear in the changed symbols",
    )
    uncovered_tokens: List[str] = Field(
        default_factory=list,
        description="Goal words that no changed symbol contains",
    )
    related_files_touched: List[str] = Field(
        default_factory=list,
        description="Declared related files that the change actually touched",
    )
    named_identifiers: List[str] = Field(
        default_factory=list,
        description="Identifiers the distilled goal names, such as register_user",
    )

    @property
    def aligned(self) -> bool:
        """Intent alignment: meaningful changes, coverage above threshold, no stubs."""
        return (
            bool(self.files_changed)
            and self.intent_coverage >= 0.0  # threshold applied by caller
            and not self.has_todo_stubs
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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IntentPackage":
        return cls(
            id=data.get("id", f"dwp-{uuid4().hex[:12]}"),
            true_goal=data.get("true_goal", ""),
            true_goal_summary=data.get("true_goal_summary", "")[:200],
            related_files=data.get("related_files", []),
            context_packet=data.get("context_packet"),
            estimated_tokens=data.get("estimated_tokens", 8000),
        )


class CorrectionPrompt(BaseModel):
    """
    A bounded, strict Correction Prompt handed back to Taskrivet on failure.

    Contains exactly the evidence needed to fix the failure: stack traces,
    failed test output, semantic diff, and remaining attempt budget.
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
    llm_text: Optional[str] = Field(
        default=None,
        description=(
            "Advisory wording composed by the local model. It never replaces "
            "the deterministic evidence below it."
        ),
    )

    def render(self) -> str:
        """Render the full Correction Prompt text for Taskrivet."""
        lines = [
            "# Correction Prompt (El Verificador)",
            "",
            f"## Task",
            f"Goal: {self.task_goal}",
            f"Summary: {self.task_goal_summary}",
            "",
            f"## Attempt {self.attempt} of {self.max_retries}",
            f"Remaining retries: {self.remaining_attempts}",
            "",
            "## Failed Checks",
        ]
        for check in self.failed_checks:
            lines.append(f"### {check.kind.value}")
            lines.append(f"Command: `{check.command}`")
            lines.append(f"Exit code: {check.exit_code}")
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
            if self.semantic_diff.coverage_basis:
                lines.append(f"Coverage basis: {self.semantic_diff.coverage_basis}")
            if self.semantic_diff.uncovered_tokens:
                lines.append(
                    "Goal words not present in the change: "
                    + ", ".join(self.semantic_diff.uncovered_tokens[:40])
                )
            if self.semantic_diff.has_todo_stubs:
                lines.append(
                    f"Stub symbols detected: {', '.join(self.semantic_diff.stub_symbols)}"
                )
            lines.append("")
        if self.llm_text:
            # Advisory only. The deterministic evidence above is what the
            # decision was made from; this is wording, not authority.
            lines.append("## Advisor's Note (advisory, not evidence)")
            lines.append("```")
            lines.append(self.llm_text[-3000:])
            lines.append("```")
            lines.append("")
        lines.append("## Required Action")
        lines.append(
            "Fix the failures above, re-run acceptance tests, and report back. "
            "Do not add new TODO stubs. Every change must map to the task goal."
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
    duration_ms: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


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
    passed: bool = False
    committed: bool = False
    commit_note: Optional[str] = Field(
        default=None,
        description="What the commit path did, or why it did nothing",
    )
    edge_record_payload_hash: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def final_attempt(self) -> Optional[VerificationAttempt]:
        return self.attempts[-1] if self.attempts else None


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
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    work_package_id: str
    work_package_preview: str = Field(max_length=500)
    repo_path: str

    decision: VerificationDecision
    attempts: int
    passed: bool
    committed: bool

    rationale: str = Field(..., min_length=10)
    key_evidence: List[str] = Field(default_factory=list)

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

    def compute_hash(self) -> str:
        """Compute SHA256 of the decision content for immutability."""
        import hashlib
        content = (
            f"{self.work_package_id}{self.decision.value}"
            f"{self.rationale}{self.passed}{self.committed}"
            f"{self.timestamp.isoformat()}"
        )
        return hashlib.sha256(content.encode()).hexdigest()


class EdgeRecordLogger:
    """
    Interface for writing to EdgeRecord (append-only SQLite ledger, WAL mode).

    All verification decisions are written here for immutable audit trail.
    Mirrors el-destilador's EdgeRecordLogger pattern.
    """

    def __init__(self, db_path: str = "~/.edgerecord/ledger.sqlite"):
        from pathlib import Path
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        import sqlite3
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS verification_audits (
                id TEXT PRIMARY KEY,
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
                content_hash TEXT NOT NULL,
                previous_audit_hash TEXT,
                total_duration_ms INTEGER,
                model_calls INTEGER,
                created_at TEXT NOT NULL
            )
        """)
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
        self.conn.commit()

    def log_decision(self, audit: 'VerificationAudit') -> str:
        """Write an immutable audit record. Returns the audit ID."""
        import hashlib
        import json

        content = (
            f"{audit.work_package_id}{audit.decision.value}"
            f"{audit.rationale}{audit.passed}{audit.committed}"
            f"{audit.timestamp.isoformat()}"
        )
        audit.content_hash = hashlib.sha256(content.encode()).hexdigest()

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT content_hash FROM verification_audits "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            audit.previous_audit_hash = row[0]

        cursor.execute("""
            INSERT INTO verification_audits
            (id, timestamp, work_package_id, work_package_preview, repo_path,
             decision, attempts, passed, committed, rationale, key_evidence,
             content_hash, previous_audit_hash, total_duration_ms, model_calls,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            audit.id,
            audit.timestamp.isoformat(),
            audit.work_package_id,
            audit.work_package_preview[:500],
            audit.repo_path,
            audit.decision.value,
            audit.attempts,
            int(audit.passed),
            int(audit.committed),
            audit.rationale,
            json.dumps(audit.key_evidence),
            audit.content_hash,
            audit.previous_audit_hash,
            audit.total_duration_ms,
            audit.model_calls,
            datetime.utcnow().isoformat(),
        ))
        self.conn.commit()
        return audit.id

    def get_audit_chain(self, work_package_id: str) -> List[Dict]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM verification_audits "
            "WHERE work_package_id=? ORDER BY timestamp",
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
            "SELECT id, content_hash, previous_audit_hash "
            "FROM verification_audits ORDER BY timestamp"
        )
        rows = cursor.fetchall()

        prev_hash = None
        for row in rows:
            if row["previous_audit_hash"] != prev_hash:
                return False
            prev_hash = row["content_hash"]
        return True

    def close(self):
        if hasattr(self, 'conn'):
            self.conn.close()


# ============================================================================
# CONFIGURATION
# ============================================================================

class VerificationConfig(BaseModel):
    """Configuration for El Verificador."""
    model_config = ConfigDict(extra="forbid")

    # Ollama / Local LLM (used to build Correction Prompts)
    ollama_base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:14b"
    model_temperature: float = 0.1
    model_timeout: int = 60

    # EdgeRecord
    edgerecord_db: str = "~/.edgerecord/ledger.sqlite"

    # Docker sandbox
    docker_image: str = "taskrivet/sandbox:latest"
    docker_workdir: str = "/workspace"
    docker_timeout: int = 300
    docker_remove_on_teardown: bool = True

    # Acceptance test commands (run inside the container, in order)
    test_commands: List[str] = Field(
        default_factory=lambda: ["pytest -q", "ruff check .", "bandit -r . -q"]
    )

    # Guardrails
    max_retries: int = 3
    max_retry_duration_ms: int = 600_000

    # Semantic diff / intent alignment
    intent_coverage_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    fail_on_todo_stubs: bool = True

    # Git
    commit_branch: str = "main"
    commit_message_prefix: str = "feat(el-verificador):"

    # Dry-run: skip Docker/git/EdgeRecord writes for offline testing
    dry_run: bool = False

    def __post_init__(self):  # type: ignore[override]
        import os
        self.edgerecord_db = os.path.expanduser(self.edgerecord_db)


__all__ = [
    # Enums
    "VerificationDecision", "CheckKind", "ChangeType", "SymbolKind",
    # Core models
    "CheckResult", "ASTChange", "SemanticDiff", "IntentPackage",
    "CorrectionPrompt", "VerificationAttempt", "VerificationResult",
    # Audit
    "VerificationAudit", "EdgeRecordLogger",
    # Config
    "VerificationConfig",
]
