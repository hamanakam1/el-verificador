"""Tests for the acceptance contract resolution.

The verifier used to run a hardcoded list of three commands regardless of what
the work asked for. A ticket that asked for `npm test` was "verified" by
running pytest, which proves nothing about the ticket.
"""
import unittest

from el_verificador.acceptance import (
    AcceptanceSource,
    extract_commands,
    resolve_acceptance,
)
from el_verificador.schemas import IntentPackage, VerificationConfig


def _intent(**kwargs) -> IntentPackage:
    base = dict(
        id="dwp-1",
        true_goal="Implement the create_user_route function",
        true_goal_summary="create_user_route",
    )
    base.update(kwargs)
    return IntentPackage.from_dict(base)


class TestExtractCommands(unittest.TestCase):
    def test_a_fenced_block_is_read(self):
        criteria = "The route must work.\n\n```sh\npytest -q\nruff check .\n```\n"
        self.assertEqual(extract_commands(criteria), ["pytest -q", "ruff check ."])

    def test_comments_in_a_fenced_block_are_skipped(self):
        criteria = "```\n# run the suite\npytest -q\n```"
        self.assertEqual(extract_commands(criteria), ["pytest -q"])

    def test_a_bare_command_line_is_read(self):
        criteria = "Acceptance:\n- pytest -q\n- ruff check .\n"
        self.assertEqual(extract_commands(criteria), ["pytest -q", "ruff check ."])

    def test_prose_is_not_guessed_at(self):
        criteria = "The service should respond quickly and handle errors."
        self.assertEqual(extract_commands(criteria), [])

    def test_empty_criteria_yield_nothing(self):
        self.assertEqual(extract_commands(""), [])

    def test_duplicates_are_dropped_and_order_kept(self):
        criteria = "```\npytest -q\nruff check .\npytest -q\n```"
        self.assertEqual(extract_commands(criteria), ["pytest -q", "ruff check ."])

    def test_a_javascript_command_is_read(self):
        criteria = "```sh\nnpm test\nnpm run lint\n```"
        self.assertEqual(extract_commands(criteria), ["npm test", "npm run lint"])


class TestResolveAcceptance(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig()

    def test_an_explicit_command_list_wins(self):
        intent = _intent(
            acceptance_commands=["npm test", "npm run lint"],
            acceptance_criteria="```\npytest -q\n```",
        )
        contract = resolve_acceptance(intent, self.config)
        self.assertEqual(contract.commands, ["npm test", "npm run lint"])
        self.assertEqual(contract.source, AcceptanceSource.WORK_PACKAGE)
        self.assertFalse(contract.is_default)

    def test_criteria_are_used_when_there_is_no_list(self):
        intent = _intent(acceptance_criteria="```\ngo test ./...\n```")
        contract = resolve_acceptance(intent, self.config)
        self.assertEqual(contract.commands, ["go test ./..."])
        self.assertEqual(contract.source, AcceptanceSource.CRITERIA)

    def test_the_configured_default_is_the_fallback(self):
        contract = resolve_acceptance(_intent(), self.config)
        self.assertEqual(contract.commands, self.config.test_commands)
        self.assertEqual(contract.source, AcceptanceSource.CONFIG)
        self.assertTrue(contract.is_default)

    def test_unparseable_criteria_fall_back_to_the_default(self):
        intent = _intent(acceptance_criteria="It should behave correctly.")
        contract = resolve_acceptance(intent, self.config)
        self.assertEqual(contract.source, AcceptanceSource.CONFIG)


if __name__ == "__main__":
    unittest.main()
