"""Tests for the commit path.

The commit path is the one place where the verifier touches the operator's
repository, so it is tested against real git repositories: the rule is that
nothing is ever staged that the verification did not review.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.git_ops import CommitRefused, GitOps


class _RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "t")
        self._git("config", "commit.gpgsign", "false")
        (self.path / "app.py").write_text("VALUE = 1\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        self.ops = GitOps(str(self.path))

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, capture_output=True, text=True
        )

    def _write(self, name: str, text: str) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target


class TestCommitScope(_RepoTestCase):
    def test_commit_without_paths_is_refused(self):
        with self.assertRaises(ValueError):
            self.ops.commit("message", "main")

    def test_commit_with_no_paths_is_refused(self):
        with self.assertRaises(ValueError):
            self.ops.commit("message", "main", paths=[])

    def test_only_the_reviewed_paths_are_committed(self):
        # The reviewed change is in the working tree, not yet committed.
        self._write("app.py", "VALUE = 2\n")
        self._write("operator-notes.md", "private notes\n")
        self._write(".env", "SECRET=hunter2\n")

        outcome = self.ops.commit("verified", "main", paths=["app.py"])
        self.assertEqual(outcome.status, "committed")
        committed = self._git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(committed, ["app.py"])
        # The operator's files are still theirs, and still untracked.
        self.assertTrue((self.path / ".env").exists())
        self.assertIn(".env", self._git("status", "--porcelain").stdout)
        self.assertIn("operator-notes.md", self._git("status", "--porcelain").stdout)

    def test_untracked_files_are_never_swept_in(self):
        self._write(".env", "SECRET=hunter2\n")
        self._write("app.py", "VALUE = 3\n")

        outcome = self.ops.commit("verified", "main", paths=["app.py"])
        self.assertEqual(outcome.status, "committed")
        committed = self._git("show", "--name-only", "--format=", "HEAD").stdout
        self.assertNotIn(".env", committed)

    def test_foreign_staged_work_is_refused(self):
        self._write("app.py", "VALUE = 4\n")
        self._write("someone-elses.md", "not ours\n")
        self._git("add", "someone-elses.md")
        with self.assertRaises(CommitRefused) as caught:
            self.ops.commit("verified", "main", paths=["app.py"])
        self.assertIn("someone-elses.md", str(caught.exception))

    def test_drift_from_the_verified_commit_is_refused(self):
        self._write("app.py", "VALUE = 5\n")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "work")
        verified = self._git("rev-parse", "HEAD").stdout.strip()

        # The operator edits the reviewed file after the verification ran.
        self._write("app.py", "VALUE = 6\n")
        with self.assertRaises(CommitRefused) as caught:
            self.ops.commit(
                "verified", "main", paths=["app.py"], expected_head=verified
            )
        self.assertIn("app.py", str(caught.exception))

    def test_nothing_to_commit_is_reported_not_raised(self):
        self._write("app.py", "VALUE = 7\n")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "work")
        outcome = self.ops.commit("verified", "main", paths=["app.py"])
        self.assertEqual(outcome.status, "already-committed")
        self.assertTrue(outcome.sha)

    def test_a_missing_path_counts_as_drift(self):
        self._write("app.py", "VALUE = 8\n")
        self._git("add", "app.py")
        self._git("commit", "-q", "-m", "work")
        verified = self._git("rev-parse", "HEAD").stdout.strip()
        self.path.joinpath("app.py").unlink()
        with self.assertRaises(CommitRefused):
            self.ops.commit(
                "verified", "main", paths=["app.py"], expected_head=verified
            )


if __name__ == "__main__":
    unittest.main()
