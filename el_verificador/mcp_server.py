"""
MCP surface for El Verificador.

The point of exposing the verifier as a tool is that agents stop
reimplementing checks. Taskrivet wrote the code; if it can also write the
check, the check proves nothing. A separate process, with its own context,
that answers "is this actually done?" is what makes the gate a gate.

Tools:

* `verify_work_package` — run one verification and return the verdict, the
  resolved obligations and the evidence hashes.
* `verification_status` — the last recorded verdict for a work package.
* `verification_evidence` — the evidence artifacts behind a receipt.
* `verify_receipt_chain` — check the durable record's integrity.

The transport is stdio, and the tools run against the operator's configured
data directories. Nothing here reaches the network, and the local model is
never asked to decide anything.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .engine import ElVerificador
from .receipt import ReceiptChain
from .schemas import VerificationConfig

# The tools are also usable without the MCP package, so the logic lives in
# this class and the transport is a thin wrapper.
TOOL_NAMES = (
    "verify_work_package",
    "verification_status",
    "verification_evidence",
    "verify_receipt_chain",
)


class VerificationTools:
    """The verifier, as callable tools."""

    def __init__(self, config: Optional[VerificationConfig] = None):
        self.config = config or VerificationConfig()

    # ------------------------------------------------------------------ #
    async def verify_work_package(
        self,
        work_package: Dict[str, Any],
        repo_path: str,
        base_commit: str = "HEAD~1",
        head_commit: str = "HEAD",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Verify a work package against a repository.

        Runs the acceptance commands inside the sandbox, resolves the intent
        into obligations, and returns the verdict with the evidence it rests
        on. A `pass` means every applicable obligation was satisfied and
        every acceptance command exited zero; an `inconclusive` means a
        command did not run, so no verdict was reached.
        """
        config = self.config.model_copy()
        if dry_run:
            config.dry_run = True

        verifier = ElVerificador(config)
        result = await verifier.verify(
            work_package, repo_path, base_commit, head_commit
        )
        return {
            "decision": result.decision.value,
            "passed": result.passed,
            "committed": result.committed,
            "work_package_id": result.work_package_id,
            "obligations": result.obligations,
            "blocking_obligations": result.blocking_obligations,
            "evidence_hashes": result.evidence_hashes,
            "evidence_artifacts": result.evidence_artifacts,
            "receipt_id": result.receipt_id,
            "receipt_fingerprint": result.receipt_fingerprint,
            "attempts": len(result.attempts),
        }

    # ------------------------------------------------------------------ #
    def verification_status(self, work_package_id: str) -> Dict[str, Any]:
        """The last recorded verdict for a work package."""
        chain = ReceiptChain(self.config.receipt_dir)
        matches = [
            receipt
            for receipt in chain.receipts()
            if receipt.get("work_package_id") == work_package_id
        ]
        if not matches:
            return {
                "work_package_id": work_package_id,
                "found": False,
                "detail": "no verification has been recorded for this work package",
            }
        latest = matches[-1]
        return {
            "work_package_id": work_package_id,
            "found": True,
            "outcome": latest.get("outcome"),
            "receipt_id": latest.get("receipt_id"),
            "proof_fingerprint": latest.get("proof_fingerprint"),
            "created_at": latest.get("created_at"),
            "summary": latest.get("summary"),
            "notes": latest.get("notes"),
            "artifacts": latest.get("artifacts", []),
            "history": len(matches),
        }

    def verification_evidence(self, receipt_id: str) -> Dict[str, Any]:
        """The evidence artifacts behind a receipt, with their hashes."""
        chain = ReceiptChain(self.config.receipt_dir)
        for receipt in chain.receipts():
            if receipt.get("receipt_id") != receipt_id:
                continue
            artifacts = receipt.get("artifacts", [])
            return {
                "receipt_id": receipt_id,
                "found": True,
                "proof_fingerprint": receipt.get("proof_fingerprint"),
                "artifacts": artifacts,
                "count": len(artifacts),
            }
        return {
            "receipt_id": receipt_id,
            "found": False,
            "detail": "no receipt with that id",
        }

    def verify_receipt_chain(self) -> Dict[str, Any]:
        """Check the durable record's integrity."""
        chain = ReceiptChain(self.config.receipt_dir)
        receipts = chain.receipts()
        return {
            "valid": chain.verify_chain(),
            "receipts": len(receipts),
            "directory": str(chain.directory),
        }


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

def build_server(tools: Optional[VerificationTools] = None) -> Any:
    """Build the MCP server. Requires the `mcp` package."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "the MCP server needs the 'mcp' package: pip install 'el-verificador[mcp]'"
        ) from exc

    toolset = tools or VerificationTools()
    server = MCPServer(
        name="el-verificador",
        instructions=(
            "Independent verification for agent-written code. Ask this server "
            "whether a work package was actually implemented, rather than "
            "checking it yourself: the point is that the checker is not the "
            "author."
        ),
    )

    @server.tool(
        name="verify_work_package",
        description=(
            "Verify a distilled work package against a repository. Runs the "
            "acceptance commands in the sandbox, resolves the intent into "
            "obligations, and returns the verdict with the evidence it rests "
            "on."
        ),
    )
    async def _verify_work_package(
        work_package: Dict[str, Any],
        repo_path: str,
        base_commit: str = "HEAD~1",
        head_commit: str = "HEAD",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        return await toolset.verify_work_package(
            work_package, repo_path, base_commit, head_commit, dry_run
        )

    @server.tool(
        name="verification_status",
        description=(
            "The last recorded verdict for a work package, with the receipt "
            "that carries it."
        ),
    )
    def _verification_status(work_package_id: str) -> Dict[str, Any]:
        return toolset.verification_status(work_package_id)

    @server.tool(
        name="verification_evidence",
        description=(
            "The evidence artifacts behind a receipt, with the content hash "
            "of each one."
        ),
    )
    def _verification_evidence(receipt_id: str) -> Dict[str, Any]:
        return toolset.verification_evidence(receipt_id)

    @server.tool(
        name="verify_receipt_chain",
        description=(
            "Check the integrity of the verification record: every receipt's "
            "fingerprint and every link in the chain."
        ),
    )
    def _verify_receipt_chain() -> Dict[str, Any]:
        return toolset.verify_receipt_chain()

    return server


def main() -> int:
    """Serve the tools over stdio."""
    server = build_server()
    asyncio.run(server.run_stdio_async())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TOOL_NAMES", "VerificationTools", "build_server", "main"]
