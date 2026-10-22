"""Tests for the semantic diff engine.

This module had no test file at all, and the engine test replaced the diff
engine with a mock. That is how a diff engine which could not see a rewritten
function body — the single most important change in the whole exercise —
shipped green through four commits.

The tests below build real git repositories so the engine is exercised against
real git output rather than against a hand-written diff.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.schemas import ChangeType, IntentPackage
from el_verificador.semantic_diff import SemanticDiffEngine


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class _Repo:
    """A throwaway git repository with two commits."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.repo = self.path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "config", "user.email", "diff@el-verificador.invalid")
        _git(self.repo, "config", "user.name", "El Verificador Diff")

    def write(self, name: str, content: str) -> None:
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, message: str) -> str:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", message, "--allow-empty")
        return _git(self.repo, "rev-parse", "HEAD")

    def diff(self, base: str, head: str, intent: dict = None) -> "object":
        intent = intent or {
            "id": "dwp-1",
            "true_goal": "Implement the create_user_route function",
            "true_goal_summary": "create_user_route",
        }
        return SemanticDiffEngine(str(self.repo), base, head).compute(
            IntentPackage.from_dict(intent)
        )

    def cleanup(self) -> None:
        self._tmp.cleanup()


class TestModificationDetection(unittest.TestCase):
    """A rewritten function body must be visible."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)

    def test_a_rewritten_body_is_reported_as_modified(self):
        self.repo.write("app.py", "def helper():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write("app.py", "def helper():\n    return 2\n")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        modified = [c for c in diff.ast_changes if c.change_type == ChangeType.MODIFIED]
        self.assertEqual(len(modified), 1, diff.ast_changes)
        self.assertEqual(modified[0].name, "helper")
        self.assertIn("return 1", modified[0].before)
        self.assertIn("return 2", modified[0].after)

    def test_an_unchanged_symbol_is_not_reported(self):
        self.repo.write("app.py", "def helper():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write(
            "app.py", "def helper():\n    return 1\n\n\ndef other():\n    return 3\n"
        )
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        names = [c.name for c in diff.ast_changes]
        self.assertIn("other", names)
        self.assertNotIn("helper", names)

    def test_added_and_removed_symbols(self):
        self.repo.write("app.py", "def gone():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write("app.py", "def fresh():\n    return 2\n")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        by_type = {c.change_type: c.name for c in diff.ast_changes}
        self.assertEqual(by_type.get(ChangeType.ADDED), "fresh")
        self.assertEqual(by_type.get(ChangeType.REMOVED), "gone")

    def test_modified_symbols_helper(self):
        self.repo.write("app.py", "def helper():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write("app.py", "def helper():\n    return 2\n")
        head = self.repo.commit("head")
        diff = self.repo.diff(base, head)
        self.assertEqual(len(diff.modified_symbols), 1)
        self.assertIn("helper", diff.modified_symbols[0])


class TestStubDetection(unittest.TestCase):
    """Stubs are found structurally, not by grepping for a word."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)

    def _diff_for(self, body: str, intent: dict = None):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write("app.py", "def keeper():\n    return 1\n\n\n" + body)
        head = self.repo.commit("head")
        return self.repo.diff(base, head, intent)

    def test_a_pass_body_is_a_stub(self):
        diff = self._diff_for("def create_user_route():\n    pass\n")
        self.assertTrue(diff.has_todo_stubs)
        self.assertIn("create_user_route", " ".join(diff.stub_symbols))

    def test_an_ellipsis_body_is_a_stub(self):
        diff = self._diff_for("def create_user_route():\n    ...\n")
        self.assertTrue(diff.has_todo_stubs)

    def test_a_not_implemented_raise_is_a_stub(self):
        diff = self._diff_for(
            "def create_user_route():\n    raise NotImplementedError\n"
        )
        self.assertTrue(diff.has_todo_stubs)

    def test_a_todo_string_body_is_a_stub(self):
        diff = self._diff_for('def create_user_route():\n    "TODO: implement"\n')
        self.assertTrue(diff.has_todo_stubs)

    def test_a_real_implementation_is_not_a_stub(self):
        diff = self._diff_for(
            'def create_user_route():\n    """Create a user."""\n    return {"id": 1}\n'
        )
        self.assertFalse(diff.has_todo_stubs)
        self.assertEqual(diff.stub_symbols, [])

    def test_a_todo_comment_beside_real_code_is_not_a_stub(self):
        """A file that merely mentions TODO is not a stub."""
        diff = self._diff_for(
            "def create_user_route():\n"
            "    # TODO: add pagination later\n"
            '    return {"id": 1}\n'
        )
        self.assertFalse(diff.has_todo_stubs)

    def test_a_docstring_only_function_is_a_stub(self):
        diff = self._diff_for('def create_user_route():\n    """Not yet."""\n')
        self.assertTrue(diff.has_todo_stubs)

    def test_a_marker_after_a_docstring_is_a_stub(self):
        """A marker statement after a docstring must not raise."""
        diff = self._diff_for(
            'def create_user_route():\n    """Route."""\n    "TODO: implement"\n'
        )
        self.assertTrue(diff.has_todo_stubs)

    def test_a_pre_existing_stub_the_agent_did_not_touch_is_not_reported(self):
        """A stub in untouched code is not the agent's violation."""
        self.repo.write(
            "app.py",
            "def old_stub():\n    pass\n\n\ndef keeper():\n    return 1\n",
        )
        base = self.repo.commit("base")
        self.repo.write(
            "app.py",
            "def old_stub():\n    pass\n\n\ndef keeper():\n    return 2\n",
        )
        head = self.repo.commit("head")
        diff = self.repo.diff(base, head)
        self.assertFalse(diff.has_todo_stubs)


