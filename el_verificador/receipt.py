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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Sequence, Tuple

RECEIPT_TYPE = "EdgeRecordReceipt"
RECEIPT_VERSION = "v1.1.3"

# The fields the fingerprint is computed over.
_EXCLUDED_FROM_HASH = ("proof_fingerprint", "full_hash")

# The contract in factory-contracts/schemas/receipt.v1.json.
_REQUIRED_FIELDS = ("receipt_id", "kind", "created_at")
_OUTCOMES = ("success", "failed", "partial", "blocked")


class ReceiptChainError(ValueError):
    """The durable record cannot be read and verified in its entirety."""


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
    work_package_id: Optional[str] = None,
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
    if work_package_id:
        receipt["work_package_id"] = work_package_id

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

    Ordering is by a monotonic `seq` assigned at write time, not by
    `created_at`. The timestamp has second resolution, and a verifier that
    writes several receipts in the same second would otherwise be ordered by
    a random receipt id — which looks exactly like a broken chain.

    Cooperating processes and threads share a persistent local lock file.
    Writers hold an exclusive lock from predecessor lookup through atomic
    publication; readers hold a shared lock while loading a complete snapshot.
    """

    def __init__(self, directory: str = "~/.el-verificador/receipts"):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        """Lock a stable inode; never unlink it while the store is in use."""
        try:
            import fcntl
        except ImportError as exc:
            raise ReceiptChainError("receipt chain locking requires a POSIX local filesystem") from exc
        lock_file: BinaryIO
        try:
            lock_path = self.directory / ".receipt-chain.lock"
            if exclusive:
                lock_file = lock_path.open("a+b")
            else:
                try:
                    lock_file = lock_path.open("rb")
                except FileNotFoundError:
                    lock_file = lock_path.open("a+b")
        except OSError as exc:
            raise ReceiptChainError("cannot open receipt chain lock") from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            except OSError as exc:
                raise ReceiptChainError("cannot acquire receipt chain lock") from exc
            # Closing this independently opened descriptor releases the lock,
            # including on exceptions. Process exit also releases kernel locks.
            yield

    # ------------------------------------------------------------------ #
    def append(self, receipt: Dict[str, Any]) -> Path:
        """Write a receipt, chaining it to the previous one."""
        receipt_id = receipt.get("receipt_id")
        if (not isinstance(receipt_id, str) or not receipt_id
                or receipt_id in (".", "..")
                or any(char in receipt_id for char in ("/", "\\"))
                or any(ord(char) < 32 for char in receipt_id)):
            raise ReceiptChainError("receipt_id must be a nonempty safe filename component")
        problems = validate_receipt(receipt)
        if problems:
            raise ValueError("invalid receipt: " + "; ".join(problems))

        with self._locked(exclusive=True):
            return self._append_locked(receipt, receipt_id)

    def _append_locked(self, receipt: Dict[str, Any], receipt_id: str) -> Path:
        records = self._read_receipts()
        previous = records[-1] if records else None
        if previous is not None:
            receipt["previous_receipt_id"] = previous.get("receipt_id")
            receipt["previous_fingerprint"] = previous.get("proof_fingerprint")
            receipt["seq"] = int(previous.get("seq", 0)) + 1
        else:
            receipt["seq"] = 1
        proof, full = fingerprint(receipt)
        receipt["proof_fingerprint"] = proof
        receipt["full_hash"] = full

        path = self.directory / f"{receipt_id}.json"
        handle, temp_path = tempfile.mkstemp(dir=str(self.directory), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            # Publishing a complete file by hard link is atomic and refuses
            # an existing destination, including a concurrent writer's file.
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise ReceiptChainError(f"receipt already exists: {receipt_id}") from exc
        except OSError as exc:
            raise ReceiptChainError(f"could not append receipt: {receipt_id}") from exc
        finally:
            Path(temp_path).unlink(missing_ok=True)
        return path

    # ------------------------------------------------------------------ #
    def receipts(self) -> List[Dict[str, Any]]:
        """Every verified receipt, in chain order, or an explicit integrity error.

        Skipping a damaged file could expose an older success as the latest
        verdict and let new receipts fork from an incomplete history.
        """
        with self._locked(exclusive=False):
            return self._read_receipts()

    def _read_receipts(self) -> List[Dict[str, Any]]:
        """Load the chain while the caller holds its shared or exclusive lock."""
        records: List[Dict[str, Any]] = []
        try:
            paths = sorted(path for path in self.directory.iterdir() if path.suffix == ".json")
        except OSError as exc:
            raise ReceiptChainError("cannot read receipt directory") from exc
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReceiptChainError(f"unreadable receipt: {path.name}") from exc
            if not isinstance(record, dict):
                raise ReceiptChainError(f"receipt is not a JSON object: {path.name}")
            try:
                problems = validate_receipt(record)
            except (TypeError, ValueError) as exc:
                raise ReceiptChainError(f"receipt cannot be fingerprinted: {path.name}") from exc
            if problems:
                raise ReceiptChainError(f"invalid receipt fingerprint or fields: {path.name}")
            records.append(record)
        records.sort(key=_order_key)
        previous: Optional[Dict[str, Any]] = None
        for record in records:
            if previous is None:
                if record.get("previous_receipt_id") or record.get("previous_fingerprint"):
                    raise ReceiptChainError("receipt chain has no valid genesis")
            elif (record.get("previous_receipt_id") != previous.get("receipt_id")
                  or record.get("previous_fingerprint") != previous.get("proof_fingerprint")):
                raise ReceiptChainError("receipt chain links are invalid")
            previous = record
        return records

    def last(self) -> Optional[Dict[str, Any]]:
        records = self.receipts()
        return records[-1] if records else None

    def verify_chain(self) -> bool:
        """Verify the chain: every link, every fingerprint, every body."""
        try:
            self.receipts()
        except ReceiptChainError:
            return False
        return True

    def for_ticket(self, ticket: str) -> List[Dict[str, Any]]:
        return [r for r in self.receipts() if r.get("ticket") == ticket]


def _order_key(receipt: Dict[str, Any]) -> Tuple[int, int, str, str]:
    """Order by the chain's own sequence, then by time, then by id."""
    seq = receipt.get("seq")
    if isinstance(seq, int):
        return (0, seq, "", "")
    return (1, 0, str(receipt.get("created_at", "")), str(receipt.get("receipt_id", "")))


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
    "ReceiptChainError",
    "artifact_records",
    "build_receipt",
    "canonical_bytes",
    "fingerprint",
    "outcome_for_decision",
    "sort_keys",
    "validate_receipt",
]
