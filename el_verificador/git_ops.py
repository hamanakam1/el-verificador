"""
Git helpers for El Verificador.

Reads base/head commits and commits verified code back to the target branch.
El Verificador commits only after a passing verification decision.

**The commit is scoped to the verified change set.**

The previous implementation ran `git checkout <branch>` and then `git add -A`
before committing. `git add -A` stages *every* file in the working tree,
including changes this process did not make and does not own. In a shared
checkout — where several agents and an orchestrator work at once — that is a
data-loss vector, and it is invisible because the commit is supposed to look
like a success. The factory's operating rules are explicit: stage only the
paths you personally changed, and never `git add -A`.

The commit path now takes the exact set of files the verifier evaluated, stages
only those, commits only those, and then checks that the resulting commit
contains nothing else.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


class CommitScopeError(RuntimeError):
    """Raised when a commit cannot be scoped to the verified change set."""


@dataclass
class GitState:
    base_commit: str
    head_commit: str
    branch: str
    is_dirty: bool


class GitOps:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _run(self, args: List[str]) -> "subprocess.CompletedProcess[str]":
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
        return str(self._run(["diff", "--stat", f"{base}...{head}"]).stdout)

    def changed_paths(self, base: str, head: str = "HEAD") -> List[str]:
        """Files that differ between base and head."""
        result = self._run(["diff", "--name-only", f"{base}...{head}"])
        return [line.strip() for line in str(result.stdout).splitlines() if line.strip()]

    def assert_paths_match_head(self, paths: Sequence[str]) -> None:
        """Fail when the working tree differs from HEAD for these paths.

        The verifier judges the *committed* tree: the diff runs between two
        commits. The commit it then makes takes the *working tree* content of
        the same paths. If the working tree has moved on since the
        verification — the agent is still editing, or something else touched
        the file — the commit would carry content that was never verified.

        The commit must contain what was judged, or nothing.
        """
        scope = [p for p in dict.fromkeys(paths) if p]
        if not scope:
            return
        result = self._run(["diff", "--name-only", "HEAD", "--", *scope])
        if result.returncode != 0:
            raise CommitScopeError(
                f"could not compare the working tree with HEAD: {result.stderr.strip()}"
            )
        changed = [line.strip() for line in str(result.stdout).splitlines() if line.strip()]
        if changed:
            raise CommitScopeError(
                "the working tree changed since the verification, so the "
                "commit would carry content that was never verified: "
                + ", ".join(sorted(changed))
            )

    def commit(
        self,
        message: str,
        paths: Sequence[str],
        branch: Optional[str] = None,
        verified_state: str = "head",
    ) -> str:
        """Commit exactly `paths` and nothing else. Returns the new commit SHA.

        The branch is never switched: `git checkout` on a dirty shared working
        tree can move another session's work. When the requested branch is not
        the current one, the commit is refused rather than forced.

        `verified_state` says what the verifier judged: `"head"` (the
        committed tree) or `"worktree"` (the files on disk). Only the
        worktree case may commit a working tree that differs from HEAD —
        that difference is exactly what was verified.
        """
        scope = [p for p in dict.fromkeys(paths) if p]
        if not scope:
            raise CommitScopeError(
                "refusing to commit: the verified change set is empty"
            )

        current = self.current_state().branch
        if branch and branch != current:
            raise CommitScopeError(
                f"refusing to commit on {current!r} when {branch!r} was requested; "
                f"switching branches in a shared working tree is not safe"
            )

        if verified_state == "head":
            # What was judged must be what is committed.
            self.assert_paths_match_head(scope)

        # `-A` with an explicit pathspec stages additions, modifications and
        # deletions *for those paths only*. It is not the blanket `git add -A`.
        add = self._run(["add", "-A", "--", *scope])
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr.strip()}")

        commit_result = self._run(["commit", "-m", message, "--", *scope])
        if commit_result.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_result.stderr.strip()}")

        self._assert_commit_scope(scope)
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def committed_paths(self, commit: str = "HEAD") -> List[str]:
        """Files touched by a commit."""
        result = self._run(["show", "--name-only", "--format=", commit])
        return [line.strip() for line in str(result.stdout).splitlines() if line.strip()]

    def worktree_fingerprint(self) -> str:
        """A stable fingerprint of the working tree's content.

        Two attempts with the same fingerprint mean the agent changed
        nothing, so another attempt cannot produce a different result. The
        correction loop uses this to stop burning its budget on a no-op.
        """
        import hashlib

        head = self._run(["rev-parse", "HEAD"]).stdout.strip()
        status = self._run(["status", "--porcelain"]).stdout
        diff = self._run(["diff", "HEAD"]).stdout
        digest = hashlib.sha256()
        digest.update(head.encode())
        digest.update(b"\0")
        digest.update(status.encode())
        digest.update(b"\0")
        digest.update(diff.encode())
        return digest.hexdigest()

    def _assert_commit_scope(self, scope: Sequence[str]) -> None:
        """Fail loudly if the commit contains anything outside the scope."""
        committed = set(self.committed_paths())
        allowed = set(scope)
        extra = sorted(committed - allowed)
        if extra:
            raise CommitScopeError(
                "the commit contains files outside the verified change set: "
                + ", ".join(extra)
            )

    def get_commit_message(self, commit: str) -> str:
        return str(self._run(["log", "-1", "--format=%B", commit]).stdout.strip())


def merge_base(repo_path: str, a: str, b: str) -> str:
    result = subprocess.run(
        ["git", "merge-base", a, b],
        capture_output=True,
        text=True,
        cwd=repo_path,
        timeout=30,
    )
    return result.stdout.strip() or a


__all__ = ["CommitScopeError", "GitOps", "GitState", "merge_base"]
