"""
EdgeRecord receipts — the durable record of a verification decision.

The verifier wrote its audit to a SQLite table it invented. The real
EdgeRecord is file-based: JSON receipts, fingerprinted by a canonical rule,
chained to the previous receipt, and verifiable by anyone who has the bytes.

This module emits receipts that conform to two contracts at once:

* `factory-contracts/schemas/receipt.v1.json` — the cross-component receipt
  contract (`receipt_id`, `kind`, `created_at`, `ticket`, `actor`, `outcome`,
  `artifacts`, `notes`).
* the EdgeRecord receipt shape and its canonical hashing rule, so the receipts
  can be checked with the same tooling that checks the operator's own cycle
  journal.

The canonical rule, from `RUN_ERA.md`:

    1. Drop `proof_fingerprint` and `full_hash`.
    2. Sort all object keys lexicographically, recursively.
    3. Serialise with compact separators and non-ASCII left as literal UTF-8.
    4. UTF-8 encode, SHA-256, hex.
    5. `proof_fingerprint` is the first 16 hex characters; `full_hash` is all 64.

Python's `json.dumps` escapes non-ASCII by default, which silently produces a
different fingerprint from the reference implementation. `ensure_ascii=False`
is not optional here.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

RECEIPT_TYPE = "EdgeRecordReceipt"
RECEIPT_VERSION = "v1.1.3"

# The fields the fingerprint is computed over.
_EXCLUDED_FROM_HASH = ("proof_fingerprint", "full_hash")

# The contract in factory-contracts/schemas/receipt.v1.json.
_REQUIRED_FIELDS = ("receipt_id", "kind", "created_at")
_OUTCOMES = ("success", "failed", "partial", "blocked")


def sort_keys(obj: Any) -> Any:
    """Recursively sort object keys, exactly as the reference tool does."""
    if isinstance(obj, dict):
        return {k: sort_keys(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [sort_keys(item) for item in obj]
    return obj


def canonical_bytes(body: Dict[str, Any]) -> bytes:
    """The canonical serialisation a fingerprint is computed over."""
    return json.dumps(
        sort_keys(body), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def fingerprint(receipt: Dict[str, Any]) -> Tuple[str, str]:
    """The `proof_fingerprint` and `full_hash` of a receipt body."""
    body = {
        key: value
        for key, value in receipt.items()
        if key not in _EXCLUDED_FROM_HASH
    }
    full = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return full[:16], full


def build_receipt(
    receipt_id: str,
    kind: str,
    outcome: str,
    created_at: Optional[str] = None,
    ticket: Optional[str] = None,
    actor: str = "el-verificador",
    artifacts: Optional[List[Dict[str, Any]]] = None,
    notes: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    detected_changes: Optional[List[str]] = None,
    summary: str = "",
    previous_receipt_id: Optional[str] = None,
    previous_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a receipt body, with its fingerprint fields filled in.

    The `proof_fingerprint` and `full_hash` are derived, never typed by hand.
    """
    if outcome not in _OUTCOMES:
        raise ValueError(
            f"outcome must be one of {_OUTCOMES}; got {outcome!r}"
        )

    receipt: Dict[str, Any] = {
        # factory-contracts receipt.v1.json
        "receipt_id": receipt_id,
        "kind": kind,
        "created_at": created_at or datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "actor": actor,
        "outcome": outcome,
        "artifacts": artifacts or [],
        "notes": notes,
        # EdgeRecord receipt shape
        "type": RECEIPT_TYPE,
        "version": RECEIPT_VERSION,
        "before_state": before_state or {},
        "after_state": after_state or {},
        "detected_changes": detected_changes or [],
        "summary": summary,
        "persisted_at": created_at
        or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "persisted_paths": [],
        "previous_receipt_id": previous_receipt_id,
        "previous_fingerprint": previous_fingerprint,
    }
    if ticket:
        receipt["ticket"] = ticket

    proof, full = fingerprint(receipt)
    receipt["proof_fingerprint"] = proof
    receipt["full_hash"] = full
    return receipt


