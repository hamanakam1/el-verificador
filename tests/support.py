"""Shared test fixtures.

Running the test suite must not write into the operator's real verification
store. Every test that builds a `VerificationConfig` points its evidence
store and receipt chain at a temporary directory, so a test run leaves
`~/.el-verificador/` exactly as it found it.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

from el_verificador.schemas import VerificationConfig


class TempStore:
    """A temporary evidence store and receipt chain."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="el-verificador-test-")
        self.root = Path(self._tmp.name)

    @property
    def evidence_root(self) -> str:
        return str(self.root / "evidence")

    @property
    def receipt_dir(self) -> str:
        return str(self.root / "receipts")

    def config(self, **overrides: Any) -> VerificationConfig:
        """A config whose stores are inside the temporary directory."""
        settings: Dict[str, Any] = {
            "evidence_root": self.evidence_root,
            "receipt_dir": self.receipt_dir,
            "edgerecord_db": str(self.root / "ledger.sqlite"),
        }
        settings.update(overrides)
        return VerificationConfig(**settings)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def isolated_config(**overrides: Any) -> VerificationConfig:
    """A config with every path inside a temporary directory."""
    store = TempStore()
    # The caller is responsible for cleanup; the directory is small and the
    # process exit removes it in any case.
    return store.config(**overrides)


__all__ = ["TempStore", "isolated_config"]
