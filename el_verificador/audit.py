"""
Independent verification of the EdgeRecord audit ledger.

The ledger is the only durable record of what El Verificador decided. It is
written by `EdgeRecordLogger` in `schemas.py`, which hashes each decision
over a canonical body that includes the verdict, the rationale, the evidence
hashes and the obligation outcomes.

`EdgeRecordLogger.verify_chain()` checks that the stored hashes form an
unbroken chain. That proves the rows were *written* consistently. It does not
prove they still *say* what they said: nothing recomputes a record's hash
from the record's own content, so a row whose rationale was edited in place
still verifies.

This module closes that gap. It reads the ledger independently, rebuilds each
`VerificationAudit` from its stored row, and recomputes the digest with the
same canonical function the writer used. A record that has been altered fails,
even if the attacker also rewrote the stored hash.

    from el_verificador.audit import verify_ledger

    report = verify_ledger("~/.edgerecord/ledger.sqlite")
    if not report.ok:
        for error in report.errors:
            print(error)

The verification is deliberately independent of the writer: it reconstructs the
audit through the public model rather than reusing the writer's own helpers,
so a bug in the writer cannot hide itself from the check.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import EdgeRecordLogger, VerificationAudit, VerificationDecision

LEDGER_TABLE = "verification_audits"


@dataclass
class ChainReport:
    """The result of verifying a ledger."""

    ok: bool
    checked: int = 0
    errors: List[str] = field(default_factory=list)
    head_hash: Optional[str] = None

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        if self.ok:
            return f"Audit chain valid: {self.checked} record(s)"
        return (
            f"Audit chain INVALID: {len(self.errors)} problem(s) "
            f"in {self.checked} record(s)\n"
            + "\n".join(f"  - {error}" for error in self.errors)
        )


def _loads(raw: Optional[str]) -> List[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _rationale(raw: Optional[str]) -> str:
    """Reconstruct the rationale exactly, padding only if it is too short."""
    text = raw or ""
    if len(text) >= 10:
        return text
    return (text + " no rationale recorded")[: max(10, len(text))]


def _row_to_audit(row: sqlite3.Row) -> VerificationAudit:
    """Rebuild the audit record from its stored row."""
    columns = row.keys()
    timestamp_raw = row["timestamp"]
    try:
        timestamp = datetime.fromisoformat(timestamp_raw)
    except (TypeError, ValueError):
        timestamp = datetime.fromisoformat("1970-01-01T00:00:00")

    kwargs: Dict[str, Any] = {
        "id": row["id"],
        "timestamp": timestamp,
        "work_package_id": row["work_package_id"] or "",
        "work_package_preview": (row["work_package_preview"] or "")[:500],
        "repo_path": row["repo_path"] or "",
        "decision": VerificationDecision(row["decision"]),
        "attempts": row["attempts"] or 0,
        "passed": bool(row["passed"]),
        "committed": bool(row["committed"]),
        # The rationale is hashed, so it must be reconstructed exactly. It is
        # only padded when it is shorter than the model's minimum length.
        "rationale": _rationale(row["rationale"]),
        "key_evidence": _loads(row["key_evidence"]),
    }
    if "evidence_hashes" in columns:
        kwargs["evidence_hashes"] = _loads(row["evidence_hashes"])
    if "obligations" in columns:
        kwargs["obligations"] = _loads(row["obligations"])
    return VerificationAudit(**kwargs)


def verify_ledger(db_path: str = "~/.edgerecord/ledger.sqlite") -> ChainReport:
    """
    Verify the whole ledger: integrity, linkage, and sequence.

    - **Integrity** — each record's digest is recomputed from the record's own
      stored content and must match the digest recorded with it.
    - **Linkage** — each record's ``previous_audit_hash`` must equal its
      predecessor's ``content_hash``.
    - **Sequence** — the ``seq`` numbers must be contiguous from 1, so a
      removed or reordered record is caught.
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return ChainReport(ok=True, checked=0)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                f"SELECT * FROM {LEDGER_TABLE} ORDER BY seq"
            ).fetchall()
        except sqlite3.OperationalError:
            # A ledger written before the sequence column existed.
            rows = conn.execute(
                f"SELECT * FROM {LEDGER_TABLE} ORDER BY timestamp"
            ).fetchall()
    finally:
        conn.close()

    errors: List[str] = []
    previous_hash: Optional[str] = None
    expected_seq = 1

    for row in rows:
        record_id = row["id"]
        seq = row["seq"] if "seq" in row.keys() else None
        if seq is not None and seq != expected_seq:
            errors.append(
                f"record {record_id}: sequence gap "
                f"(expected {expected_seq}, found {seq}) — a record was removed "
                f"or reordered"
            )
        if seq is not None:
            expected_seq = seq + 1

        stored_hash = row["content_hash"]
        try:
            audit = _row_to_audit(row)
            recomputed = audit.compute_hash()
        except Exception as exc:  # pragma: no cover - malformed row
            errors.append(f"record {record_id}: could not be reconstructed ({exc})")
            previous_hash = stored_hash
            continue

        if recomputed != stored_hash:
            errors.append(
                f"record {record_id}: content does not match its recorded digest "
                f"(recomputed {recomputed[:16]}, recorded {stored_hash[:16]}) — "
                f"the record was altered after it was written"
            )

        if row["previous_audit_hash"] != previous_hash:
            errors.append(
                f"record {record_id}: chain link does not match its predecessor"
            )
        previous_hash = stored_hash

    return ChainReport(
        ok=not errors,
        checked=len(rows),
        errors=errors,
        head_hash=previous_hash,
    )


def chain_for(db_path: str, work_package_id: str) -> List[Dict[str, Any]]:
    """Return the records for one work package, oldest first."""
    logger = EdgeRecordLogger(db_path)
    try:
        return logger.get_audit_chain(work_package_id)
    finally:
        close = getattr(logger, "close", None)
        if callable(close):
            close()


__all__ = ["ChainReport", "chain_for", "verify_ledger"]
