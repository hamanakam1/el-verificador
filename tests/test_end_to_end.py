"""
End-to-end verification harness.

Every other test in this repository exercises a unit in isolation. This one
runs the **real CLI**, as a subprocess, against a **real git repository**, and
asserts on the result it prints. It exists because the tool spent four
commits shipping a `verify` command that could not complete a verification:
the Docker path crashed on a shadowed method, the CLI crashed on its own JSON
serialisation, and the dry-run path executed checks in the wrong directory.

A unit test suite was green the whole time. Only running the thing end to
end showed the problem.

The harness covers three scenarios:

- a faithful implementation passes and is committed;
- a broken implementation fails, is retried within the budget, and escalates;
- a binary file in the diff does not crash the semantic diff.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_USERS = '''\
"""User registration routes."""
from __future__ import annotations


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    raise NotImplementedError
'''

HEAD_USERS = '''\
"""User registration routes."""
from __future__ import annotations

import hashlib

from app.schemas import UserCreate, UserRecord

_REGISTRY: dict[str, UserRecord] = {}


def validate_payload(raw: dict) -> tuple[list[str], UserCreate | None]:
    """Validate an incoming registration payload."""
    errors: list[str] = []
    username = str(raw.get("username", "")).strip()
    secret = str(raw.get("password", ""))
    if len(username) < 3:
        errors.append("username must be at least 3 characters")
    if len(secret) < 8:
        errors.append("password must be at least 8 characters")
    if errors:
        return errors, None
    return [], UserCreate(username=username, password=secret)


def hash_password(secret: str) -> str:
    """Hash a plaintext secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_user(payload: UserCreate) -> UserRecord:
    """Persist a validated user."""
    record = UserRecord(username=payload.username, password_hash=hash_password(payload.password))
    _REGISTRY[payload.username] = record
    return record


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    errors, payload = validate_payload(raw)
    if errors:
        return 422, {"errors": errors}
    record = create_user(payload)
    return 201, {"username": record.username}
'''

SCHEMAS = '''\
"""Request and response schemas."""
from dataclasses import dataclass


@dataclass
class UserCreate:
    username: str
    password: str


@dataclass
class UserRecord:
    username: str
    password_hash: str
'''

TESTS = '''\
"""Acceptance tests for the users registration route."""
import unittest

from app.routes.users import create_user, hash_password, register_user, validate_payload


class TestRegistration(unittest.TestCase):
    def test_validate_payload_rejects_short_username(self):
        errors, payload = validate_payload({"username": "ab", "password": "longenough"})
        self.assertIsNone(payload)
        self.assertTrue(any("username" in error for error in errors))

    def test_hash_password_is_not_plaintext(self):
        self.assertNotEqual(hash_password("correcthorse"), "correcthorse")

    def test_register_user_creates_record(self):
        status, body = register_user({"username": "ada", "password": "correcthorse"})
        self.assertEqual(status, 201)
        self.assertEqual(body["username"], "ada")

    def test_register_user_rejects_bad_payload(self):
        status, body = register_user({"username": "x", "password": "y"})
        self.assertEqual(status, 422)
        self.assertTrue(body["errors"])


if __name__ == "__main__":
    unittest.main()
