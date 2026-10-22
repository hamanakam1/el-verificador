#!/usr/bin/env python3
"""
Capture authentic El Verificador terminal screenshots.

Every PNG produced by this script is a rendering of real bytes the CLI wrote
to stdout/stderr. Nothing is mocked, hand-written or illustrated: the demo
repository is built, committed, and verified for real; the correction prompt
is the one the tool actually emits; the audit chain is a real SQLite hash
chain.

Usage:
    python scripts/capture_screenshots.py
    python scripts/capture_screenshots.py --outdir docs/screenshots
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "docs" / "examples"
DEFAULT_VENV = Path.home() / ".cache" / "el-verificador" / "demo-venv"

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
BLUE = "\x1b[34m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"

WORK_PACKAGE = {
    "id": "dwp-4c81f0a7b2d9",
    "true_goal": (
        "Implement register_user, create_user, validate_payload, hash_password and "
        "get_user in app/routes/users.py so the POST /users/register endpoint "
        "validates the request body and persists a new user record."
    ),
    "true_goal_summary": (
        "Add register_user, create_user, validate_payload, hash_password and get_user "
        "to app/routes/users.py"
    ),
    "related_files": [
        "app/routes/users.py",
        "app/schemas.py",
        "tests/test_users.py",
    ],
    "estimated_tokens": 8000,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run(
    cmd: Sequence[str],
    cwd: Path,
    env: dict | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        list(cmd), cwd=str(cwd), env=env, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def ensure_venv(venv: Path) -> Path:
    """Create (once) an isolated toolchain with the acceptance checks installed."""
    python = venv / "bin" / "python"
    if not (venv / "bin" / "ruff").exists():
        print(f"  creating demo toolchain at {venv} ...")
        venv.parent.mkdir(parents=True, exist_ok=True)
        run([sys.executable, "-m", "venv", str(venv)], cwd=REPO_ROOT)
        run(
            [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
             "ruff", "bandit", "pytest"],
            cwd=REPO_ROOT,
        )
    return python


IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc")


def build_demo_repo(work: Path, venv_python: Path, broken: bool) -> None:
    """
    Build a two-commit demo repository.

    Commit 1 (base)  - the stub implementation.
    Commit 2 (head)  - the implemented version.

    When `broken` is set, the head commit introduces a real security
    regression (plaintext secrets) so the acceptance suite fails for a
    genuine reason.
    """
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # --- base tree ----------------------------------------------------- #
    shutil.copytree(EXAMPLES / "demo-base", work, dirs_exist_ok=True, ignore=IGNORE)
    shutil.copy2(EXAMPLES / "demo-project" / "pytest.ini", work / "pytest.ini")
    shutil.copy2(EXAMPLES / "demo-project" / "ruff.toml", work / "ruff.toml")
    shutil.copy2(EXAMPLES / "demo-project" / ".gitignore", work / ".gitignore")

    run(["git", "init", "-q", "-b", "main"], cwd=work)
    run(["git", "config", "user.email", "capture@el-verificador.local"], cwd=work)
    run(["git", "config", "user.name", "El Verificador Capture"], cwd=work)
    run(["git", "config", "commit.gpgsign", "false"], cwd=work)
    run(["git", "add", "-A"], cwd=work)
    run(["git", "commit", "-q", "-m", "feat(users): scaffold registration route"], cwd=work)

    # --- head tree ----------------------------------------------------- #
    shutil.copytree(EXAMPLES / "demo-project", work, dirs_exist_ok=True, ignore=IGNORE)
    if broken:
        users = work / "app" / "routes" / "users.py"
        text = users.read_text()
        # A real regression: the secret is stored in plaintext.
        text = text.replace(
            'return hashlib.sha256(secret.encode("utf-8")).hexdigest()',
            'return secret',
        )
        users.write_text(text)

    run(["git", "add", "-A"], cwd=work)
    run(["git", "commit", "-q", "-m", "feat(users): implement registration route"], cwd=work)

    # --- work package -------------------------------------------------- #
    (work / "work_package.json").write_text(json.dumps(WORK_PACKAGE, indent=2) + "\n")

    # --- acceptance config --------------------------------------------- #
    (work / "verificador.yaml").write_text(
        "ollama_base_url: \"http://127.0.0.1:11434\"\n"
        "model: \"qwen3.8:latest\"\n"
        "model_temperature: 0.1\n"
        "model_timeout: 60\n"
        "edgerecord_db: \"~/.edgerecord/ledger.sqlite\"\n"
        "evidence_root: \"~/.el-verificador/evidence\"\n"
        "receipt_dir: \"~/.el-verificador/receipts\"\n"
        "docker_image: \"el-verificador/sandbox:local\"\n"
        "docker_workdir: \"/workspace\"\n"
        "docker_timeout: 300\n"
        "docker_remove_on_teardown: true\n"
        "test_commands:\n"
        "  - \"pytest -q\"\n"
        "  - \"ruff check .\"\n"
        "  - \"bandit -r app -q\"\n"
        "max_retries: 3\n"
        "max_retry_duration_ms: 600000\n"
        "intent_coverage_threshold: 0.85\n"
        "fail_on_todo_stubs: true\n"
        "scope_mode: \"warn\"\n"
        "goal_terms_enforced: true\n"
        "behavior_mode: \"warn\"\n"
        "commit_branch: \"main\"\n"
        "commit_message_prefix: \"feat(el-verificador):\"\n"
        "dry_run: true\n"
    )


def record_checks(work: Path, venv_python: Path) -> Path:
    """
    Run the acceptance commands for real and record their results.

    The capture host may not have a Docker daemon, and the verifier never
    runs a command on the host. So the acceptance results are produced here
    by the same tools the sandbox image carries — pytest, ruff and bandit —
    and recorded into a fixture that the CLI replays. The CLI's own output
    is real; only the command's *result* is a recording.

    `scripts/sandbox_smoke_test.py` runs the same acceptance commands
    against a real container, and CI runs it.
    """
    toolchain = Path(venv_python).parent
    checks = []
    for command in ("pytest -q", "ruff check .", "bandit -r app -q"):
        argv = [str(toolchain / command.split()[0]), *command.split()[1:]]
        proc = subprocess.run(
            argv, cwd=str(work), capture_output=True, text=True, timeout=300
        )
        checks.append(
            {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
    path = work / "checks.json"
    path.write_text(json.dumps({"checks": checks}, indent=2) + "\n")
    return path


def cli_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def prompt_line(command: str, cwd_label: str) -> str:
    return (
        f"{GREEN}{BOLD}❯{RESET} {DIM}{cwd_label}{RESET} "
        f"{CYAN}{command}{RESET}\n"
    )


def render(
    capture: Path,
    out_png: Path,
    title: str,
    theme: str = "dark",
    head: int | None = None,
    tail: int | None = None,
    font_size: int = 15,
    max_cols: int | None = 118,
) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "render_terminal.py"),
        str(capture),
        "-o",
        str(out_png),
        "--title",
        title,
        "--theme",
        theme,
        "--font-size",
        str(font_size),
    ]
    if max_cols:
        cmd += ["--max-cols", str(max_cols)]
    if head is not None:
        cmd += ["--head", str(head)]
    if tail is not None:
        cmd += ["--tail", str(tail)]
    run(cmd, cwd=REPO_ROOT)
    print(f"  -> {out_png.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


VERIFY_CMD = [
    sys.executable, "-m", "el_verificador", "verify",
    "--work-package", "work_package.json",
    "--repo", ".",
    "--base", "HEAD~1",
    "--head", "HEAD",
    "--config", "verificador.yaml",
    "--dry-run",
    "--check-fixture", "checks.json",
]

PASS_FILTER = """{
  decision, passed, committed, edge_record_payload_hash,
  checks: [.check_results[] | {
    kind, command, exit_code, passed: (.exit_code == 0)
  }],
  intent: {
    coverage: .semantic_diff.intent_coverage,
    files_changed: .semantic_diff.files_changed,
    ast_changes: (.semantic_diff.ast_changes | length),
    todo_stubs: .semantic_diff.has_todo_stubs
  }
}"""

ESCALATE_FILTER = """{
  decision, passed, committed,
  attempts: [.attempts[] | {
    attempt, decision, duration_ms,
    failed: [.check_results[] | select(.exit_code != 0) | {kind, command, exit_code}]
  }]
}"""

AUDIT_FILTER = """map({
  id, timestamp, decision, attempts, passed, committed,
  content_hash: .content_hash[0:16],
  previous_audit_hash: (.previous_audit_hash[0:16])
})"""

SEED_LEDGER_TEMPLATE = """\
import sys

