"""Tests for the Docker sandbox runner (dry-run mode)."""
import subprocess
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
from el_verificador.schemas import CheckKind, VerificationConfig


class TestDockerRunnerDryRun(unittest.TestCase):
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
        runner = DockerRunner(self.config)
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


if __name__ == "__main__":
    unittest.main()
