#!/usr/bin/env python3
"""
Sandbox smoke test — the acceptance checks, for real, inside the container.

This is the proof that the verification path works end to end with a real
Docker daemon: a synthetic repository is built, the sandbox image is
provisioned, the acceptance commands run inside it, the obligations are
resolved, and the verdict is asserted.

It is the test that the dry-run path cannot be, and it is the test that the
missing `taskrivet/sandbox:latest` image made impossible. Run it in CI, where
a Docker daemon is available:

    python scripts/sandbox_smoke_test.py

It fails loudly if the image is missing, because a missing image is a
missing sandbox.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from el_verificador.engine import ElVerificador  # noqa: E402
from el_verificador.schemas import (  # noqa: E402
    VerificationConfig,
    VerificationDecision,
)

BASE_MODULE = '''"""A tiny module for the sandbox smoke test."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return 0
'''

HEAD_MODULE = '''"""A tiny module for the sandbox smoke test."""


def add(a: int, b: int) -> int:
    """Return the sum of two integers."""
    return a + b
'''

TEST_MODULE = """from demo import add


def test_add():
    assert add(2, 3) == 5
"""

WORK_PACKAGE = {
    "id": "dwp-sandbox-smoke",
    "true_goal": "Implement the add function that returns the sum of two integers",
    "true_goal_summary": "add returns the sum of two integers",
    "related_files": ["demo.py"],
    "acceptance_commands": ["pytest -q"],
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _build_repo(root: Path) -> tuple:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "smoke@el-verificador.invalid")
    _git(repo, "config", "user.name", "El Verificador Smoke")

    (repo / "demo.py").write_text(BASE_MODULE)
    (repo / "test_demo.py").write_text(TEST_MODULE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "demo.py").write_text(HEAD_MODULE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        repo, base, head = _build_repo(root)

        config = VerificationConfig(
            dry_run=False,
            commit_branch=_git(repo, "branch", "--show-current"),
        )
        verifier = ElVerificador(config)
        result = await verifier.verify(WORK_PACKAGE, str(repo), base, head)

    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))

    if result.decision != VerificationDecision.PASS:
        print(
            f"\nFAIL: expected a pass, got {result.decision.value}",
            file=sys.stderr,
        )
        for obligation in result.obligations:
            print(
                f"  [{obligation['status']}] {obligation['kind']}: "
                f"{obligation['detail']}",
                file=sys.stderr,
            )
        return 1

    if not result.committed:
        print("\nFAIL: the verification passed but nothing was committed", file=sys.stderr)
        return 1

    print("\nOK: the acceptance checks ran inside the sandbox and the verdict passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
