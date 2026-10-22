"""Tests for the obligation evaluator — the decision path.

The decision function is pure, so these tests are mostly about the
obligations themselves: that each one resolves honestly, that a missing
dimension is reported as not-applicable rather than as a pass, and that the
decision never depends on anything but the obligation list.
"""
import unittest

from el_verificador.obligations import (
    Obligation,
    ObligationEvaluator,
    ObligationStatus,
    coverage,
    decide,
    identifiers_in,
    significant_terms,
    violations,
)
from el_verificador.schemas import (
    CheckExecution,
    CheckKind,
    CheckResult,
    IntentPackage,
    SemanticDiff,
    VerificationDecision,
)


def _diff(**kwargs) -> SemanticDiff:
    base = dict(
        repo_path="/tmp/repo",
        base_commit="a" * 40,
        head_commit="b" * 40,
        files_changed=["app/api.py"],
        added_lines=10,
        removed_lines=0,
        ast_changes=[],
        intent_coverage=0.0,
        has_todo_stubs=False,
        stub_symbols=[],
    )
    base.update(kwargs)
    return SemanticDiff(**base)


def _passing_check() -> CheckResult:
    return CheckResult(
        kind=CheckKind.UNIT_TESTS,
        command="pytest -q",
        exit_code=0,
        execution=CheckExecution.CONTAINER,
    )


def _failing_check() -> CheckResult:
    return CheckResult(
        kind=CheckKind.UNIT_TESTS,
        command="pytest -q",
        exit_code=1,
        stderr="AssertionError: boom",
        execution=CheckExecution.CONTAINER,
    )


def _skipped_check() -> CheckResult:
    return CheckResult(
        kind=CheckKind.UNIT_TESTS,
        command="pytest -q",
        exit_code=127,
        execution=CheckExecution.SKIPPED,
    )


def _intent(**kwargs) -> IntentPackage:
    base = dict(
        id="dwp-1",
        true_goal="Implement the create_user_route function that returns a user payload",
        true_goal_summary="create_user_route returns a user payload",
        related_files=["app/api.py"],
    )
    base.update(kwargs)
    return IntentPackage.from_dict(base)


class TestDecisionFunction(unittest.TestCase):
    """`decide` is a pure function of the obligation list."""

    def test_all_satisfied_is_a_pass(self):
        obligations = [
            Obligation(
                id="a", kind="no_stub", statement="s", status=ObligationStatus.SATISFIED
            )
        ]
        self.assertEqual(decide(obligations), VerificationDecision.PASS)

    def test_one_violation_fails(self):
        obligations = [
            Obligation(
                id="a", kind="no_stub", statement="s", status=ObligationStatus.SATISFIED
            ),
            Obligation(
                id="b", kind="no_stub", statement="s", status=ObligationStatus.VIOLATED
            ),
        ]
        self.assertEqual(decide(obligations), VerificationDecision.FAIL)

    def test_unproven_means_no_verdict(self):
        obligations = [
            Obligation(
                id="a",
                kind="acceptance_command",
                statement="s",
                status=ObligationStatus.UNPROVEN,
            )
        ]
        self.assertEqual(decide(obligations), VerificationDecision.INCONCLUSIVE)

    def test_not_applicable_does_not_block(self):
        obligations = [
            Obligation(
                id="a",
                kind="scope_respected",
                statement="s",
                status=ObligationStatus.NOT_APPLICABLE,
            )
        ]
        self.assertEqual(decide(obligations), VerificationDecision.PASS)

    def test_the_order_of_obligations_does_not_matter(self):
        first = [
            Obligation(
                id="a", kind="no_stub", statement="s", status=ObligationStatus.VIOLATED
            ),
            Obligation(
                id="b", kind="no_stub", statement="s", status=ObligationStatus.SATISFIED
            ),
        ]
        second = list(reversed(first))
        self.assertEqual(decide(first), decide(second))

    def test_coverage_counts_only_applicable_obligations(self):
        obligations = [
            Obligation(
                id="a", kind="no_stub", statement="s", status=ObligationStatus.SATISFIED
            ),
            Obligation(
                id="b",
                kind="scope_respected",
                statement="s",
                status=ObligationStatus.NOT_APPLICABLE,
            ),
        ]
        self.assertEqual(coverage(obligations), 1.0)

    def test_violations_lists_only_blocking_obligations(self):
        obligations = [
            Obligation(
                id="a", kind="no_stub", statement="s", status=ObligationStatus.VIOLATED
            ),
            Obligation(
                id="b", kind="no_stub", statement="s", status=ObligationStatus.SATISFIED
            ),
            Obligation(
                id="c",
                kind="acceptance_command",
                statement="s",
                status=ObligationStatus.UNPROVEN,
            ),
        ]
        self.assertEqual({o.id for o in violations(obligations)}, {"a", "c"})


def _complete_diff() -> SemanticDiff:
    """A diff that satisfies every obligation except the one under test."""
    return _diff(
        files_changed=["app/api.py"],
        ast_changes=[
            {
                "change_type": "added",
                "symbol_kind": "function",
                "name": "create_user_route",
                "file_path": "app/api.py",
                "line_no": 1,
                "after": 'def create_user_route():\n    return {"id": 1, "name": "ada"}',
            }
        ],
    )


