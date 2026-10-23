"""A sandbox timeout stops the pipeline before further execution or correction."""

import threading
from unittest import mock

import pytest

import docker
from el_verificador.docker_runner import (
    ContainerHandle,
    DockerRunner,
    SandboxAbortedError,
    SandboxUnavailableError,
)
from el_verificador.schemas import CheckExecution, CheckKind, VerificationConfig


def runner_and_client():
    runner = DockerRunner(VerificationConfig(dry_run=False))
    runner._container = ContainerHandle("synthetic-box", "test", "/workspace")
    client = mock.Mock()
    container = client.containers.get.return_value
    container.attrs = {"State": {"Running": False}}
    return runner, client, container


def test_timeout_stops_sandbox_and_never_runs_remaining_commands():
    runner, client, container = runner_and_client()
    release = threading.Event()
    completed = threading.Event()

    def slow_check(*args, **kwargs):
        release.wait(timeout=3)
        completed.set()
        return mock.Mock(exit_code=0, output=(b"late pass", b""))

    container.exec_run.side_effect = slow_check
    container.kill.side_effect = release.set
    runner.config.docker_timeout = 0
    with mock.patch("docker.from_env", return_value=client), \
            mock.patch.object(runner, "_replay_or_skip") as replay:
        try:
            with pytest.raises(SandboxAbortedError) as raised:
                runner.run_all(["slow", "second"])
            assert raised.value.termination_confirmed
            assert [c.exit_code for c in raised.value.checks] == [124, 125]
            assert raised.value.checks[1].execution == CheckExecution.SKIPPED
            container.exec_run.assert_called_once_with("slow", workdir="/workspace", demux=True)
            container.kill.assert_called_once()
            container.reload.assert_called_once()
            replay.assert_not_called()
            with pytest.raises(SandboxAbortedError):
                runner.run_check(CheckKind.LINT, "third")
            assert completed.wait(timeout=3)
            assert raised.value.checks[0].exit_code == 124
        finally:
            release.set()


@pytest.mark.parametrize("state", [{}, {"Running": True}, {"Running": 0},
                                   {"Running": None}, {"Running": "false"}, None])
def test_kill_return_without_positive_state_cannot_confirm_termination(state):
    runner, client, container = runner_and_client()
    container.attrs = {"State": state}
    with mock.patch("docker.from_env", return_value=client):
        assert not runner._terminate_sandbox()


@pytest.mark.parametrize("where", ["get", "kill", "reload"])
def test_absent_container_confirms_termination(where):
    runner, client, container = runner_and_client()
    operation = client.containers.get if where == "get" else getattr(container, where)
    operation.side_effect = docker.errors.NotFound("gone")
    with mock.patch("docker.from_env", return_value=client):
        assert runner._terminate_sandbox()


def test_daemon_failure_keeps_sandbox_identity_and_blocks_restart():
    runner, client, container = runner_and_client()
    container.reload.side_effect = docker.errors.APIError("synthetic-private-daemon-detail")
    with mock.patch("docker.from_env", return_value=client), \
            mock.patch.object(runner, "_exec_with_timeout", return_value=(124, "", "timeout", True)):
        with pytest.raises(SandboxAbortedError) as raised:
            runner.run_all(["slow"])
        assert not raised.value.termination_confirmed
        assert "synthetic-private" not in str(raised.value)
        runner.teardown()
        assert runner._container.container_id == "synthetic-box"
        with pytest.raises(SandboxUnavailableError, match="termination is unconfirmed"):
            runner.start(".")
        container.remove.assert_not_called()


def test_cleanup_rpc_has_a_bounded_wait_and_cannot_late_confirm():
    runner, client, container = runner_and_client()
    release = threading.Event()
    completed = threading.Event()
    runner._termination_timeout_s = 0

    def delayed_kill():
        release.wait(timeout=3)
        completed.set()

    container.kill.side_effect = delayed_kill
    try:
        with mock.patch("docker.from_env", return_value=client):
            confirmed = runner._terminate_sandbox()
            assert not confirmed
            release.set()
            assert completed.wait(timeout=3)
            assert not confirmed
    finally:
        release.set()


def test_exit_124_without_a_runner_timeout_keeps_ordinary_failure_semantics():
    runner, client, container = runner_and_client()
    container.exec_run.side_effect = [mock.Mock(exit_code=124, output=b"failed"),
                                      mock.Mock(exit_code=0, output=b"passed")]
    with mock.patch("docker.from_env", return_value=client):
        results = runner.run_all(["first", "second"])
    assert [c.exit_code for c in results] == [124, 0]
    container.kill.assert_not_called()


def test_abort_retains_results_before_the_timeout():
    runner, client, container = runner_and_client()
    with mock.patch("docker.from_env", return_value=client), \
            mock.patch.object(runner, "_exec_with_timeout", side_effect=[
                (0, "first passed", "", False), (124, "", "timeout", True)]):
        with pytest.raises(SandboxAbortedError) as raised:
            runner.run_all(["first", "slow", "last"])
    assert [c.exit_code for c in raised.value.checks] == [0, 124, 125]
    assert raised.value.checks[0].stdout == "first passed"


def test_only_an_explicit_fresh_start_can_reuse_confirmed_runner():
    runner, client, container = runner_and_client()
    with mock.patch("docker.from_env", return_value=client), \
            mock.patch.object(runner, "_exec_with_timeout", return_value=(124, "", "timeout", True)):
        with pytest.raises(SandboxAbortedError):
            runner.run_all(["slow"])
        runner.teardown()
        with pytest.raises(SandboxAbortedError):
            runner.run_all(["second"])
    runner.config.dry_run = True
    runner.start(".")
    assert runner.run_check(CheckKind.LINT, "fresh").execution == CheckExecution.SKIPPED
