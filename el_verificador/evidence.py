"""
The evidence store — content-addressed, immutable, verifiable.

"Every decision cites evidence" is only true if the evidence still exists.
A hash that points at bytes nobody kept is a receipt, not a proof.

Artifacts are stored under their own SHA-256, so the same bytes always land
in the same place and a re-run of the same check produces the same artifact
rather than a second copy. Nothing is ever overwritten: the store is
append-only by construction, because the path *is* the hash.

    ~/.el-verificador/evidence/
      └── ab/
        └── ab12cd34.../
          └── <kind>.<ext>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .schemas import SemanticDiff


class EvidenceIntegrityError(ValueError):
    """Stored evidence does not match its content-addressed identity."""


class EvidenceKind(str, Enum):
    """What an artifact is."""
    CHECK_STDOUT = "check_stdout"
    CHECK_STDERR = "check_stderr"
    CHECK_RESULT = "check_result"
    JUNIT_XML = "junit_xml"
    DIFF_PATCH = "diff_patch"
    AST_SUMMARY = "ast_summary"
    SANDBOX_META = "sandbox_meta"
    OBLIGATION_TRACE = "obligation_trace"
    DECISION = "decision"

    @property
    def suffix(self) -> str:
        return {
            EvidenceKind.CHECK_STDOUT: ".stdout",
            EvidenceKind.CHECK_STDERR: ".stderr",
            EvidenceKind.CHECK_RESULT: ".json",
            EvidenceKind.JUNIT_XML: ".xml",
            EvidenceKind.DIFF_PATCH: ".patch",
            EvidenceKind.AST_SUMMARY: ".json",
            EvidenceKind.SANDBOX_META: ".json",
            EvidenceKind.OBLIGATION_TRACE: ".json",
            EvidenceKind.DECISION: ".json",
        }[self]


class EvidenceArtifact(BaseModel):
    """One stored artifact, addressed by the hash of its bytes."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="sha256 of the artifact's bytes")
    kind: EvidenceKind
    path: str
    bytes: int
    sha256: str
    produced_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    produced_by: str = "el-verificador"
    label: str = ""

    def verify(self) -> bool:
        """Re-hash the stored bytes and compare them with the record."""
        try:
            data = Path(self.path).read_bytes()
        except OSError:
            return False
        return (self.id == self.sha256
                and len(data) == self.bytes
                and hashlib.sha256(data).hexdigest() == self.sha256)


class EvidenceStore:
    """A content-addressed store for verification evidence."""

    def __init__(self, root: str = "~/.el-verificador/evidence"):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def write(
        self,
        kind: EvidenceKind,
        data: bytes,
        produced_by: str = "el-verificador",
        label: str = "",
    ) -> EvidenceArtifact:
        """Store bytes and return the artifact record.

        The write is atomic: the bytes are written to a temporary file and
        moved into place, so a reader never sees a partial artifact.
        """
        digest = hashlib.sha256(data).hexdigest()
        directory = self.root / digest[:2] / digest
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{kind.value}{kind.suffix}"

        if target.exists():
            if target.read_bytes() != data:
                raise EvidenceIntegrityError(f"stored evidence does not match digest: {digest}")
        else:
            handle, temp_path = tempfile.mkstemp(dir=str(directory))
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.replace(temp_path, target)
            except BaseException:
                Path(temp_path).unlink(missing_ok=True)
                raise

        return EvidenceArtifact(
            id=digest,
            kind=kind,
            path=str(target),
            bytes=len(data),
            sha256=digest,
            produced_by=produced_by,
            label=label,
        )

    def write_text(
        self,
        kind: EvidenceKind,
        text: str,
        produced_by: str = "el-verificador",
        label: str = "",
    ) -> EvidenceArtifact:
        return self.write(
            kind, text.encode("utf-8"), produced_by=produced_by, label=label
        )

    def write_json(
        self,
        kind: EvidenceKind,
        payload: Any,
        produced_by: str = "el-verificador",
        label: str = "",
    ) -> EvidenceArtifact:
        return self.write_text(
            kind,
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            produced_by=produced_by,
            label=label,
        )

    # ------------------------------------------------------------------ #
    def get(self, digest: str) -> Optional[EvidenceArtifact]:
        """The artifact for a digest, if the store still holds it."""
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("evidence digest must be a lowercase SHA-256 hex string")
        directory = self.root / digest[:2] / digest
        if not directory.is_dir():
            return None
        for child in sorted(directory.iterdir()):
            if child.is_file():
                data = child.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise EvidenceIntegrityError(f"stored evidence does not match digest: {digest}")
                return EvidenceArtifact(
                    id=digest,
                    kind=_kind_from_suffix(child.name),
                    path=str(child),
                    bytes=len(data),
                    sha256=digest,
                )
        return None

    def verify(self, artifact: EvidenceArtifact) -> bool:
        return artifact.verify()

    def verify_all(self, artifacts: List[EvidenceArtifact]) -> List[str]:
        """The digests of any artifacts that no longer match their record."""
        return [a.sha256 for a in artifacts if not self.verify(a)]


def _kind_from_suffix(name: str) -> EvidenceKind:
    stem = name.split(".")[0]
    for kind in EvidenceKind:
        if kind.value == stem:
            return kind
    return EvidenceKind.CHECK_RESULT


def summarise_diff(diff: SemanticDiff) -> Dict[str, Any]:
    """A compact, JSON-serialisable view of a diff."""
    return {
        "repo_path": diff.repo_path,
        "base_commit": diff.base_commit,
        "head_commit": diff.head_commit,
        "files_changed": list(diff.files_changed),
        "deleted_files": list(diff.deleted_files),
        "added_lines": diff.added_lines,
        "removed_lines": diff.removed_lines,
        "intent_coverage": diff.intent_coverage,
        "has_todo_stubs": diff.has_todo_stubs,
        "stub_symbols": list(diff.stub_symbols),
        "weakened_tests": list(diff.weakened_tests),
        "ast_changes": [
            {
                "change_type": change.change_type.value,
                "symbol_kind": change.symbol_kind.value,
                "name": change.name,
                "file_path": change.file_path,
                "line_no": change.line_no,
            }
            for change in diff.ast_changes
        ],
    }


__all__ = [
    "EvidenceArtifact",
    "EvidenceKind",
    "EvidenceIntegrityError",
    "EvidenceStore",
    "summarise_diff",
]