class TestTestWeakeningDetection(unittest.TestCase):
    """Deleting or skipping a test to make the build green is a violation."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)

    def test_a_deleted_test_is_detected(self):
        self.repo.write(
            "tests/test_api.py",
            "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n",
        )
        base = self.repo.commit("base")
        self.repo.write("tests/test_api.py", "def test_ok():\n    assert True\n")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertTrue(diff.weakened_tests)
        self.assertIn("test_bad", " ".join(diff.weakened_tests))

    def test_a_skip_marker_is_detected(self):
        self.repo.write(
            "tests/test_api.py", "def test_ok():\n    assert True\n"
        )
        base = self.repo.commit("base")
        self.repo.write(
            "tests/test_api.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip\ndef test_ok():\n    assert True\n",
        )
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertTrue(diff.weakened_tests)
        self.assertIn("skip marker added", " ".join(diff.weakened_tests))

    def test_a_reduced_assertion_count_is_detected(self):
        self.repo.write(
            "tests/test_api.py",
            "def test_ok():\n    assert True\n    assert 1 == 1\n",
        )
        base = self.repo.commit("base")
        self.repo.write("tests/test_api.py", "def test_ok():\n    assert True\n")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertTrue(diff.weakened_tests)
        self.assertIn("assertions reduced", " ".join(diff.weakened_tests))

    def test_a_stronger_test_is_not_a_violation(self):
        self.repo.write("tests/test_api.py", "def test_ok():\n    assert True\n")
        base = self.repo.commit("base")
        self.repo.write(
            "tests/test_api.py",
            "def test_ok():\n    assert True\n    assert 1 == 1\n",
        )
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertEqual(diff.weakened_tests, [])


class TestUnresolvableCommits(unittest.TestCase):
    """A commit that does not exist must not look like "nothing changed"."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)

    def test_a_missing_base_commit_is_an_error(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        head = self.repo.commit("head")
        with self.assertRaises(ValueError) as ctx:
            self.repo.diff("0" * 40, head)
        self.assertIn("base", str(ctx.exception))

    def test_a_missing_head_commit_is_an_error(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        base = self.repo.commit("base")
        with self.assertRaises(ValueError) as ctx:
            self.repo.diff(base, "0" * 40)
        self.assertIn("head", str(ctx.exception))

    def test_an_empty_commit_is_an_error(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        head = self.repo.commit("head")
        with self.assertRaises(ValueError):
            self.repo.diff("", head)


class TestDiffRobustness(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.cleanup)

    def test_a_binary_file_does_not_crash_the_diff(self):
        """A compiled artefact in the diff must not raise."""
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        base = self.repo.commit("base")
        (self.repo.repo / "blob.bin").write_bytes(bytes(range(256)))
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertIn("blob.bin", diff.files_changed)

    def test_a_deleted_file_is_reported(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        self.repo.write("gone.py", "x = 1\n")
        base = self.repo.commit("base")
        (self.repo.repo / "gone.py").unlink()
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertIn("gone.py", diff.deleted_files)
        self.assertNotIn("gone.py", diff.files_changed)

    def test_a_non_python_change_is_listed_but_not_parsed(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        base = self.repo.commit("base")
        self.repo.write("README.md", "# hello\n")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertIn("README.md", diff.files_changed)
        self.assertEqual(diff.ast_changes, [])

    def test_an_empty_diff_is_empty(self):
        self.repo.write("app.py", "def keeper():\n    return 1\n")
        base = self.repo.commit("base")
        head = self.repo.commit("head")

        diff = self.repo.diff(base, head)
        self.assertEqual(diff.files_changed, [])
        self.assertEqual(diff.ast_changes, [])


if __name__ == "__main__":
    unittest.main()