'''

WORK_PACKAGE = {
    "id": "dwp-e2e-0001",
    "true_goal": (
        "Implement register_user, create_user, validate_payload and hash_password "
        "in app/routes/users.py so the POST /users/register endpoint validates the "
        "request body and persists a new user record."
    ),
    "true_goal_summary": (
        "Implement register_user, create_user, validate_payload and hash_password"
    ),
    "related_files": ["app/routes/users.py", "app/schemas.py"],
    "estimated_tokens": 8000,
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class EndToEndVerification(unittest.TestCase):
    """Runs the real CLI against a real repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name) / "project"
        self.work.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

        self.ledger = Path(self._tmp.name) / "ledger.sqlite"
        self.config = self.work / "verificador.yaml"
        # CI builds the repository's own sandbox image and points the harness
        # at it, so the container path is exercised rather than assumed.
        self.sandbox_image = os.environ.get(
            "EL_VERIFICADOR_SANDBOX_IMAGE", "el-verificador/sandbox:ci"
        )
        _write(
            self.config,
            "ollama_base_url: \"http://127.0.0.1:11434\"\n"
            "model: \"qwen2.5:14b\"\n"
            "model_timeout: 5\n"
            f"edgerecord_db: \"{self.ledger}\"\n"
            f"docker_image: \"{self.sandbox_image}\"\n"
            "docker_timeout: 120\n"
            # Standard-library only, and `python3` rather than an absolute
            # interpreter path, so the same command works both on the host
            # and inside the sandbox image. `-t .` puts the project root on
            # the import path so the fixture's own package resolves.
            "test_commands:\n"
            "  - \"python3 -m unittest discover -s tests -t . -q\"\n"
            "  - \"python3 -m compileall -q app\"\n"
            "max_retries: 3\n"
            "intent_coverage_threshold: 0.85\n"
            "fail_on_todo_stubs: true\n"
            "commit_branch: \"main\"\n"
            "commit_message_prefix: \"feat(el-verificador):\"\n"
            "dry_run: false\n",
        )

    # ------------------------------------------------------------------ #
    # Repository construction
    # ------------------------------------------------------------------ #
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args], cwd=str(self.work), capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc

    def _build_repo(self, broken: bool = False) -> None:
        _write(self.work / "app" / "__init__.py", "")
        _write(self.work / "app" / "routes" / "__init__.py", "")
        _write(self.work / "app" / "routes" / "users.py", BASE_USERS)
        _write(self.work / "tests" / "__init__.py", "")
        _write(self.work / "tests" / "test_users.py", TESTS)
        # Keep the fixture's own hygiene out of the way, so the commit-scope
        # assertion below is about the verifier's behaviour, not about
        # bytecode.
        _write(self.work / ".gitignore", "__pycache__/\n*.py[cod]\n")

        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "e2e@el-verificador.local")
        self._git("config", "user.name", "El Verificador E2E")
        self._git("config", "commit.gpgsign", "false")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "feat(users): scaffold registration route")

        _write(self.work / "app" / "schemas.py", SCHEMAS)
        head = HEAD_USERS
        if broken:
            head = head.replace(
                'return hashlib.sha256(secret.encode("utf-8")).hexdigest()',
                "return secret",
            )
        _write(self.work / "app" / "routes" / "users.py", head)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "feat(users): implement registration route")

        _write(self.work / "work_package.json", json.dumps(WORK_PACKAGE, indent=2))

    # ------------------------------------------------------------------ #
    # Running the CLI
    # ------------------------------------------------------------------ #
    def _run_cli(self, *extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.run(
            [
                sys.executable, "-m", "el_verificador", "verify",
                "--work-package", "work_package.json",
                "--repo", ".",
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--config", "verificador.yaml",
                *extra,
            ],
            cwd=str(self.work),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def _result(self, proc: subprocess.CompletedProcess) -> dict:
        text = proc.stdout
        start = text.rfind("\n{\n")
        if start == -1 and text.lstrip().startswith("{"):
            start = text.index("{")
        self.assertNotEqual(
            start, -1, f"no JSON result in output:\n{proc.stdout}\n{proc.stderr}"
        )
        return json.loads(text[start:])

    # ------------------------------------------------------------------ #
    # Scenarios
    # ------------------------------------------------------------------ #
    def test_a_faithful_implementation_passes_and_is_committed(self):
        self._build_repo()
        proc = self._run_cli()

        self.assertEqual(
            proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        result = self._result(proc)
        self.assertEqual(result["decision"], "pass")
        self.assertTrue(result["passed"])
        self.assertTrue(result["committed"])

        checks = result["check_results"]
        self.assertTrue(checks)
        for check in checks:
            self.assertEqual(check["exit_code"], 0, f"{check['command']} failed")

        # The commit must contain the verified change set and nothing else.
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(self.work), capture_output=True, text=True,
        ).stdout.split()
        self.assertIn("app/routes/users.py", committed)
        self.assertNotIn("work_package.json", committed)

    def test_a_broken_implementation_fails_and_escalates_within_budget(self):
        self._build_repo(broken=True)
        proc = self._run_cli()

        self.assertEqual(proc.returncode, 1)
        result = self._result(proc)
        self.assertFalse(result["passed"])
        self.assertIn(result["decision"], ("fail", "escalate"))

        # The retry budget is a hard limit, and the escalation carries the
        # evidence a human needs rather than a bare count.
        self.assertLessEqual(len(result["attempts"]), 3)
        if result["decision"] == "escalate":
            self.assertTrue(result["attempts"], "escalation carried no evidence")
            self.assertIn("check_results", result["attempts"][-1])

    def test_a_binary_file_in_the_diff_does_not_crash_the_verifier(self):
        self._build_repo()
        (self.work / "logo.bin").write_bytes(bytes(range(256)) * 8)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "chore: add binary asset")

        proc = self._run_cli()
        self.assertNotIn("UnicodeDecodeError", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn(proc.returncode, (0, 1))

    def test_a_diff_with_no_intent_alignment_does_not_pass(self):
        """
        The gate must actually gate.

        The code under test is correct and its acceptance checks pass. What
        changes is the *goal*: the work package asks for capabilities that
        were never built. A verifier that only runs the tests would call this
        a pass.
        """
        self._build_repo()

        other_goal = {
            "id": "dwp-e2e-0002",
            "true_goal": (
                "Implement rate_limit_requests and throttle_window in "
                "app/routes/users.py so the POST /users/register endpoint "
                "enforces a per-client rate limit."
            ),
            "true_goal_summary": (
                "Implement rate_limit_requests and throttle_window"
            ),
            "related_files": ["app/routes/users.py"],
            "estimated_tokens": 8000,
        }
        _write(self.work / "other_package.json", json.dumps(other_goal, indent=2))

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(
            [
                sys.executable, "-m", "el_verificador", "verify",
                "--work-package", "other_package.json",
                "--repo", ".",
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--config", "verificador.yaml",
            ],
            cwd=str(self.work),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

        result = self._result(proc)
        self.assertFalse(
            result["passed"],
            "work that does not meet the goal was accepted as a pass",
        )


if __name__ == "__main__":
    unittest.main()
