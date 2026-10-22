"""
Docker sandbox runner for El Verificador.

Runs the CI/CD pipeline (unit tests, linters, security scans) inside an
isolated Docker container. El Verificador never trusts Taskrivet's
self-reported status — it only cares if the container exits with `0` and every
check passes.

**No acceptance command ever runs on the host.**

The previous implementation fell back to `subprocess.run` in the repository
working directory whenever Docker was unavailable or `dry_run` was set. That
made the one component whose job is to run *agent-generated, potentially
hostile* code have a supported path that executed it directly on the operator's
machine, with the operator's credentials, in the operator's real working tree.
A generated `conftest.py` or `sitecustomize.py` is code execution.

There are now exactly two outcomes for a check:

* it ran **inside the container** (`CheckExecution.CONTAINER`), or
* it did not run at all (`CheckExecution.SKIPPED`), or it was **replayed** from
  a recorded fixture (`CheckExecution.REPLAY`).

A skipped check can never pass, so a dry run can never manufacture a PASS.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schemas import CheckExecution, CheckKind, CheckResult, VerificationConfig


class SandboxUnavailableError(RuntimeError):
    """Raised when the Docker sandbox cannot be provisioned.

    Carries an actionable message instead of a raw Docker SDK traceback.
    The most common cause is a `docker_image` that no local or registry
    source can provide.
    """


@dataclass
class ContainerHandle:
    """Opaque handle to a running sandbox container."""
    container_id: str
    image: str
    workdir: str
    started_at: float = field(default_factory=time.time)
    is_container: bool = True


class DockerRunner:
    """
    Executes acceptance checks inside an isolated Docker container.

    When the container cannot be provisioned the runner fails loudly. It never
    degrades to executing the command on the host.
    """

    def __init__(self, config: VerificationConfig):
        self.config = config
        self._container: Optional[ContainerHandle] = None
        # NOTE: the cache attribute must not share a name with the
        # `_docker_available()` method below, or the instance attribute
        # shadows the method and every non-dry-run call raises
        # `TypeError: 'NoneType' object is not callable`.
        self._docker_ok: Optional[bool] = None
        self._fixture: Optional[Dict[str, Dict[str, Any]]] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, repo_path: str) -> ContainerHandle:
        """Provision the sandbox container with the repo mounted as a volume."""
        if self.config.dry_run:
            # No container, and therefore no execution. Checks are replayed
            # from a recorded fixture if one is configured, and skipped
            # otherwise.
            self._container = ContainerHandle(
                container_id="dry-run",
                image=self.config.docker_image,
                workdir=str(Path(repo_path).resolve()),
                is_container=False,
            )
            return self._container

        if not self._docker_available():
            raise SandboxUnavailableError(
                "the Docker daemon is not available, so acceptance checks cannot "
                "be isolated; start Docker, or run with --dry-run to replay "
                "recorded results without executing anything"
            )

        import docker

        client = docker.from_env()
        repo_path = str(Path(repo_path).resolve())

        try:
            container = client.containers.run(
                self.config.docker_image,
                command="sleep infinity",
                detach=True,
                remove=self.config.docker_remove_on_teardown,
                volumes={repo_path: {"bind": self.config.docker_workdir, "mode": "rw"}},
                working_dir=self.config.docker_workdir,
                network_disabled=True,
            )
        except docker.errors.ImageNotFound as exc:
            raise SandboxUnavailableError(
                f"sandbox image {self.config.docker_image!r} could not be pulled; "
                f"build it or point docker_image at an existing image"
            ) from exc
        except docker.errors.DockerException as exc:
            raise SandboxUnavailableError(
                f"sandbox could not be provisioned: {exc}"
            ) from exc

        self._container = ContainerHandle(
            container_id=str(container.id or ""),
            image=self.config.docker_image,
            workdir=self.config.docker_workdir,
        )
        return self._container

    def teardown(self) -> None:
        """Tear down the sandbox container and release compute resources."""
        if self._container is None:
            return

        if self._container.is_container:
            try:
                import docker

                client = docker.from_env()
                container = client.containers.get(self._container.container_id)
                container.stop(timeout=10)
                container.remove(force=True)
            except Exception:
                # Best-effort teardown; never mask a verification result
                pass

        self._container = None

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def run_check(self, kind: CheckKind, command: str) -> CheckResult:
        """Run a single acceptance check inside the sandbox container."""
        start = time.time()

        if not self._container or not self._container.is_container:
            return self._replay_or_skip(kind, command, start)

        assert self._container is not None, "Sandbox not started"
        import docker

        client = docker.from_env()
        container = client.containers.get(self._container.container_id)

        exit_code, stdout, stderr, timed_out = self._exec_with_timeout(
            client, container, command
        )

        return CheckResult(
            kind=kind,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.time() - start) * 1000),
            execution=CheckExecution.CONTAINER,
        )

    def _exec_with_timeout(
        self, client: Any, container: Any, command: str
    ) -> Tuple[int, str, str, bool]:
        """Execute a command in the container under a wall-clock limit.

        `Container.exec_run` has no `timeout` parameter — passing one raised
        `TypeError` on every non-dry-run check, so the acceptance pipeline
        never ran at all outside dry-run mode. The deadline is enforced here
        with a worker thread; a check that overruns is reported as exit 124
        and the container teardown at the end of the verification kills
        whatever is still running inside it.
        """
        import threading

        outcome: Dict[str, Any] = {
            "exit_code": 124,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }

        def _run() -> None:
            try:
                exec_result = container.exec_run(
                    command,
                    workdir=self.config.docker_workdir,
                    demux=True,
                )
                stdout, stderr = self._split_output(getattr(exec_result, "output", None))
                outcome["exit_code"] = exec_result.exit_code or 0
                outcome["stdout"] = stdout
                outcome["stderr"] = stderr
            except Exception as exc:  # noqa: BLE001
                outcome["exit_code"] = 125
                outcome["stderr"] = f"exec failed: {exc}"

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=self.config.docker_timeout)

        if worker.is_alive():
            outcome["timed_out"] = True
            outcome["exit_code"] = 124
            outcome["stderr"] = (
                f"command exceeded the {self.config.docker_timeout}s limit and was "
                f"abandoned; the sandbox is torn down at the end of the run"
            )
        return (
            outcome["exit_code"],
            outcome["stdout"],
            outcome["stderr"],
            outcome["timed_out"],
        )

    @staticmethod
    def _split_output(output: Any) -> Tuple[str, str]:
        """Split a demultiplexed exec result into stdout and stderr."""
        if not output:
            return "", ""
        if isinstance(output, tuple):
            stdout = output[0] if len(output) > 0 else b""
            stderr = output[1] if len(output) > 1 else b""
        else:
            stdout, stderr = output, b""
        if isinstance(stdout, (bytes, bytearray)):
            stdout = bytes(stdout).decode(errors="replace")
        if isinstance(stderr, (bytes, bytearray)):
            stderr = bytes(stderr).decode(errors="replace")
        return str(stdout or ""), str(stderr or "")

    def run_all(self, commands: Optional[List[str]] = None) -> List[CheckResult]:
        """Run every acceptance command in order and return results."""
        commands = commands or self.config.test_commands
        results: List[CheckResult] = []

        for index, command in enumerate(commands):
            kind = self._infer_kind(command, index)
            results.append(self.run_check(kind, command))

        return results

    # ------------------------------------------------------------------ #
    # Replay
    # ------------------------------------------------------------------ #
    def _replay_or_skip(
        self, kind: CheckKind, command: str, start: float
    ) -> CheckResult:
        """Return a recorded result, or a result that explicitly did not run."""
        recorded = self._load_fixture().get(command)
        if recorded is not None:
            return CheckResult(
                kind=kind,
                command=command,
                exit_code=int(recorded.get("exit_code", 0)),
                stdout=str(recorded.get("stdout", "")),
                stderr=str(recorded.get("stderr", "")),
                duration_ms=int(recorded.get("duration_ms", 0)),
                execution=CheckExecution.REPLAY,
            )

        reason = (
            "dry run: acceptance commands are never executed on the host; "
            "record a check_fixture to replay results instead"
        )
        return CheckResult(
            kind=kind,
            command=command,
            exit_code=127,
            stdout="",
            stderr=reason,
            duration_ms=int((time.time() - start) * 1000),
            execution=CheckExecution.SKIPPED,
        )

    def _load_fixture(self) -> Dict[str, Dict[str, Any]]:
        """Recorded acceptance results, keyed by the exact command string."""
        if self._fixture is not None:
            return self._fixture
        self._fixture = {}
        path = self.config.check_fixture
        if not path:
            return self._fixture
        try:
            raw = json.loads(Path(path).expanduser().read_text())
        except (OSError, ValueError):
            return self._fixture
        for entry in raw.get("checks", []):
            command = entry.get("command")
            if command:
                self._fixture[command] = entry
        return self._fixture

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _infer_kind(command: str, index: int) -> CheckKind:
        """Classify a command by what it does, not by where it sits.

        The previous implementation assigned kinds by position, so a fourth
        command was reported as a unit test.
        """
        lowered = command.lower()
        if any(token in lowered for token in ("bandit", "security", "snyk", "semgrep")):
            return CheckKind.SECURITY_SCAN
        if any(token in lowered for token in ("ruff", "lint", "flake8", "eslint", "mypy")):
            return CheckKind.LINT
        if any(token in lowered for token in ("pytest", "test", "jest", "npm test", "go test")):
            return CheckKind.UNIT_TESTS
        fallback = [CheckKind.UNIT_TESTS, CheckKind.LINT, CheckKind.SECURITY_SCAN]
        return fallback[index % len(fallback)]

    def _docker_available(self) -> bool:
        if self._docker_ok is not None:
            return self._docker_ok
        try:
            import docker  # noqa: F401

            client = docker.from_env()
            client.ping()
            self._docker_ok = True
        except Exception:
            self._docker_ok = False
        return self._docker_ok


__all__ = ["ContainerHandle", "DockerRunner", "SandboxUnavailableError"]