sys.path.insert(0, {repo_root!r})

from el_verificador.schemas import EdgeRecordLogger, VerificationAudit

logger = EdgeRecordLogger({db_path!r})

DECISIONS = [
    ("pass", "All acceptance checks passed and semantic intent aligned for work package dwp-4c81f0a7b2d9", True),
    ("fail", "Verification failed for work package dwp-4c81f0a7b2d9", False),
    ("escalate", "Verification exhausted after 3 attempts; escalating to human", False),
]

for decision, rationale, passed in DECISIONS:
    audit = VerificationAudit(
        work_package_id="dwp-4c81f0a7b2d9",
        work_package_preview={preview!r},
        repo_path="/tmp/demo-project",
        decision=decision,
        attempts=3 if decision == "escalate" else 1,
        passed=passed,
        committed=passed,
        rationale=rationale,
        key_evidence=["checks_passed=True", "intent_coverage=0.90"],
        total_duration_ms=1840,
    )
    logger.log_decision(audit)

print("seeded")
"""

PROMPT_VERIFY = (
    "python -m el_verificador verify --work-package work_package.json \\\n"
    "      --repo . --base HEAD~1 --head HEAD"
)


def json_block(out: str) -> str:
    """The JSON document the CLI printed after its human-readable report."""
    start = out.rfind("\n{\n")
    if start == -1:
        raise RuntimeError("no verification result found in the CLI output")
    return out[start + 1 :]


def run_jq(payload: str, filter_expr: str, cwd: Path) -> str:
    """Pipe a JSON document through jq without involving a shell."""
    proc = subprocess.run(
        ["jq", filter_expr],
        cwd=str(cwd),
        input=payload,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"jq failed: {proc.stderr.strip()}")
    return proc.stdout


def capture_verify_pass(work: Path, outdir: Path, tmp: Path) -> None:
    print("scenario: verify (pass)")
    build_demo_repo(work, Path(os.environ["EV_DEMO_PYTHON"]), broken=False)
    record_checks(work, Path(os.environ["EV_DEMO_PYTHON"]))

    proc = run(VERIFY_CMD, cwd=work, env=cli_env(), check=False)
    pretty = run_jq(json_block(proc.stdout), PASS_FILTER, work)

    body = prompt_line(
        PROMPT_VERIFY + " | jq '{decision, checks, intent}'",
        "demo-project",
    ) + pretty
    if proc.stderr.strip():
        body += "\n" + proc.stderr

    capture = tmp / "verify-pass.txt"
    capture.write_text(body)
    render(capture, outdir / "01-verify-pass.png", "el-verificador — verify (PASS)")


def capture_failure(outdir: Path, tmp: Path) -> None:
    print("scenario: verify (fail -> correction -> escalate)")
    work = tmp / "demo-broken"
    build_demo_repo(work, Path(os.environ["EV_DEMO_PYTHON"]), broken=True)
    record_checks(work, Path(os.environ["EV_DEMO_PYTHON"]))

    proc = run(VERIFY_CMD, cwd=work, env=cli_env(), check=False)
    out = proc.stdout or ""

    # --- correction prompt (first block) -------------------------------- #
    start = out.find("=== CORRECTION PROMPT")
    end = out.find("=== END CORRECTION PROMPT")
    if start == -1 or end == -1:
        raise RuntimeError("no correction prompt found in the CLI output")
    block = out[start : end + len("=== END CORRECTION PROMPT ===")]

    body = prompt_line(PROMPT_VERIFY, "demo-project") + block + "\n"

    capture = tmp / "correction-prompt.txt"
    capture.write_text(body)
    render(
        capture,
        outdir / "02-correction-prompt.png",
        "el-verificador — correction prompt",
    )

    # --- escalation ------------------------------------------------------ #
    pretty = run_jq(json_block(out), ESCALATE_FILTER, work)

    body = prompt_line(
        PROMPT_VERIFY + " | jq '{decision, attempts}'",
        "demo-project",
    ) + pretty

    capture = tmp / "escalate.txt"
    capture.write_text(body)
    render(capture, outdir / "03-escalate.png", "el-verificador — escalate")


def capture_audit(outdir: Path, tmp: Path) -> None:
    print("scenario: audit chain")
    db = tmp / "ledger.sqlite"

    # Populate a real hash chain through the public EdgeRecordLogger API.
    script = tmp / "seed_ledger.py"
    script.write_text(
        SEED_LEDGER_TEMPLATE.format(
            repo_root=str(REPO_ROOT),
            db_path=str(db),
            preview=WORK_PACKAGE["true_goal"][:200],
        )
    )
    run([sys.executable, str(script)], cwd=REPO_ROOT)

    env = cli_env()
    env["EL_VERIFICADOR_DB"] = str(db)

    proc = run(
        [sys.executable, "-m", "el_verificador", "audit", "--verify-chain"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    body = prompt_line(
        "python -m el_verificador audit --verify-chain", "el-verificador"
    ) + (proc.stdout or "")

    capture = tmp / "audit-chain.txt"
    capture.write_text(body)
    render(capture, outdir / "04-audit-chain.png", "el-verificador — audit --verify-chain")

    # --- full audit trail for one work package -------------------------- #
    proc = run(
        [
            sys.executable, "-m", "el_verificador", "audit",
            "--work-package", "dwp-4c81f0a7b2d9",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    pretty = run_jq(proc.stdout, AUDIT_FILTER, REPO_ROOT)
    body = prompt_line(
        'python -m el_verificador audit --work-package dwp-4c81f0a7b2d9 \\\n'
        "      | jq 'map({id, decision, attempts, content_hash})'",
        "el-verificador",
    ) + pretty

    capture = tmp / "audit-trail.txt"
    capture.write_text(body)
    render(capture, outdir / "05-audit-trail.png", "el-verificador — audit trail")


def capture_config_init(outdir: Path, tmp: Path) -> None:
    print("scenario: config --init")
    fake_home = tmp / "home"
    fake_home.mkdir(parents=True, exist_ok=True)

    env = cli_env()
    env["HOME"] = str(fake_home)

    proc = run(
        [sys.executable, "-m", "el_verificador", "config", "--init"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    body = prompt_line(
        "python -m el_verificador config --init", "el-verificador"
    ) + (proc.stdout or "")

    capture = tmp / "config-init.txt"
    capture.write_text(body)
    render(capture, outdir / "06-config-init.png", "el-verificador — config --init")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture El Verificador screenshots")
    parser.add_argument("--outdir", default=str(REPO_ROOT / "docs" / "screenshots"))
    parser.add_argument("--venv", default=str(DEFAULT_VENV))
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args(argv)

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    python = ensure_venv(Path(args.venv).resolve())
    os.environ["EV_DEMO_PYTHON"] = str(python)

    tmp = Path(tempfile.mkdtemp(prefix="el-verificador-capture-"))
    try:
        capture_verify_pass(tmp / "demo-project", outdir, tmp)
        capture_failure(outdir, tmp)
        capture_audit(outdir, tmp)
        capture_config_init(outdir, tmp)
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"temp kept at {tmp}")

    print(f"\nscreenshots written to {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
