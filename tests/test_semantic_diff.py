"""Tests for the semantic diff engine.

The engine is the part of El Verificador that answers the headline question —
"did the agent build the route, or write a TODO stub?" — so it is tested
against real source text and real git repositories rather than mocks.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.schemas import ChangeType, IntentPackage
from el_verificador.semantic_diff import (
    SemanticDiffEngine,
    _significant_words,
    _split_identifier,
    _stem,
)

IMPLEMENTATION = '''"""Search routes."""

from .empty_state import EmptyState


def create_search_route(query: str, results: list[str]) -> dict:
    """Build the search response payload for a query."""
    trimmed = query.strip()
    if not trimmed:
        return EmptyState("Type a search term to begin.").render()
    if not results:
        return EmptyState(f"No matches for '{trimmed}'.").render()
    return {"query": trimmed, "results": list(results), "message": ""}
'''

STUB = '''"""Search routes."""

from .empty_state import EmptyState


def create_search_route(query: str, results: list[str]) -> dict:
    """Build the search response payload for a query."""
    # TODO: implement route
    pass
'''

# A goal that names five identifiers, and two implementations: one that
# provides all of them and one that provides two.
USERS_GOAL = (
    "Implement register_user, create_user, validate_payload, hash_password and "
    "get_user in app/routes/users.py so the POST /users/register endpoint "
    "validates the request body and persists a new user record."
)

USERS_IMPLEMENTED = '''"""User registration routes."""


def validate_payload(raw: dict) -> list[str]:
    """Validate an incoming registration payload."""
    errors = []
    if len(str(raw.get("username", ""))) < 3:
        errors.append("username must be at least 3 characters")
    return errors


def hash_password(secret: str) -> str:
    """Hash a plaintext secret."""
    return secret[::-1]


def create_user(username: str, password_hash: str) -> dict:
    """Persist a validated user."""
    return {"username": username, "password_hash": password_hash}


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    errors = validate_payload(raw)
    if errors:
        return 422, {"errors": errors}
    return 201, create_user(str(raw["username"]), hash_password(str(raw["password"])))


def get_user(username: str) -> dict | None:
    """Look up a registered user."""
    return {"username": username}
'''

USERS_PARTIAL = '''"""User registration routes."""


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    return 201, {"username": raw.get("username")}


def get_user(username: str) -> dict | None:
    """Look up a registered user."""
    return {"username": username}
'''


class _Repo:
    """A throwaway git repository with two commits."""

    def __init__(self, base_file: str, base_text: str, head_file: str, head_text: str):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self._write(base_file, base_text)
        self._git("init", "-q", "-b", "main")
        self._git("add", "-A")
        self._git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "base")
        self._write(head_file, head_text)
        self._git("add", "-A")
        self._git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "head")

    def close(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True)

    def diff(self, base: str = "HEAD~1", head: str = "HEAD") -> object:
        return self.diff_with(
            IntentPackage(
                id="dwp-test",
                true_goal=(
                    "Ship a search endpoint that returns matching results for a query "
                    "and renders useful guidance when the result set is empty."
                ),
                true_goal_summary="Implement create_search_route with an empty-state message.",
                related_files=["searchservice/routes.py"],
            ),
            base,
            head,
        )

    def diff_with(
        self, intent: IntentPackage, base: str = "HEAD~1", head: str = "HEAD"
    ) -> object:
        return SemanticDiffEngine(str(self.path), base, head).compute(intent)


class _RepoTestCase(unittest.TestCase):
    """Base class that cleans up every repository it creates."""

    def setUp(self) -> None:
        self._repos: list[_Repo] = []

    def tearDown(self) -> None:
        for repo in self._repos:
            repo.close()

    def repo(self, *args: str) -> _Repo:
        created = _Repo(*args)
        self._repos.append(created)
        return created


class TestModifiedDetection(_RepoTestCase):
    """A rewritten function body must be reported as a change."""

    def test_rewritten_function_is_reported_as_modified(self):
        repo = self.repo(
            "searchservice/routes.py",
            "def create_search_route(query):\n    return query\n",
            "searchservice/routes.py",
            "def create_search_route(query):\n    return query.strip()\n",
        )
        diff = repo.diff()
        modified = [c for c in diff.ast_changes if c.change_type is ChangeType.MODIFIED]
        self.assertEqual(len(modified), 1, diff.ast_changes)
        self.assertEqual(modified[0].name, "create_search_route")
        self.assertIn("query.strip()", modified[0].after)
        self.assertIn("return query", modified[0].before)

    def test_unchanged_function_is_not_reported(self):
        source = "def create_search_route(query):\n    return query\n"
        repo = self.repo(
            "searchservice/routes.py",
            source,
            "searchservice/other.py",
            "VALUE = 1\n",
        )
        diff = repo.diff()
        self.assertFalse(
            [c for c in diff.ast_changes if c.change_type is ChangeType.MODIFIED]
        )

    def test_new_function_is_reported_as_added_with_its_body(self):
        repo = self.repo(
            "searchservice/routes.py",
            "VALUE = 1\n",
            "searchservice/routes.py",
            "VALUE = 1\n\n\ndef create_search_route(query):\n    return query\n",
        )
        diff = repo.diff()
        added = [c for c in diff.ast_changes if c.change_type is ChangeType.ADDED]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].name, "create_search_route")
        self.assertIn("return query", added[0].after)

    def test_removed_function_is_reported_as_removed(self):
        repo = self.repo(
            "searchservice/routes.py",
            "def create_search_route(query):\n    return query\n",
            "searchservice/routes.py",
            "VALUE = 1\n",
        )
        diff = repo.diff()
        removed = [c for c in diff.ast_changes if c.change_type is ChangeType.REMOVED]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].name, "create_search_route")

    def test_same_method_name_in_two_classes_is_not_confused(self):
        base = (
            "class A:\n    def render(self):\n        return 'a'\n\n\n"
            "class B:\n    def render(self):\n        return 'b'\n"
        )
        head = (
            "class A:\n    def render(self):\n        return 'a2'\n\n\n"
            "class B:\n    def render(self):\n        return 'b'\n"
        )
        repo = self.repo("m.py", base, "m.py", head)
        diff = repo.diff()
        modified_methods = [
            c
            for c in diff.ast_changes
            if c.change_type is ChangeType.MODIFIED and c.symbol_kind.value == "method"
        ]
        # Only A's render changed. B's render has the same name and must not
        # be swept into the comparison.
        self.assertEqual(len(modified_methods), 1, diff.ast_changes)
        self.assertIn("a2", modified_methods[0].after)
        self.assertIn("return 'a'", modified_methods[0].before)


class TestStubDetection(_RepoTestCase):
    """A stub is a stub. Prose is not."""

    def test_pass_body_is_a_stub(self):
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", STUB)
        diff = repo.diff()
        self.assertTrue(diff.has_todo_stubs)
        self.assertIn("create_search_route", diff.stub_symbols)

    def test_ellipsis_body_is_a_stub(self):
        source = "def handler(event):\n    ...\n"
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertTrue(diff.has_todo_stubs)

    def test_not_implemented_error_is_a_stub(self):
        source = "def handler(event):\n    raise NotImplementedError\n"
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertTrue(diff.has_todo_stubs)

    def test_word_bypass_is_not_a_stub(self):
        source = "def handler(event):\n    return event.bypass\n"
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertFalse(diff.has_todo_stubs, diff.stub_symbols)

    def test_ellipsis_in_a_docstring_is_not_a_stub(self):
        source = 'def handler(event):\n    """Load ... the thing."""\n    return event\n'
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertFalse(diff.has_todo_stubs, diff.stub_symbols)

    def test_todo_in_a_string_literal_is_not_a_marker(self):
        source = 'def handler(event):\n    return "TODO: implement this later"\n'
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertFalse(diff.has_todo_stubs, diff.stub_symbols)

    def test_todo_in_a_comment_is_a_marker(self):
        source = "def handler(event):\n    # TODO: implement this later\n    return event\n"
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertTrue(diff.has_todo_stubs)
        self.assertTrue(any("TODO" in s for s in diff.stub_symbols))

    def test_a_function_that_uses_pass_in_the_middle_is_not_a_stub(self):
        source = (
            "def handler(event):\n"
            "    for item in event:\n"
            "        pass\n"
            "    return event\n"
        )
        repo = self.repo("m.py", "VALUE = 1\n", "m.py", source)
        diff = repo.diff()
        self.assertFalse(diff.has_todo_stubs, diff.stub_symbols)


class TestIntentCoverage(_RepoTestCase):
    """The score must measure the code, and publish what it measured."""

    def test_implementation_scores_higher_than_a_partial_implementation(self):
        """A change that omits what the goal names must score lower."""
        intent = IntentPackage(
            id="dwp-test",
            true_goal=USERS_GOAL,
            true_goal_summary="Add the user registration functions to app/routes/users.py",
            related_files=["app/routes/users.py"],
        )
        implemented = self.repo(
            "app/routes/users.py", "VALUE = 1\n", "app/routes/users.py", USERS_IMPLEMENTED
        )
        partial = self.repo("app/routes/users.py", "VALUE = 1\n", "app/routes/users.py", USERS_PARTIAL)
        good = implemented.diff_with(intent)
        bad = partial.diff_with(intent)
        self.assertGreater(
            good.intent_coverage,
            bad.intent_coverage,
            f"{good.intent_coverage} vs {bad.intent_coverage}",
        )
        self.assertIn("validate", bad.uncovered_tokens)
        self.assertNotIn("validate", good.uncovered_tokens)

    def test_a_stub_with_the_right_names_is_caught_by_the_stub_rule_not_the_score(self):
        """A hollow body with the right name still has to fail.

        The coverage score measures whether the named identifiers are present;
        it cannot see that one of them is empty. That is the stub rule's job,
        and the two rules have to cover each other.
        """
        repo = self.repo("searchservice/routes.py", "VALUE = 1\n", "searchservice/routes.py", STUB)
        diff = repo.diff()
        self.assertTrue(diff.has_todo_stubs)
        self.assertIn("create_search_route", diff.stub_symbols)

    def test_coverage_publishes_its_inputs(self):
        repo = self.repo(
            "searchservice/routes.py", "VALUE = 1\n", "searchservice/routes.py", IMPLEMENTATION
        )
        diff = repo.diff()
        self.assertTrue(diff.coverage_basis)
        self.assertTrue(diff.goal_tokens)
        # Covered and uncovered are a partition of the goal vocabulary.
        self.assertFalse(set(diff.covered_tokens) & set(diff.uncovered_tokens))
        self.assertEqual(
            set(diff.covered_tokens) | set(diff.uncovered_tokens),
            set(diff.goal_tokens),
        )
        self.assertEqual(diff.related_files_touched, ["searchservice/routes.py"])

    def test_the_named_identifiers_are_published(self):
        repo = self.repo(
            "searchservice/routes.py", "VALUE = 1\n", "searchservice/routes.py", IMPLEMENTATION
        )
        diff = repo.diff()
        self.assertIn("create_search_route", diff.named_identifiers)

    def test_a_goal_without_identifiers_falls_back_to_its_words(self):
        intent = IntentPackage(
            id="dwp-test",
            true_goal="Render useful guidance when a search returns no results",
            true_goal_summary="",
            related_files=[],
        )
        with tempfile.TemporaryDirectory() as workdir:
            repo = Path(workdir)
            (repo / "m.py").write_text(IMPLEMENTATION)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "x"],
                cwd=repo,
                check=True,
            )
            diff = SemanticDiffEngine(str(repo), "HEAD", "HEAD").compute(intent)
        self.assertIn("the goal names no identifiers", diff.coverage_basis)
        self.assertEqual(diff.named_identifiers, [])

    def test_covered_tokens_are_a_subset_of_goal_tokens(self):
        repo = self.repo(
            "searchservice/routes.py", "VALUE = 1\n", "searchservice/routes.py", IMPLEMENTATION
        )
        diff = repo.diff()
        self.assertTrue(set(diff.covered_tokens) <= set(diff.goal_tokens))

    def test_no_related_files_does_not_grant_a_free_pass(self):
        intent = IntentPackage(
            id="dwp-test",
            true_goal="Implement create_search_route with an empty-state message.",
            true_goal_summary="",
            related_files=[],
        )
        with tempfile.TemporaryDirectory() as workdir:
            repo = Path(workdir)
            (repo / "m.py").write_text(IMPLEMENTATION)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "x"],
                cwd=repo,
                check=True,
            )
            engine = SemanticDiffEngine(str(repo), "HEAD", "HEAD")
            diff = engine.compute(intent)
        self.assertIn("no related files declared", diff.coverage_basis)

    def test_changed_file_outside_the_declared_scope_does_not_score(self):
        repo = self.repo("other/thing.py", "VALUE = 1\n", "other/thing.py", "VALUE = 2\n")
        diff = repo.diff()
        self.assertEqual(diff.related_files_touched, [])


class TestWordHandling(unittest.TestCase):
    """The token comparison has to behave like a comparison, not a coincidence."""

    def test_identifier_is_split_into_words(self):
        self.assertEqual(_split_identifier("create_search_route"), ["create", "search", "route"])

    def test_camel_case_is_split_into_words(self):
        self.assertEqual(_split_identifier("createSearchRoute"), ["create", "search", "route"])

    def test_plural_and_verb_forms_share_a_stem(self):
        self.assertEqual(_stem("results"), _stem("result"))
        self.assertEqual(_stem("returns"), _stem("return"))
        self.assertEqual(_stem("rendering"), _stem("render"))

    def test_a_singular_and_its_plural_share_a_stem(self):
        """"route" and "routes" are the same claim, not two."""
        self.assertEqual(_stem("route"), _stem("routes"))
        self.assertEqual(_stem("match"), _stem("matches"))
        self.assertEqual(_stem("class"), _stem("classes"))
        self.assertEqual(_stem("query"), _stem("queries"))

    def test_stop_words_are_not_significant(self):
        self.assertNotIn("the", _significant_words("the result of the query"))
        self.assertIn("result", _significant_words("the result of the query"))


if __name__ == "__main__":
    unittest.main()
