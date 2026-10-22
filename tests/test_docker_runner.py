"""Tests for the Docker sandbox runner (dry-run mode)."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker

from el_verificador.docker_runner import (
    HOST_EXECUTION_REFUSED,
    ContainerHandle,
    DockerRunner,
    SandboxUnavailableError,
)
from el_verificador.schemas import CheckKind, VerificationConfig


class TestDockerRunnerDryRun(unittest.TestCase):
    """Host execution is opt-in. These tests ask for it explicitly."""

    def setUp(self):
        self.config = VerificationConfig(dry_run=True, allow_host_execution=True)
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_start_returns_handle(self):
        runner = DockerRunner(self.config)
        handle = runner.start(self.repo)
        self.assertIsInstance(handle, ContainerHandle)
        self.assertEqual(handle.container_id, "dry-run")

    def test_run_check_passes_locally(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "echo hello")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)
        self.assertIsInstance(result.duration_ms, int)

    def test_run_check_fails_locally(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "sh -c 'exit 3'")
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.passed)

    def test_run_check_runs_in_the_repo_directory(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "pwd")
        self.assertEqual(result.stdout.strip(), subprocess.run(
            ["pwd"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip())

    def test_run_all(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        # Use safe host commands; in production these run inside Docker.
        results = runner.run_all(commands=["echo a", "echo b", "echo c"])
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.exit_code, 0)
            self.assertIsInstance(r.kind, CheckKind)

    def test_teardown_noop(self):
        runner = DockerRunner(self.config)
        runner.teardown()  # should not raise


class TestDockerRunnerLiveMode(unittest.TestCase):
    """Non-dry-run mode must not crash.

    Regression guard: the instance attribute used to cache the daemon probe
    shared a name with the probe method itself, which shadowed the method
    and made every non-dry-run call raise
    `TypeError: 'NoneType' object is not callable`. The dry-run tests above
    could never catch it because `dry_run or ...` short-circuits.

    The daemon probe is mocked so the outcome does not depend on whether the
    host running the tests happens to have a Docker daemon.
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

    def test_start_without_daemon_uses_local_fallback(self):
        runner = DockerRunner(self.config)
        with mock.patch.object(DockerRunner, "_docker_available", return_value=False):
            handle = runner.start(self.repo)
        self.assertIsInstance(handle, ContainerHandle)
        self.assertEqual(handle.container_id, "dry-run")
        self.assertEqual(handle.workdir, str(Path(self.repo).resolve()))
        runner.teardown()

    def test_run_check_without_daemon_does_not_raise(self):
        """Without a sandbox and without the opt-in, the check is refused."""
        runner = DockerRunner(self.config)
        with mock.patch.object(DockerRunner, "_docker_available", return_value=False):
            runner.start(self.repo)
            result = runner.run_check(CheckKind.UNIT_TESTS, "echo hello")
        self.assertEqual(result.exit_code, HOST_EXECUTION_REFUSED)
        self.assertIn("allow-host-execution", result.stderr)
        self.assertNotIn("hello", result.stdout)
        runner.teardown()

    def test_run_check_without_daemon_runs_when_host_execution_is_allowed(self):
        runner = DockerRunner(
            VerificationConfig(dry_run=False, allow_host_execution=True)
        )
        with mock.patch.object(DockerRunner, "_docker_available", return_value=False):
            runner.start(self.repo)
            result = runner.run_check(CheckKind.UNIT_TESTS, "echo hello")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)
        runner.teardown()

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


class TestHostExecutionIsRefusedByDefault(unittest.TestCase):
    """The repository under test contains code the agent wrote.

    Running its acceptance commands on the host executes that code with the
    operator's credentials. That has to be a decision the operator makes, not
    a fallback the tool takes.
    """

    def setUp(self):
        self.config = VerificationConfig(dry_run=True)
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.marker = self.repo / "pwned.txt"

    def _write_a_test_that_writes_to_the_host(self):
        # A conftest.py is arbitrary code: pytest imports it before the
        # tests run, so this is enough to execute anything.
        (self.repo / "conftest.py").write_text(
            "import pathlib\n"
            "pathlib.Path(%r).write_text('executed on the host')\n" % str(self.marker)
        )

    def test_the_default_refuses_to_execute(self):
        self._write_a_test_that_writes_to_the_host()
        runner = DockerRunner(self.config)
        runner.start(str(self.repo))
        result = runner.run_check(CheckKind.UNIT_TESTS, "python -c 'pass'")
        self.assertEqual(result.exit_code, HOST_EXECUTION_REFUSED)
        self.assertFalse(result.passed)
        self.assertFalse(self.marker.exists())

    def test_the_refusal_explains_how_to_allow_it(self):
        runner = DockerRunner(self.config)
        runner.start(str(self.repo))
        result = runner.run_check(CheckKind.UNIT_TESTS, "python -c 'pass'")
        self.assertIn("not run", result.stderr)
        self.assertIn("Docker daemon", result.stderr)
        self.assertIn("--allow-host-execution", result.stderr)

    def test_the_opt_in_runs_the_command(self):
        self._write_a_test_that_writes_to_the_host()
        config = VerificationConfig(dry_run=True, allow_host_execution=True)
        runner = DockerRunner(config)
        runner.start(str(self.repo))
        result = runner.run_check(CheckKind.UNIT_TESTS, f"touch {self.marker}")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(self.marker.exists())

    def test_every_check_is_refused(self):
        runner = DockerRunner(self.config)
        runner.start(str(self.repo))
        results = runner.run_all(commands=["echo a", "echo b", "echo c"])
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result.exit_code, HOST_EXECUTION_REFUSED)
            self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
