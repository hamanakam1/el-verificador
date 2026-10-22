"""Tests for the Docker sandbox runner (dry-run mode)."""
import subprocess
import tempfile
import unittest

from el_verificador.docker_runner import ContainerHandle, DockerRunner
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
    """Non-dry-run mode must not crash when the Docker daemon is unavailable.

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

    def test_start_does_not_raise_without_daemon(self):
        runner = DockerRunner(self.config)
        handle = runner.start(self.repo)
        self.assertIsInstance(handle, ContainerHandle)
        self.assertTrue(handle.container_id)
        runner.teardown()

    def test_run_check_does_not_raise_without_daemon(self):
        runner = DockerRunner(self.config)
        runner.start(self.repo)
        result = runner.run_check(CheckKind.UNIT_TESTS, "echo hello")
        self.assertIn(result.exit_code, (0, 127))
        runner.teardown()

    def test_probe_is_callable_and_cached(self):
        runner = DockerRunner(self.config)
        first = runner._docker_available()
        self.assertIsInstance(first, bool)
        self.assertEqual(runner._docker_ok, first)
        self.assertEqual(runner._docker_available(), first)


if __name__ == "__main__":
    unittest.main()
