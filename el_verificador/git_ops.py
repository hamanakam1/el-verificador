"""
Git helpers for El Verificador.

Reads base/head commits and commits verified code back to the target branch.
El Verificador commits only after a passing verification decision.

**The commit path never stages anything it did not review.** A verifier that
runs `git add -A` on a shared checkout is a data-loss vector: it sweeps the
operator's untracked files, their secrets, and their build artefacts into a
commit that looks like a success. The set of paths to stage is therefore an
explicit argument, and the commit is refused when the index or the working
tree holds something the verification did not cover.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class CommitRefused(RuntimeError):
    """The commit was refused because it would carry unreviewed content."""


@dataclass
class GitState:
    base_commit: str
    head_commit: str
    branch: str
    is_dirty: bool


@dataclass
class CommitOutcome:
    """What the commit path actually did."""

    sha: str
    status: str  # "committed" or "already-committed"
    message: str


class GitOps:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
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

    def staged_paths(self) -> list[str]:
        """Paths currently staged in the index."""
        output = self._run(["diff", "--cached", "--name-only"]).stdout
        return [line for line in output.splitlines() if line.strip()]

    def working_tree_differs(self, commit: str, path: str) -> bool:
        """Does the working tree's copy of `path` differ from `commit`?

        True also covers a path that is absent from the working tree, or a
        path that does not exist in `commit` at all.
        """
        committed = self._run(["show", f"{commit}:{path}"])
        if committed.returncode != 0:
            return True
        target = self.repo_path / path
        if not target.is_file():
            return True
        try:
            return target.read_bytes() != committed.stdout.encode()
        except OSError:
            return True

    def progress_signature(self) -> str:
        """A fingerprint of the repository's current state.

        Covers the head commit, the set of changed paths, and how much each
        one changed — so an edit that leaves the same file names but
        different contents produces a different signature. Two attempts with
        the same signature ran against the same tree, and another identical
        run would fail identically.
        """
        import hashlib

        head = self._run(["rev-parse", "HEAD"]).stdout.strip()
        status = self._run(["status", "--porcelain=v1"]).stdout
        numstat = self._run(["diff", "HEAD", "--numstat"]).stdout
        digest = hashlib.sha256(
            "\0".join((head, status, numstat)).encode("utf-8", "replace")
        ).hexdigest()
        return digest

    def tracked_changes(self) -> list[str]:
        """Paths with tracked modifications. Untracked files are not included.

        Untracked files survive a checkout untouched, so they do not make a
        branch switch unsafe. A modified tracked file does: switching would
        carry it, or fail, under another session's feet.
        """
        return self._changed_paths(["--untracked-files=no"])

    def _changed_paths(self, extra: list[str]) -> list[str]:
        output = self._run(["status", "--porcelain", *extra]).stdout
        paths: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # a rename
                path = path.split(" -> ", 1)[1].strip()
            paths.append(path.strip('"'))
        return paths

    def commit(
        self,
        message: str,
        branch: str | None = None,
        paths: list[str] | None = None,
        expected_head: str | None = None,
    ) -> CommitOutcome:
        """Stage exactly `paths` and commit them, and nothing else.

        `paths` is mandatory. There is no blanket add: a verifier that stages
        the whole tree will eventually stage somebody's secrets.

        Two refusals protect the operator:

        - the index already holds staged work that is not in `paths` — that is
          another session's work, and committing it is not ours to do;
        - the working tree's copy of a reviewed path differs from the commit
          the verification actually inspected — committing it would put
          unverified bytes into a commit that carries a verification receipt.

        When the reviewed paths are already committed there is nothing to do,
        and the current head is returned.
        """
        if paths is None:
            raise ValueError(
                "commit() requires the explicit list of paths to stage; "
                "refusing to stage the whole tree"
            )
        if not paths:
            raise ValueError("commit() requires at least one path to stage")

        target = branch or self.current_state().branch

        foreign = sorted(set(self.staged_paths()) - set(paths))
        if foreign:
            raise CommitRefused(
                "the index already holds staged work that this verification "
                "did not review: " + ", ".join(foreign[:10])
            )

        if expected_head:
            drifted = [
                path
                for path in paths
                if self.working_tree_differs(expected_head, path)
            ]
            if drifted:
                raise CommitRefused(
                    "the working tree differs from the verified commit for: "
                    + ", ".join(drifted[:10])
                    + " — committing it would put unverified content into a "
                    "verified commit"
                )

        # Switching branches under another session is not safe. The branch is
        # only changed when the working tree has nothing tracked in flight
        # that is not ours to move — the reviewed paths are the verified
        # work, and they are meant to travel with the switch. Anything else
        # in the tree is another session's, and it is refused rather than
        # carried.
        current = self.current_state().branch
        if target != current:
            in_flight = [p for p in self.tracked_changes() if p not in set(paths)]
            if in_flight:
                raise CommitRefused(
                    f"the working tree has tracked changes this verification "
                    f"did not review ({', '.join(in_flight[:10])}), so it "
                    f"cannot be moved to {target} without disturbing them; "
                    f"commit to the current branch ({current}) or clean the "
                    f"tree first"
                )
            checkout = self._run(["checkout", target])
            if checkout.returncode != 0:
                raise CommitRefused(
                    f"could not check out {target}: {checkout.stderr.strip()}"
                )

        self._run(["add", "--", *paths])

        staged = sorted(set(self.staged_paths()))
        unexpected = sorted(set(staged) - set(paths))
        if unexpected:
            raise CommitRefused(
                "refusing to commit paths outside the reviewed set: "
                + ", ".join(unexpected[:10])
            )

        if not staged:
            head = self._run(["rev-parse", "HEAD"]).stdout.strip()
            return CommitOutcome(
                sha=head,
                status="already-committed",
                message="the reviewed paths were already committed",
            )

        commit_result = self._run(["commit", "-m", message])
        if commit_result.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_result.stderr.strip()}")
        sha = self._run(["rev-parse", "HEAD"]).stdout.strip()
        return CommitOutcome(sha=sha, status="committed", message="committed")

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