class TestObligationEvaluation(unittest.TestCase):
    def setUp(self):
        self.evaluator = ObligationEvaluator(scope_mode="enforce")

    def _by_id(self, obligations):
        return {o.id: o for o in obligations}

    def test_a_clean_change_satisfies_everything(self):
        intent = _intent()
        obligations = self.evaluator.evaluate(
            intent, _complete_diff(), [_passing_check()]
        )
        statuses = {o.id: o.status for o in obligations}
        self.assertEqual(statuses["no_stub"], ObligationStatus.SATISFIED)
        self.assertEqual(statuses["symbol_present"], ObligationStatus.SATISFIED)
        self.assertEqual(statuses["change_present"], ObligationStatus.SATISFIED)
        self.assertEqual(statuses["tests_intact"], ObligationStatus.SATISFIED)
        self.assertEqual(statuses["acceptance_command_0"], ObligationStatus.SATISFIED)
        self.assertEqual(decide(obligations), VerificationDecision.PASS)

    def test_a_stub_is_a_violation(self):
        diff = _diff(has_todo_stubs=True, stub_symbols=["app/api.py::create_user_route"])
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["no_stub"].status, ObligationStatus.VIOLATED)
        self.assertIn("create_user_route", by_id["no_stub"].detail)

    def test_a_failed_check_is_a_violation(self):
        obligations = self.evaluator.evaluate(
            _intent(), _complete_diff(), [_failing_check()]
        )
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["acceptance_command_0"].status, ObligationStatus.VIOLATED)
        self.assertIn("exited 1", by_id["acceptance_command_0"].detail)

    def test_a_skipped_check_is_unproven(self):
        obligations = self.evaluator.evaluate(
            _intent(), _complete_diff(), [_skipped_check()]
        )
        by_id = self._by_id(obligations)
        self.assertEqual(
            by_id["acceptance_command_0"].status, ObligationStatus.UNPROVEN
        )
        self.assertEqual(decide(obligations), VerificationDecision.INCONCLUSIVE)

    def test_no_change_is_a_violation(self):
        diff = _diff(files_changed=[], ast_changes=[])
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["change_present"].status, ObligationStatus.VIOLATED)

    def test_a_missing_symbol_is_a_violation(self):
        diff = _diff(
            ast_changes=[
                {
                    "change_type": "added",
                    "symbol_kind": "function",
                    "name": "something_else",
                    "file_path": "app/api.py",
                    "line_no": 1,
                    "after": "def something_else():\n    return 1",
                }
            ]
        )
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["symbol_present"].status, ObligationStatus.VIOLATED)
        self.assertIn("create_user_route", by_id["symbol_present"].detail)

    def test_a_weakened_test_is_a_violation(self):
        diff = _diff(weakened_tests=["tests/test_api.py::test_bad (removed)"])
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["tests_intact"].status, ObligationStatus.VIOLATED)

    def test_scope_is_enforced_when_asked(self):
        diff = _diff(files_changed=["app/api.py", "app/unrelated.py"])
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["scope_respected"].status, ObligationStatus.VIOLATED)
        self.assertIn("app/unrelated.py", by_id["scope_respected"].detail)

    def test_scope_is_reported_but_not_blocking_by_default(self):
        evaluator = ObligationEvaluator(scope_mode="warn")
        diff = _diff(files_changed=["app/api.py", "app/unrelated.py"])
        obligations = evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["scope_respected"].status, ObligationStatus.SATISFIED)
        self.assertIn("WARNING", by_id["scope_respected"].detail)

    def test_test_files_are_outside_the_scope_fence(self):
        """A test file accompanying the change is not a scope violation."""
        diff = _diff(files_changed=["app/api.py", "tests/test_api.py"])
        obligations = self.evaluator.evaluate(_intent(), diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(by_id["scope_respected"].status, ObligationStatus.SATISFIED)

    def test_scope_is_not_applicable_without_related_files(self):
        evaluator = ObligationEvaluator(scope_mode="enforce")
        intent = _intent(related_files=[])
        diff = _diff(files_changed=["app/whatever.py"])
        obligations = evaluator.evaluate(intent, diff, [_passing_check()])
        by_id = self._by_id(obligations)
        self.assertEqual(
            by_id["scope_respected"].status, ObligationStatus.NOT_APPLICABLE
        )

    def test_an_invalid_scope_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            ObligationEvaluator(scope_mode="sometimes")


class TestIntentHelpers(unittest.TestCase):
    def test_plural_spellings_match(self):
        """A goal saying "returns" must match code saying "return"."""
        from el_verificador.obligations import _variants

        self.assertIn("return", _variants("returns"))
        self.assertIn("integers", _variants("integer"))

    def test_significant_terms_drop_stopwords(self):
        terms = significant_terms("Implement the user login API route for the service")
        self.assertNotIn("the", terms)
        self.assertNotIn("implement", terms)
        self.assertIn("user", terms)
        self.assertIn("login", terms)

    def test_identifiers_in_finds_a_named_symbol(self):
        self.assertIn("create_user_route", identifiers_in("add create_user_route now"))

    def test_identifiers_in_ignores_prose(self):
        self.assertEqual(identifiers_in("create user route"), set())

    def test_identifiers_in_finds_camel_case(self):
        self.assertIn("create_user_route", identifiers_in("add createUserRoute now"))


if __name__ == "__main__":
    unittest.main()
