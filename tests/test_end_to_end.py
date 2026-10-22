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

The harness runs in two modes, and both matter:

- **replay** (always available) — the acceptance results are replayed from a
  recorded fixture, so the decision path is exercised with no Docker and no
  execution of anything. This is also where the security property is pinned:
  the recorded commands would leave a marker file if they were ever run on
  the host, and the harness asserts that no marker appears.
- **sandbox** (needs Docker) — the acceptance commands really run inside the
  container built from `docker/sandbox.Dockerfile`, and the commit and the
  audit record really happen. CI runs this mode in a dedicated job.
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

#: A command that would leave a marker if it were ever executed on the host.
MARKER_COMMAND = "touch /tmp/el-verificador-e2e-marker"
MARKER_PATH = Path("/tmp/el-verificador-e2e-marker")

UNIT_COMMAND = "python3 -m unittest discover -s tests -t . -q"
COMPILE_COMMAND = "python3 -m compileall -q app"

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
    record = UserRecord(
        username=payload.username, password_hash=hash_password(payload.password)
    )
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


def _sandbox_available(image: str) -> bool:
    """True when the sandbox image can actually be used on this host."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(image)
        return True
    except Exception:
        return False


class EndToEndVerification(unittest.TestCase):
    """Runs the real CLI against a real repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name) / "project"
        self.work.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

        self.ledger = Path(self._tmp.name) / "ledger.sqlite"
        self.fixture = self.work / "checks.json"
        self.sandbox_image = os.environ.get(
            "EL_VERIFICADOR_SANDBOX_IMAGE", "el-verificador/sandbox:local"
        )
        self.sandbox_available = _sandbox_available(self.sandbox_image)
        self.addCleanup(self._remove_marker)

    @staticmethod
    def _remove_marker() -> None:
        try:
            MARKER_PATH.unlink()
        except OSError:
            pass

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
    # Configuration
    # ------------------------------------------------------------------ #
    def _write_config(self, replay: bool, passing: bool = True) -> None:
        """Write the verifier's configuration.

        In replay mode the acceptance results come from a recorded fixture and
        nothing is executed or written. In sandbox mode the commands really
        run inside the container.
        """
        if replay:
            self._write_fixture(passing=passing)
            commands = [UNIT_COMMAND, COMPILE_COMMAND, MARKER_COMMAND]
            extra = f'check_fixture: "{self.fixture}"\ndry_run: true\n'
        else:
            commands = [UNIT_COMMAND, COMPILE_COMMAND]
            extra = "dry_run: false\n"

        _write(
            self.work / "verificador.yaml",
            "ollama_base_url: \"http://127.0.0.1:11434\"\n"
            "model: \"qwen2.5:14b\"\n"
            "model_timeout: 5\n"
            f"edgerecord_db: \"{self.ledger}\"\n"
            f"docker_image: \"{self.sandbox_image}\"\n"
            "docker_timeout: 120\n"
            "test_commands:\n"
            + "".join(f'  - "{command}"\n' for command in commands)
            + extra
            + "max_retries: 3\n"
            "intent_coverage_threshold: 0.85\n"
            "fail_on_todo_stubs: true\n"
            "commit_branch: \"main\"\n"
            "commit_message_prefix: \"feat(el-verificador):\"\n",
        )

    def _write_fixture(self, passing: bool) -> None:
        """Record the acceptance results the fixture replays."""
        if passing:
            unit = {
                "command": UNIT_COMMAND,
                "exit_code": 0,
                "stdout": "Ran 4 tests in 0.01s\n\nOK\n",
                "stderr": "",
                "duration_ms": 12,
            }
        else:
            unit = {
                "command": UNIT_COMMAND,
                "exit_code": 1,
                "stdout": "",
                "stderr": (
                    "FAILED (failures=1)\n\n"
                    "    def test_hash_password_is_not_plaintext(self):\n"
                    ">       self.assertNotEqual(hash_password('correcthorse'), 'correcthorse')\n"
                    "AssertionError: 'correcthorse' == 'correcthorse'\n"
                ),
                "duration_ms": 14,
            }
        _write(
            self.fixture,
            json.dumps(
                {
                    "checks": [
                        unit,
                        {
                            "command": COMPILE_COMMAND,
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "duration_ms": 5,
                        },
                        {
                            "command": MARKER_COMMAND,
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                            "duration_ms": 1,
                        },
                    ]
                },
                indent=2,
            ),
        )

    # ------------------------------------------------------------------ #
    # Running the CLI
    # ------------------------------------------------------------------ #
    def _run_cli(
        self,
        replay: bool = True,
        work_package: str = "work_package.json",
        passing: bool = True,
    ):
        self._write_config(replay=replay, passing=passing)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.run(
            [
                sys.executable, "-m", "el_verificador", "verify",
                "--work-package", work_package,
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
    # The decision path (replay mode — always available)
    # ------------------------------------------------------------------ #
    def test_a_faithful_implementation_passes(self):
        self._build_repo()
        proc = self._run_cli(replay=True)

        self.assertEqual(
            proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        result = self._result(proc)
        self.assertEqual(result["decision"], "pass")
        self.assertTrue(result["passed"])

        checks = result["check_results"]
        self.assertTrue(checks)
        for check in checks:
            self.assertEqual(check["exit_code"], 0, f"{check['command']} failed")

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

        proc = self._run_cli(replay=True, work_package="other_package.json")
        result = self._result(proc)
        self.assertFalse(
            result["passed"],
            "work that does not meet the goal was accepted as a pass",
        )

    def test_a_broken_implementation_fails_and_escalates_within_budget(self):
        self._build_repo(broken=True)
        proc = self._run_cli(replay=True, passing=False)

        self.assertNotEqual(proc.returncode, 0)
        result = self._result(proc)
        self.assertFalse(result["passed"])
        self.assertIn(result["decision"], ("fail", "escalate", "inconclusive"))

        # The retry budget is a hard limit, and the escalation carries the
        # evidence a human needs rather than a bare count.
        self.assertLessEqual(len(result["attempts"]), 3)

    def test_the_correction_prompt_is_bounded_and_actionable(self):
        """A prompt that grows without bound is a denial-of-service on the agent."""
        self._build_repo(broken=True)
        proc = self._run_cli(replay=True, passing=False)

        start = proc.stdout.find("=== CORRECTION PROMPT")
        end = proc.stdout.find("=== END CORRECTION PROMPT")
        if start == -1 or end == -1:
            self.skipTest("this run produced no correction prompt")
        prompt = proc.stdout[start:end]
        self.assertLess(len(prompt), 64_000, "the correction prompt is unbounded")
        self.assertIn("register_user", prompt)
        self.assertIn("Failed Checks", prompt)

    def test_acceptance_commands_are_never_executed_on_the_host(self):
        """
        The one component whose job is to run agent-generated code must not
        run it on the operator's machine.

        The recorded command writes a marker file. If the verifier ever
        executed it outside a container, the marker would appear.
        """
        self._build_repo()
        proc = self._run_cli(replay=True)

        self.assertFalse(
            MARKER_PATH.exists(),
            "an acceptance command was executed on the host",
        )
        result = self._result(proc)
        for check in result["check_results"]:
            self.assertNotEqual(
                check.get("execution"),
                "container",
                "the harness expected a replayed result",
            )

    def test_a_binary_file_in_the_diff_does_not_crash_the_verifier(self):
        self._build_repo()
        (self.work / "logo.bin").write_bytes(bytes(range(256)) * 8)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "chore: add binary asset")

        proc = self._run_cli(replay=True)
        self.assertNotIn("UnicodeDecodeError", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn(proc.returncode, (0, 1))

    # ------------------------------------------------------------------ #
    # The full cycle (sandbox mode — needs Docker)
    # ------------------------------------------------------------------ #
    @unittest.skipUnless(
        _sandbox_available(
            os.environ.get("EL_VERIFICADOR_SANDBOX_IMAGE", "el-verificador/sandbox:local")
        ),
        "the verification sandbox image is not available on this host",
    )
    def test_a_faithful_implementation_is_committed_and_audited(self):
        self._build_repo()
        # Taskrivet leaves the finished change in the working tree; the
        # verifier is what turns it into a commit.
        users = self.work / "app" / "routes" / "users.py"
        users.write_text(users.read_text() + "\n# verified by el-verificador\n")

        proc = self._run_cli(replay=False)

        self.assertEqual(
            proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        result = self._result(proc)
        self.assertEqual(result["decision"], "pass")
        self.assertTrue(result["committed"])

        # The commit must contain the verified change set and nothing else.
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=str(self.work), capture_output=True, text=True,
        ).stdout.split()
        verified = set(result["semantic_diff"]["files_changed"])
        self.assertTrue(verified, "the verifier evaluated no files")
        self.assertLessEqual(
            set(committed),
            verified,
            f"the commit contains files outside the verified change set: "
            f"{sorted(set(committed) - verified)}",
        )

    @unittest.skipUnless(
        _sandbox_available(
            os.environ.get("EL_VERIFICADOR_SANDBOX_IMAGE", "el-verificador/sandbox:local")
        ),
        "the verification sandbox image is not available on this host",
    )
    def test_every_decision_is_recorded_in_the_ledger(self):
        """A decision that cannot be audited is not a verification."""
        self._build_repo()
        proc = self._run_cli(replay=False)
        self.assertIn(proc.returncode, (0, 1))

        from el_verificador.audit import verify_ledger

        report = verify_ledger(str(self.ledger))
        self.assertTrue(report.ok, report.errors)
        self.assertGreaterEqual(report.checked, 1, "no audit record was written")

    @unittest.skipUnless(
        _sandbox_available(
            os.environ.get("EL_VERIFICADOR_SANDBOX_IMAGE", "el-verificador/sandbox:local")
        ),
        "the verification sandbox image is not available on this host",
    )
    def test_two_verifications_produce_a_linked_chain(self):
        self._build_repo()
        self._run_cli(replay=False)
        self._run_cli(replay=False)

        from el_verificador.audit import verify_ledger

        report = verify_ledger(str(self.ledger))
        self.assertTrue(report.ok, report.errors)
        self.assertGreaterEqual(report.checked, 2)


if __name__ == "__main__":
    unittest.main()
