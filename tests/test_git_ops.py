"""Tests for the git commit path.

The previous implementation ran `git checkout <branch>` and then `git add -A`
before committing. `git add -A` stages every file in the working tree,
including changes this process did not make. In a shared checkout that is a
data-loss vector, and the factory's operating rules forbid it outright:
stage only the paths you personally changed, never `git add -A`.

These tests pin the replacement: the commit is scoped to the verified change
set, and a commit that reaches outside that scope fails loudly.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.git_ops import CommitScopeError, GitOps


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class _Repo:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.repo = self.path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "config", "user.email", "git@el-verificador.invalid")
        _git(self.repo, "config", "user.name", "El Verificador Git")
        (self.repo / "owned.py").write_text("print('owned')\n")
        (self.repo / "theirs.py").write_text("print('theirs')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.branch = _git(self.repo, "branch", "--show-current")

    def write(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class TestCommitScope(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)
        self.git = GitOps(str(self.repo.repo))

    def test_a_commit_contains_only_the_named_paths(self):
        self.repo.write("owned.py", "print('changed')\n")
        # Another session left an unrelated change in the tree.
        self.repo.write("theirs.py", "print('not mine')\n")

        sha = self.git.commit("feat: scoped change", ["owned.py"])
        self.assertTrue(sha)

        committed = self.git.committed_paths(sha)
        self.assertEqual(committed, ["owned.py"])

    def test_an_unrelated_change_is_left_alone(self):
        self.repo.write("owned.py", "print('changed')\n")
        self.repo.write("theirs.py", "print('not mine')\n")

        self.git.commit("feat: scoped change", ["owned.py"])

        # The other session's file must still be uncommitted.
        status = _git(self.repo.repo, "status", "--porcelain")
        self.assertIn("theirs.py", status)

    def test_an_empty_scope_is_refused(self):
        with self.assertRaises(CommitScopeError):
            self.git.commit("feat: nothing", [])

    def test_the_branch_is_never_switched(self):
        """Switching branches in a shared working tree is not safe."""
        self.repo.write("owned.py", "print('changed')\n")
        with self.assertRaises(CommitScopeError) as ctx:
            self.git.commit("feat: scoped change", ["owned.py"], branch="some-other-branch")
        self.assertIn("some-other-branch", str(ctx.exception))
        self.assertEqual(
            _git(self.repo.repo, "branch", "--show-current"), self.repo.branch
        )

    def test_the_current_branch_is_accepted(self):
        self.repo.write("owned.py", "print('changed')\n")
        sha = self.git.commit(
            "feat: scoped change", ["owned.py"], branch=self.repo.branch
        )
        self.assertTrue(sha)

    def test_a_new_file_is_committed(self):
        self.repo.write("fresh.py", "print('new')\n")
        sha = self.git.commit("feat: new file", ["fresh.py"])
        self.assertIn("fresh.py", self.git.committed_paths(sha))

    def test_a_deleted_file_is_committed(self):
        (self.repo.repo / "theirs.py").unlink()
        sha = self.git.commit("feat: remove file", ["theirs.py"])
        self.assertIn("theirs.py", self.git.committed_paths(sha))

    def test_changed_paths_reports_the_diff(self):
        self.repo.write("owned.py", "print('changed')\n")
        base = _git(self.repo.repo, "rev-parse", "HEAD")
        self.repo.write("fresh.py", "print('new')\n")
        _git(self.repo.repo, "add", "-A")
        _git(self.repo.repo, "commit", "-qm", "change")
        head = _git(self.repo.repo, "rev-parse", "HEAD")
        self.assertIn("fresh.py", self.git.changed_paths(base, head))
        self.assertIn("owned.py", self.git.changed_paths(base, head))

    def test_the_worktree_fingerprint_changes_with_the_tree(self):
        before = self.git.worktree_fingerprint()
        self.repo.write("owned.py", "print('changed')\n")
        after = self.git.worktree_fingerprint()
        self.assertNotEqual(before, after)

    def test_the_worktree_fingerprint_is_stable_when_nothing_changes(self):
        before = self.git.worktree_fingerprint()
        self.assertEqual(before, self.git.worktree_fingerprint())


if __name__ == "__main__":
    unittest.main()
