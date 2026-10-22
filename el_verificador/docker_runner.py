"""
Docker sandbox runner for El Verificador.

Runs the CI/CD pipeline (unit tests, linters, security scans) inside the
hydrated Docker sandbox provisioned by El Hidratador. El Verificador never
trusts Taskrivet's self-reported status — it only cares if the container
exits with `0` and every check passes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .schemas import CheckKind, CheckResult, VerificationConfig


@dataclass
class ContainerHandle:
    """Opaque handle to a running sandbox container."""
    container_id: str
    image: str
    workdir: str
    started_at: float = field(default_factory=time.time)


class DockerRunner:
    """
    Executes acceptance checks inside an isolated Docker container.

    When Docker is unavailable or `dry_run` is set, falls back to running
    commands locally so the verifier can still be exercised offline.
    """

    def __init__(self, config: VerificationConfig):
        self.config = config
        self._container: Optional[ContainerHandle] = None
        self._docker_available: Optional[bool] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, repo_path: str) -> ContainerHandle:
        """Provision the sandbox container with the repo mounted as a volume."""
        if self.config.dry_run or not self._docker_available():
            return ContainerHandle(
                container_id="dry-run",
                image=self.config.docker_image,
                workdir=str(Path(repo_path).resolve()),
            )

        import docker

        client = docker.from_env()
        repo_path = str(Path(repo_path).resolve())

        container = client.containers.run(
            self.config.docker_image,
            command="sleep infinity",
            detach=True,
            remove=self.config.docker_remove_on_teardown,
            volumes={repo_path: {"bind": self.config.docker_workdir, "mode": "rw"}},
            working_dir=self.config.docker_workdir,
            network_disabled=True,
        )

        self._container = ContainerHandle(
            container_id=container.id,
            image=self.config.docker_image,
            workdir=self.config.docker_workdir,
        )
        return self._container

    def teardown(self) -> None:
        """Tear down the sandbox container and release compute resources."""
        if self._container is None:
            return

        if not (self.config.dry_run or not self._docker_available()):
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

        if self.config.dry_run or not self._docker_available():
            # Dry-run: execute locally for offline development.
            import shlex
            import subprocess

            try:
                proc = subprocess.run(
                    shlex.split(command),
                    capture_output=True,
                    text=True,
                    timeout=self.config.docker_timeout,
                    cwd=self._container.workdir if self._container else None,
                )
                exit_code = proc.returncode
                stdout = proc.stdout
                stderr = proc.stderr
            except FileNotFoundError:
                exit_code = 127
                stdout = ""
                stderr = f"command not found: {command.split()[0] if command.split() else command}"
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout = exc.stdout.decode() if exc.stdout else ""
                stderr = exc.stderr.decode() if exc.stderr else f"timeout after {self.config.docker_timeout}s"

            return CheckResult(
                kind=kind,
                command=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.time() - start) * 1000),
            )

        assert self._container is not None, "Sandbox not started"
        import docker

        client = docker.from_env()
        container = client.containers.get(self._container.container_id)

        exec_result = container.exec_run(
            command,
            workdir=self.config.docker_workdir,
            demux=True,
            timeout=self.config.docker_timeout,
        )

        exit_code = exec_result.exit_code or 0
        output = exec_result.output
        stdout = output[0].decode(errors="replace") if output and output[0] else ""
        stderr = output[1].decode(errors="replace") if output and len(output) > 1 and output[1] else ""

        return CheckResult(
            kind=kind,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.time() - start) * 1000),
        )

    def run_all(self, commands: Optional[List[str]] = None) -> List[CheckResult]:
        """Run every acceptance command in order and return results."""
        commands = commands or self.config.test_commands
        results: List[CheckResult] = []
        kind_by_position = [CheckKind.UNIT_TESTS, CheckKind.LINT, CheckKind.SECURITY_SCAN]

        for i, command in enumerate(commands):
            kind = kind_by_position[i % len(kind_by_position)]
            results.append(self.run_check(kind, command))

        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _docker_available(self) -> bool:
        if self._docker_available is not None:
            return self._docker_available
        try:
            import docker  # noqa: F401

            client = docker.from_env()
            client.ping()
            self._docker_available = True
        except Exception:
            self._docker_available = False
        return self._docker_available
