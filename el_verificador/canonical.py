"""
Canonical hashing for the EdgeRecord audit trail.

The audit record is the only thing that outlives a verification run, so the
hash that protects it has to cover the evidence it claims to protect. A hash
over a sentence is an attestation about a sentence, not about the decision.

The rules are the ones EdgeRecord already uses elsewhere:

- the content is serialised as JSON with recursively sorted keys, compact
  separators, and ``ensure_ascii=False``;
- the digest is SHA-256 over the UTF-8 bytes of that serialisation.

One definition, used by the model, by the writer, and by the verifier. A
second definition kept in sync by hand is a second thing to get wrong.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(payload: Any) -> str:
    """The canonical serialisation of a payload."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_digest(payload: Any) -> str:
    """SHA-256 over the canonical serialisation of a payload."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def decision_content(
    *,
    audit_id: str,
    timestamp: str,
    work_package_id: str,
    work_package_preview: str,
    repo_path: str,
    decision: str,
    attempts: int,
    passed: Any,
    committed: Any,
    rationale: str,
    key_evidence: Any,
    total_duration_ms: int,
    model_calls: int,
) -> dict[str, Any]:
    """The decision, as a canonical mapping.

    Everything the record says about the decision is here. If a field is not
    in this mapping, it is not covered by the hash, and it must not be stored
    as though it were.
    """
    if isinstance(key_evidence, str):
        try:
            key_evidence = json.loads(key_evidence)
        except (TypeError, ValueError):
            key_evidence = [key_evidence]
    return {
        "id": audit_id,
        "attempts": int(attempts),
        "committed": bool(committed),
        "decision": str(decision),
        "key_evidence": list(key_evidence or []),
        "model_calls": int(model_calls),
        "passed": bool(passed),
        "rationale": rationale,
        "repo_path": repo_path,
        "timestamp": timestamp,
        "total_duration_ms": int(total_duration_ms),
        "work_package_id": work_package_id,
        "work_package_preview": work_package_preview,
    }


def digest_of(content: Mapping[str, Any]) -> str:
    """The digest of a canonical decision mapping."""
    return content_digest(dict(content))
