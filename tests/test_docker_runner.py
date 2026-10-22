"""Tests for the Docker sandbox runner (dry-run mode)."""
import subprocess
import unittest

from el_verificador.docker_runner import DockerRunner, ContainerHandle
from el_verificador.schemas import CheckKind, VerificationConfig


class TestDockerRunnerDryRun(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(dry_run=True)

    def test_start_returns_handle(self):
        runner = DockerRunner(self.config)
        handle = runner.start("/tmp/repo")
        self.assertIsInstance(handle, ContainerHandle)
        self.assertEqual(handle.container_id, "dry-run")

    def test_run_check_passes_locally(self):
        runner = DockerRunner(self.config)
        runner.start("/tmp/repo")
        result = runner.run_check(CheckKind.UNIT_TESTS, "echo hello")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)
        self.assertIsInstance(result.duration_ms, int)

    def test_run_check_fails_locally(self):
        runner = DockerRunner(self.config)
        runner.start("/tmp/repo")
        result = runner.run_check(CheckKind.UNIT_TESTS, "sh -c 'exit 3'")
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.passed)

    def test_run_all(self):
        runner = DockerRunner(self.config)
        runner.start("/tmp/repo")
        # Use safe host commands; in production these run inside Docker.
        results = runner.run_all(commands=["echo a", "echo b", "echo c"])
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.exit_code, 0)
            self.assertIsInstance(r.kind, CheckKind)

    def test_teardown_noop(self):
        runner = DockerRunner(self.config)
        runner.teardown()  # should not raise


if __name__ == "__main__":
    unittest.main()