def validate_receipt(receipt: Dict[str, Any]) -> List[str]:
    """Check a receipt against the contract. Returns the problems found."""
    problems: List[str] = []
    for field in _REQUIRED_FIELDS:
        if not receipt.get(field):
            problems.append(f"missing required field: {field}")
    outcome = receipt.get("outcome")
    if outcome and outcome not in _OUTCOMES:
        problems.append(f"outcome {outcome!r} is not one of {_OUTCOMES}")
    proof, full = fingerprint(receipt)
    if receipt.get("proof_fingerprint") != proof:
        problems.append("proof_fingerprint does not match the canonical body")
    if receipt.get("full_hash") != full:
        problems.append("full_hash does not match the canonical body")
    return problems


class ReceiptChain:
    """An append-only chain of receipts, stored as JSON files.

    Each receipt names its predecessor, so the chain can be walked in either
    direction and a removed or rewritten receipt is detectable.
    """

    def __init__(self, directory: str = "~/.el-verificador/receipts"):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def append(self, receipt: Dict[str, Any]) -> Path:
        """Write a receipt, chaining it to the previous one."""
        problems = validate_receipt(receipt)
        if problems:
            raise ValueError("invalid receipt: " + "; ".join(problems))

        previous = self.last()
        if previous is not None:
            receipt["previous_receipt_id"] = previous.get("receipt_id")
            receipt["previous_fingerprint"] = previous.get("proof_fingerprint")
            proof, full = fingerprint(receipt)
            receipt["proof_fingerprint"] = proof
            receipt["full_hash"] = full

        path = self.directory / f"{receipt['receipt_id']}.json"
        handle, temp_path = tempfile.mkstemp(dir=str(self.directory), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            os.replace(temp_path, path)
        except BaseException:
            Path(temp_path).unlink(missing_ok=True)
            raise
        return path

    # ------------------------------------------------------------------ #
    def receipts(self) -> List[Dict[str, Any]]:
        """Every receipt, in chain order."""
        records: List[Dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        records.sort(key=lambda r: (r.get("created_at", ""), r.get("receipt_id", "")))
        return records

    def last(self) -> Optional[Dict[str, Any]]:
        records = self.receipts()
        return records[-1] if records else None

    def verify_chain(self) -> bool:
        """Verify the chain: every link, every fingerprint, every body."""
        records = self.receipts()
        previous: Optional[Dict[str, Any]] = None
        for record in records:
            if validate_receipt(record):
                return False
            if previous is None:
                if record.get("previous_receipt_id"):
                    return False
            else:
                if record.get("previous_receipt_id") != previous.get("receipt_id"):
                    return False
                if record.get("previous_fingerprint") != previous.get(
                    "proof_fingerprint"
                ):
                    return False
            previous = record
        return True

    def for_ticket(self, ticket: str) -> List[Dict[str, Any]]:
        return [r for r in self.receipts() if r.get("ticket") == ticket]


def outcome_for_decision(decision: str) -> str:
    """Map a verification decision onto the receipt outcome vocabulary."""
    return {
        "pass": "success",
        "fail": "failed",
        "escalate": "blocked",
        "inconclusive": "partial",
    }.get(decision, "partial")


def artifact_records(artifacts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The receipt's artifact list, in the shape EdgeRecord uses."""
    records: List[Dict[str, Any]] = []
    for artifact in artifacts:
        path = artifact.get("path", "")
        record = {
            "path": path,
            "type": artifact.get("kind", "unknown"),
            "bytes": artifact.get("bytes", 0),
        }
        if artifact.get("sha256"):
            record["sha256"] = artifact["sha256"]
        records.append(record)
    return records


__all__ = [
    "ReceiptChain",
    "artifact_records",
    "build_receipt",
    "canonical_bytes",
    "fingerprint",
    "outcome_for_decision",
    "sort_keys",
    "validate_receipt",
]
