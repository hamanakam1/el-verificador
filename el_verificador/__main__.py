"""El Verificador CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Coroutine, Dict, Optional

import yaml

from .engine import ElVerificador
from .receipt import ReceiptChain
from .schemas import (
    CorrectionPrompt,
    EdgeRecordLogger,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)
from .taskrivet_adapter import adapter_from_args, add_taskrivet_arguments

DEFAULT_CONFIG_PATH = Path.home() / ".el-verificador" / "config.yaml"

# Exit codes. A dry run that never executed the checks did not reach a
# verdict, and that is not the same thing as a failure.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="el-verificador",
        description="El Verificador - autonomous gatekeeper for the Taskrivet pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="Verify a DistilledWorkPackage against a repository"
    )
    verify_parser.add_argument(
        "--work-package", "-w", required=True, help="Path to DistilledWorkPackage JSON"
    )
    verify_parser.add_argument("--repo", required=True, help="Path to the repository")
    verify_parser.add_argument("--base", default="HEAD~1", help="Base commit")
    verify_parser.add_argument("--head", default="HEAD", help="Head commit")
    verify_parser.add_argument(
        "--config", help="Path to config YAML (overrides ~/.el-verificador/config.yaml)"
    )
    verify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write anything and do not execute acceptance commands",
    )
    verify_parser.add_argument(
        "--check-fixture",
        help="Recorded acceptance results to replay instead of executing commands",
    )

    add_taskrivet_arguments(verify_parser)

    audit_parser = subparsers.add_parser(
        "audit", help="Query verification audit log"
    )
    audit_parser.add_argument(
        "--work-package", help="Work package ID to query"
    )
    audit_parser.add_argument(
        "--verify-chain",
        action="store_true",
        help="Verify the receipt chain and the ledger cache",
    )
    audit_parser.add_argument(
        "--receipts", action="store_true", help="List the durable receipt chain"
    )

    config_parser = subparsers.add_parser(
        "config", help="Manage configuration"
    )
    config_parser.add_argument(
        "--init", action="store_true", help="Create default config file"
    )
    config_parser.add_argument(
        "--show", action="store_true", help="Print the effective configuration"
    )

    return parser


def load_config(explicit_path: Optional[str] = None) -> VerificationConfig:
    """Load configuration from the default location, then any explicit file.

    The default location used to be written by `config --init` and never read,
    so every documented setting was inert. The file is now validated through
    the pydantic model, which means an unknown key is an error rather than a
    silently ignored line.
    """
    merged: Dict[str, Any] = {}
    sources = []
    if DEFAULT_CONFIG_PATH.exists():
        sources.append(DEFAULT_CONFIG_PATH)
    if explicit_path:
        explicit = Path(explicit_path).expanduser()
        if not explicit.exists():
            raise FileNotFoundError(f"config file not found: {explicit}")
        sources.append(explicit)

    for source in sources:
        raw = yaml.safe_load(source.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"config file must contain a mapping: {source}")
        merged.update(raw)

    return VerificationConfig.model_validate(merged)


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    try:
        if args.command == "verify":
            return run_sync(cmd_verify(args))
        if args.command == "audit":
            return run_sync(cmd_audit(args))
        if args.command == "config":
            return run_sync(cmd_config(args))
        parser.print_help()
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_sync(coro: Coroutine[Any, Any, int]) -> int:
    """Run an async command from the synchronous CLI entry point."""
    return int(asyncio.run(coro))


async def cmd_verify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "dry_run", False):
        config.dry_run = True
    if getattr(args, "check_fixture", None):
        config.check_fixture = args.check_fixture

    work_package = json.loads(Path(args.work_package).read_text())
    verifier = ElVerificador(config)

    adapter = adapter_from_args(args)
    if adapter is not None:
        # Record the verification in Taskrivet's own ledger. The task is
        # seeded from the work package's acceptance contract, and every
        # Correction Prompt is handed back as task context so Taskrivet can
        # pick the work up again.
        adapter.ensure_task_from_intent(work_package)
        adapter.start_session()
        taskrivet_retry = adapter.retry_callback()
    else:

        def taskrivet_retry(prompt: CorrectionPrompt) -> None:  # noqa: ARG001
            # No Taskrivet ledger configured: report the prompt and let the
            # operator decide what to do next.
            print("\n=== CORRECTION PROMPT (hand back to Taskrivet) ===\n")
            print(prompt.render())
            print("=== END CORRECTION PROMPT ===\n")

    result = await verifier.verify(
        work_package,
        args.repo,
        args.base,
        args.head,
        taskrivet_retry=taskrivet_retry,
    )

    if adapter is not None:
        decision = getattr(result.decision, "value", result.decision)
        if result.passed:
            adapter.apply_pass(
                evidence=(
                    "Verification passed for "
                    f"{work_package.get('id', 'work package')}"
                )
            )
        elif decision == "escalate":
            # The retry budget is exhausted. Record the final failure so
            # Taskrivet blocks the task for human intervention instead of
            # leaving an abandoned session behind.
            adapter.apply_escalation(
                summary=(
                    f"Verification exhausted after "
                    f"{len(getattr(result, 'attempts', []) or [])} attempts; "
                    "escalating to human"
                )
            )

    _report(result)
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))

    if result.decision == VerificationDecision.PASS:
        return EXIT_PASS
    if result.decision == VerificationDecision.INCONCLUSIVE:
        return EXIT_INCONCLUSIVE
    return EXIT_FAIL


def _report(result: VerificationResult) -> None:
    """A human-readable summary before the machine-readable JSON."""
    print(f"\nDecision: {result.decision.value}")
    if result.obligations:
        print("Obligations:")
        for obligation in result.obligations:
            marker = {
                "satisfied": "ok  ",
                "violated": "FAIL",
                "unproven": "????",
                "not_applicable": "--  ",
            }.get(obligation.get("status", ""), "    ")
            print(f"  [{marker}] {obligation.get('kind')}: {obligation.get('statement')}")
            detail = obligation.get("detail", "")
            if detail:
                print(f"          {detail}")
    if result.evidence_hashes:
        print(f"Evidence hashes: {len(result.evidence_hashes)}")
        for digest in result.evidence_hashes:
            print(f"  {digest}")
    if result.committed:
        print(f"Committed: {result.edge_record_payload_hash}")
    print()


async def cmd_audit(args: argparse.Namespace) -> int:
    config = load_config()
    db_path = os.environ.get("EL_VERIFICADOR_DB", config.edgerecord_db)

    if args.receipts:
        chain = ReceiptChain(config.receipt_dir)
        for receipt in chain.receipts():
            print(
                f"{receipt.get('receipt_id')}  {receipt.get('outcome', ''):8} "
                f"{receipt.get('proof_fingerprint', '')}  "
                f"{receipt.get('summary', '')[:70]}"
            )
        return 0

    if args.verify_chain:
        # The receipt chain is the durable record. The SQLite ledger is a
        # query cache over it, and is checked as a second opinion.
        receipt_chain = ReceiptChain(config.receipt_dir)
        receipts_ok = receipt_chain.verify_chain()
        logger = EdgeRecordLogger(db_path)
        ledger_ok = logger.verify_chain()
        logger.close()
        print(f"Receipt chain valid: {receipts_ok}")
        print(f"Ledger cache chain valid: {ledger_ok}")
        return 0 if receipts_ok and ledger_ok else 1

    if args.work_package:
        logger = EdgeRecordLogger(db_path)
        records = logger.get_audit_chain(args.work_package)
        logger.close()
        print(json.dumps(records, indent=2, default=str))
    else:
        print(
            "Provide --work-package to query, --verify-chain to check integrity, "
            "or --receipts to list the durable receipt chain"
        )
    return 0


async def cmd_config(args: argparse.Namespace) -> int:
    if args.init:
        target = DEFAULT_CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        shutil.copy(example, target)
        print(f"Config written to {target}")
        return 0
    if args.show:
        print(json.dumps(load_config().model_dump(mode="json"), indent=2))
        return 0
    print("Use --init to create a default config file, or --show to print it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
