"""
Tests for the Taskrivet adapter.

These tests drive a real Taskrivet ledger in a temporary directory, so the
adapter is exercised against Taskrivet's actual lifecycle rules rather than a
mock. The Taskrivet package is imported from the local checkout when present.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from el_verificador.schemas import (
    CheckKind,
    CheckResult,
    CorrectionPrompt,
    IntentPackage,
)
from el_verificador.taskrivet_adapter import (
    TaskrivetAdapter,
    TaskrivetError,
    adapter_from_args,
    add_taskrivet_arguments,
)

TASKRIVET_REPO = Path.home() / "Projects" / "taskrivet"


def _taskrivet_available() -> bool:
    """
    True when a Taskrivet checkout can be driven by the adapter.

    The adapter shells out to `python -m taskrivet` with PYTHONPATH pointing at
    the checkout, so the package does not need to be importable in-process.
    """
    return (TASKRIVET_REPO / "taskrivet" / "__init__.py").exists()


class _FakeAdapter(TaskrivetAdapter):
    """Adapter that records calls instead of shelling out to Taskrivet."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []
        self._session_counter = 0

    def _run(self, args, check=True):
        self.calls.append(list(args))
        if args and args[0] == "start":
            self._session_counter += 1
            return {"session_id": f"session-{self._session_counter}"}
        return {}


class TestTaskrivetAdapterCalls(unittest.TestCase):
    """The adapter must issue the exact Taskrivet lifecycle commands."""

    def setUp(self):
        self.adapter = _FakeAdapter(db_path="/tmp/unused.sqlite", task_id="T-1")

    def test_ensure_task_uses_task_add(self):
        self.adapter.ensure_task("Do the thing", "It works", scope=["repo/x"])
        call = self.adapter.calls[0]
        self.assertEqual(call[0], "task-add")
        self.assertIn("T-1", call)
        self.assertIn("--objective", call)
        self.assertIn("--acceptance", call)
        self.assertIn("--scope", call)
        self.assertIn("repo/x", call)

    def test_start_session_uses_start(self):
        session = self.adapter.start_session()
        self.assertEqual(session, "session-1")
        self.assertEqual(self.adapter.calls[0][0], "start")
        self.assertIn("--owner", self.adapter.calls[0])

    def test_apply_correction_records_failure_context_and_retry(self):
        self.adapter.start_session()
        self.adapter.calls.clear()
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Implement the login route",
            task_goal_summary="login route",
            failed_checks=[
                CheckResult(
                    kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=1
                )
            ],
            violations=[
                {
                    "kind": "no_stub",
                    "statement": "The route must not be a stub",
                    "detail": "pass body found",
                }
            ],
        )
        session = self.adapter.apply_correction(prompt)
        self.assertEqual(session, "session-2")
        verbs = [c[0] for c in self.adapter.calls]
        self.assertEqual(verbs, ["fail", "context-add", "retry", "start"])

    def test_apply_correction_opens_session_when_none_active(self):
        """A failure must never be dropped for lack of an owning session."""
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Implement the login route",
            task_goal_summary="login route",
        )
        self.adapter.apply_correction(prompt)
        verbs = [c[0] for c in self.adapter.calls]
        self.assertEqual(verbs, ["start", "fail", "context-add", "retry", "start"])

    def test_apply_correction_failure_summary_is_bounded(self):
        self.adapter.start_session()
        self.adapter.calls.clear()
        prompt = CorrectionPrompt(
            attempt=2,
            max_retries=3,
            remaining_attempts=1,
            task_goal="Implement the login route",
            task_goal_summary="login route",
            failed_checks=[],
        )
        self.adapter.apply_correction(prompt)
        fail_call = self.adapter.calls[0]
        summary = fail_call[fail_call.index("--summary") + 1]
        self.assertLessEqual(len(summary), 400)
        self.assertIn("Attempt 2/3", summary)

    def test_apply_pass_runs_checkpoint_finish_accept(self):
        self.adapter.session_id = "session-9"
        self.adapter.apply_pass("commit abc123; all checks passed")
        verbs = [c[0] for c in self.adapter.calls]
        self.assertEqual(verbs, ["checkpoint", "finish", "accept"])

    def test_describe_failure_includes_obligations(self):
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Implement the login route",
            task_goal_summary="login route",
            violations=[{"kind": "no_stub", "statement": "not a stub"}],
        )
        described = self.adapter.describe_failure(prompt)
        self.assertIn("no_stub", described)

    def test_retry_callback_returns_callable(self):
        callback = self.adapter.retry_callback()
        self.assertTrue(callable(callback))
        self.adapter.start_session()
        self.adapter.calls.clear()
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Implement the login route",
            task_goal_summary="login route",
        )
        callback(prompt)
        verbs = [c[0] for c in self.adapter.calls]
        self.assertEqual(verbs, ["fail", "context-add", "retry", "start"])

    def test_ensure_task_from_intent_uses_acceptance_contract(self):
        intent = IntentPackage.from_dict({
            "id": "dwp-1",
            "true_goal": "Implement the login API route",
            "true_goal_summary": "login route",
            "acceptance_criteria": "POST /login returns a token",
            "scope": ["repo/api"],
        })
        self.adapter.ensure_task_from_intent(intent)
        call = self.adapter.calls[0]
        self.assertEqual(call[call.index("--acceptance") + 1],
                         "POST /login returns a token")
        self.assertIn("repo/api", call)

    def test_missing_task_id_raises(self):
        adapter = _FakeAdapter(db_path="/tmp/unused.sqlite", task_id="")
        with self.assertRaises(TaskrivetError):
            adapter.ensure_task("obj", "acc")


