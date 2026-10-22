"""El Verificador CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

from .engine import ElVerificador
from .taskrivet_adapter import adapter_from_args, add_taskrivet_arguments
from .schemas import EdgeRecordLogger, VerificationConfig


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
    verify_parser.add_argument("--config", help="Path to config YAML")
    verify_parser.add_argument(
        "--dry-run", action="store_true", help="Skip Docker/git/EdgeRecord writes"
    )

    add_taskrivet_arguments(verify_parser)

    audit_parser = subparsers.add_parser(
        "audit", help="Query verification audit log"
    )
    audit_parser.add_argument(
        "--work-package", help="Work package ID to query"
    )
    audit_parser.add_argument(
        "--verify-chain", action="store_true", help="Verify audit chain integrity"
    )

    config_parser = subparsers.add_parser(
        "config", help="Manage configuration"
    )
    config_parser.add_argument(
        "--init", action="store_true", help="Create default config file"
    )

    return parser


async def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    config = VerificationConfig()
    if getattr(args, "config", None):
        with open(args.config) as f:
            for k, v in yaml.safe_load(f).items():
                if hasattr(config, k):
                    setattr(config, k, v)
    if getattr(args, "dry_run", False):
        config.dry_run = True

    if not hasattr(args, "command") or not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "verify":
            return await cmd_verify(args, config)
        if args.command == "audit":
            return await cmd_audit(args)
        if args.command == "config":
            return await cmd_config(args)
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


async def cmd_verify(args: argparse.Namespace, config: VerificationConfig) -> int:
    with open(args.work_package) as f:
        work_package = json.load(f)

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

        def taskrivet_retry(prompt):  # noqa: ARG001
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

    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0 if result.passed else 1


async def cmd_audit(args: argparse.Namespace) -> int:
    db_path = os.environ.get(
        "EL_VERIFICADOR_DB", str(Path.home() / ".edgerecord" / "ledger.sqlite")
    )
    logger = EdgeRecordLogger(db_path)
    if args.verify_chain:
        ok = logger.verify_chain()
        print(f"Audit chain valid: {ok}")
        return 0 if ok else 1
    if args.work_package:
        chain = logger.get_audit_chain(args.work_package)
        print(json.dumps(chain, indent=2, default=str))
    else:
        print(
            "Provide --work-package to query, or --verify-chain to check integrity"
        )
    return 0


async def cmd_config(args: argparse.Namespace) -> int:
    target = Path.home() / ".el-verificador" / "config.yaml"
    if args.init:
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        shutil.copy(example, target)
        print(f"Config written to {target}")
        return 0
    print("Use --init to create a default config file")
    return 0


def run() -> int:
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
