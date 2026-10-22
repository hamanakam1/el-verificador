"""
Git helpers for El Verificador.

Reads base/head commits and commits verified code back to the target branch.
El Verificador commits only after a passing verification decision.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class GitState:
    base_commit: str
    head_commit: str
    branch: str
    is_dirty: bool


class GitOps:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=self.repo_path,
            timeout=60,
        )

    def current_state(self) -> GitState:
        branch = self._run(["branch", "--show-current"]).stdout.strip() or "main"
        head = self._run(["rev-parse", "HEAD"]).stdout.strip()
        merge_base = self._run(["merge-base", "HEAD", branch]).stdout.strip() or head
        status = self._run(["status", "--porcelain"]).stdout.strip()
        return GitState(
            base_commit=merge_base,
            head_commit=head,
            branch=branch,
            is_dirty=bool(status),
        )

    def diff_stats(self, base: str, head: str) -> str:
        return self._run(["diff", "--stat", f"{base}...{head}"]).stdout

    def commit(self, message: str, branch: Optional[str] = None) -> str:
        """Stage all changes and commit. Returns the new commit SHA."""
        target = branch or self.current_state().branch
        self._run(["checkout", target])
        self._run(["add", "-A"])
        commit_result = self._run(["commit", "-m", message])
        if commit_result.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_result.stderr.strip()}")
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def get_commit_message(self, commit: str) -> str:
        return self._run(["log", "-1", "--format=%B", commit]).stdout.strip()


def merge_base(repo_path: str, a: str, b: str) -> str:
    result = subprocess.run(
        ["git", "merge-base", a, b],
        capture_output=True,
        text=True,
        cwd=repo_path,
        timeout=30,
    )
    return result.stdout.strip() or a