class TestTaskrivetCLIArguments(unittest.TestCase):
    """The CLI flags must build an adapter, and stay inert when unused."""

    def test_arguments_registered(self):
        import argparse

        from el_verificador.taskrivet_adapter import add_taskrivet_arguments

        parser = argparse.ArgumentParser()
        add_taskrivet_arguments(parser)
        args = parser.parse_args(
            ["--taskrivet-db", "/tmp/ledger.sqlite", "--taskrivet-task", "T-1"]
        )
        self.assertEqual(args.taskrivet_db, "/tmp/ledger.sqlite")
        self.assertEqual(args.taskrivet_task, "T-1")
        self.assertEqual(args.taskrivet_owner, "el-verificador")

    def test_adapter_from_args_requires_db_and_task(self):
        import argparse

        from el_verificador.taskrivet_adapter import add_taskrivet_arguments

        parser = argparse.ArgumentParser()
        add_taskrivet_arguments(parser)

        self.assertIsNone(adapter_from_args(parser.parse_args([])))
        self.assertIsNone(
            adapter_from_args(parser.parse_args(["--taskrivet-db", "/tmp/x.sqlite"]))
        )
        self.assertIsNone(
            adapter_from_args(parser.parse_args(["--taskrivet-task", "T-1"]))
        )

    def test_adapter_from_args_builds_adapter(self):
        import argparse

        from el_verificador.taskrivet_adapter import add_taskrivet_arguments

        parser = argparse.ArgumentParser()
        add_taskrivet_arguments(parser)
        args = parser.parse_args(
            [
                "--taskrivet-db",
                "/tmp/ledger.sqlite",
                "--taskrivet-task",
                "T-1",
                "--taskrivet-scope",
                "repo/a",
                "--taskrivet-scope",
                "repo/b",
            ]
        )
        adapter = adapter_from_args(args)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.task_id, "T-1")
        self.assertEqual(adapter.scope, ["repo/a", "repo/b"])
        self.assertTrue(adapter.db_path.endswith("ledger.sqlite"))


@unittest.skipUnless(
    _taskrivet_available(), "taskrivet package not importable for integration tests"
)
class TestTaskrivetAdapterIntegration(unittest.TestCase):
    """Drive a real Taskrivet ledger end to end."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="taskrivet-adapter-")
        self.db_path = os.path.join(self.tmpdir, "ledger.sqlite")
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(TASKRIVET_REPO) + os.pathsep + self.env.get(
            "PYTHONPATH", ""
        )
        self.adapter = TaskrivetAdapter(
            db_path=self.db_path,
            task_id="verify-demo",
            owner="el-verificador",
            scope=["repo/demo"],
            token_budget=8000,
            python_executable=sys.executable,
        )
        self.adapter.env = self.env

    def test_full_lifecycle(self):
        adapter = self.adapter
        adapter.env = self.env
        created = adapter.ensure_task(
            objective="Implement the login route",
            acceptance="POST /login returns a token",
        )
        self.assertTrue(created["created"])

        session = adapter.start_session()
        self.assertTrue(session)

        # A failure closes the session and blocks the task.
        adapter.record_failure("verification_failed", "Attempt 1/3: unit_tests")
        adapter.attach_context("fix the stub", "el-verificador:correction-prompt")
        adapter.request_retry("retry after stub fix")

        # The task is ready again; a new session can start.
        new_session = adapter.start_session()
        self.assertNotEqual(new_session, session)

        # A pass closes out the task.
        adapter.record_checkpoint(
            summary="Verification passed",
            evidence="commit abc123; all checks passed",
            next_step="Operator review",
            tokens=120,
        )
        adapter.record_finish(evidence="commit abc123; all checks passed")
        adapter.accept("El Verificador: checks passed")

        status = self._ledger_status()
        self.assertEqual(status["tasks"][0]["status"], "accepted")
        self.assertEqual(status["metrics"]["accepted"], 1)

    def _ledger_status(self):
        proc = subprocess.run(
            [
                sys.executable, "-m", "taskrivet",
                "--db", self.db_path, "status",
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)


if __name__ == "__main__":
    unittest.main()
