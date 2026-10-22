"""Tests for the Docker sandbox runner.

The behaviour asserted here is the safety property that matters most in this
component: **an acceptance command never runs on the host.**

The previous implementation fell back to `subprocess.run` in the repository
working directory whenever Docker was unavailable or `dry_run` was set. That
made the one component whose job is to run agent-generated code have a
supported path that executed it on the operator's machine. These tests pin the
replacement behaviour so it cannot come back.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker
from el_verificador.docker_runner import (
    ContainerHandle,
    DockerRunner,
    SandboxUnavailableError,
)
from el_verificador.schemas import CheckExecution, CheckKind, VerificationConfig


class TestDockerRunnerNeverExecutesOnTheHost(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(dry_run=True)
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_start_returns_handle(self):
        runner = DockerRunner(self.config)
        handle = runner.start(self.repo)
        self.assertIsInstance(handle, ContainerHandle)
        self.assertEqual(handle.container_id, "dry-run")
        self.assertFalse(handle.is_container)

    def test_dry_run_does_not_execute_the_command(self):
        """The command must not run, and must not report a pass."""
        marker = Path(self.repo) / "pwned"
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(
            CheckKind.UNIT_TESTS, f"touch {marker}"
        )
        self.assertFalse(marker.exists(), "the command ran on the host")
        self.assertEqual(result.execution, CheckExecution.SKIPPED)
        self.assertFalse(result.passed)
        self.assertFalse(result.executed)

    def test_dry_run_never_reports_a_pass(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "true")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(result.passed)

    def test_dry_run_explains_itself(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "true")
        self.assertIn("never executed on the host", result.stderr)

    def test_run_all_skips_every_command(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        results = runner.run_all(commands=["echo a", "echo b", "echo c"])
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result.execution, CheckExecution.SKIPPED)
            self.assertFalse(result.passed)
            self.assertIsInstance(result.kind, CheckKind)

    def test_teardown_noop(self):
        runner = DockerRunner(self.config)
        runner.teardown()  # should not raise


class TestCheckFixtureReplay(unittest.TestCase):
    """A recorded result may stand in for an execution, and says so."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

        self.fixture = Path(self._tmp.name) / "checks.json"
        self.fixture.write_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "command": "pytest -q",
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": "AssertionError: boom",
                        }
                    ]
                }
            )
        )
        self.config = VerificationConfig(
            dry_run=True, check_fixture=str(self.fixture)
        )

    def test_recorded_result_is_replayed(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "pytest -q")
        self.assertEqual(result.execution, CheckExecution.REPLAY)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.passed)

    def test_unrecorded_command_is_skipped(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.LINT, "ruff check .")
        self.assertEqual(result.execution, CheckExecution.SKIPPED)
        self.assertFalse(result.passed)


class TestDockerRunnerLiveMode(unittest.TestCase):
    """Non-dry-run mode must not crash, and must not fall back to the host.

    Regression guard: the instance attribute used to cache the daemon probe
    shared a name with the probe method itself, which shadowed the method
    and made every non-dry-run call raise
    `TypeError: 'NoneType' object is not callable`. The dry-run tests above
    could never catch it because `dry_run or ...` short-circuits.
    """

    def setUp(self):
        self.config = VerificationConfig(dry_run=False)
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_probe_is_callable_and_cached(self):
        runner = DockerRunner(self.config)
        first = runner._docker_available()
        self.assertIsInstance(first, bool)
        self.assertEqual(runner._docker_ok, first)
        self.assertEqual(runner._docker_available(), first)

    def test_missing_daemon_raises_instead_of_running_locally(self):
        """No daemon must fail loudly, never silently degrade to the host."""
        runner = DockerRunner(self.config)
        with mock.patch.object(DockerRunner, "_docker_available", return_value=False):
            with self.assertRaises(SandboxUnavailableError) as ctx:
                runner.start(self.repo)
        self.assertIn("Docker daemon", str(ctx.exception))

    def test_missing_image_raises_actionable_error(self):
        """A sandbox image that cannot be pulled must not surface as a raw 404."""
        runner = DockerRunner(self.config)

        class _FakeClient:
            class containers:  # noqa: N801
                @staticmethod
                def run(*args, **kwargs):
                    raise docker.errors.ImageNotFound("pull access denied")

        with mock.patch.object(DockerRunner, "_docker_available", return_value=True):
            with mock.patch("docker.from_env", return_value=_FakeClient()):
                with self.assertRaises(SandboxUnavailableError) as ctx:
                    runner.start(self.repo)
        self.assertIn(self.config.docker_image, str(ctx.exception))


class TestCheckKindInference(unittest.TestCase):
    """A command is classified by what it does, not by where it sits."""

    def setUp(self):
        self.config = VerificationConfig(dry_run=True)
        self.runner = DockerRunner(self.config)

    def test_kinds_are_inferred_from_the_command(self):
        self.assertEqual(
            DockerRunner._infer_kind("pytest -q", 0), CheckKind.UNIT_TESTS
        )
        self.assertEqual(
            DockerRunner._infer_kind("ruff check .", 1), CheckKind.LINT
        )
        self.assertEqual(
            DockerRunner._infer_kind("bandit -r . -q", 2), CheckKind.SECURITY_SCAN
        )

    def test_a_fourth_command_is_not_reported_as_a_unit_test(self):
        self.assertNotEqual(
            DockerRunner._infer_kind("mypy .", 3), CheckKind.UNIT_TESTS
        )


if __name__ == "__main__":
    unittest.main()
