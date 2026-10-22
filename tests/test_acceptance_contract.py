"""Tests for the acceptance contract.

The default contract is three Python commands. A Go project verified with
`pytest -q` is not verified at all — the verdict is about the tool rather
than about the work — so a work package may carry its own contract, and the
verifier runs it. Which contract was used is recorded with the result.
"""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from el_verificador.acceptance import acceptance_contract
from el_verificador.engine import ElVerificador, VerifierDependencies
from el_verificador.schemas import (
    CheckKind,
    CheckResult,
    IntentPackage,
    SemanticDiff,
    VerificationConfig,
    VerificationDecision,
)


class TestAcceptanceContractParsing(unittest.TestCase):
    def test_explicit_commands_are_used(self):
        commands, _ = acceptance_contract({
            "acceptance_commands": ["go test ./...", "go vet ./..."],
        })
        self.assertEqual(commands, ["go test ./...", "go vet ./..."])

    def test_explicit_commands_in_the_context_packet_are_used(self):
        commands, _ = acceptance_contract({
            "context_packet": {"acceptance_commands": ["cargo test"]},
        })
        self.assertEqual(commands, ["cargo test"])

    def test_a_fenced_code_block_in_the_criteria_is_used(self):
        commands, _ = acceptance_contract({
            "acceptance_criteria": [
                "The endpoint returns a token:",
                "```sh\ngo test ./...\ngo vet ./...\n```",
            ],
        })
        self.assertEqual(commands, ["go test ./...", "go vet ./..."])

    def test_a_criterion_without_a_code_block_declares_no_contract(self):
        commands, criteria = acceptance_contract({
            "acceptance_criteria": ["POST /login returns a token"],
        })
        self.assertEqual(commands, [])
        self.assertEqual(criteria, ["POST /login returns a token"])

    def test_prose_is_not_mistaken_for_a_command(self):
        commands, _ = acceptance_contract({
            "acceptance_criteria": ["```\n# run the tests\nmake check\n```"],
        })
        self.assertEqual(commands, ["make check"])

    def test_the_contract_is_bounded(self):
        commands, _ = acceptance_contract({
            "acceptance_commands": [f"command {index}" for index in range(50)],
        })
        self.assertLessEqual(len(commands), 16)


class _FakeDocker:
    def __init__(self, results):
        self.results = results
        self.commands = None

    def start(self, repo_path):
        return MagicMock(container_id="fake", image="img", workdir=repo_path)

    def teardown(self):
        pass

    def run_all(self, commands=None):
        self.commands = list(commands) if commands else None
        return list(self.results)


class TestEngineUsesTheContract(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig(dry_run=True)
        self.check = CheckResult(
            kind=CheckKind.UNIT_TESTS, command="pytest -q", exit_code=0
        )
        self.diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/auth.py"],
            added_lines=3,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.9,
            has_todo_stubs=False,
            stub_symbols=[],
        )

    def _run(self, work_package):
        docker = _FakeDocker([self.check])
        deps = VerifierDependencies(docker=docker, git=MagicMock(), ollama=MagicMock())
        verifier = ElVerificador(self.config, deps=deps)
        with patch("el_verificador.engine.SemanticDiffEngine") as mock_engine:
            mock_engine.return_value.compute.return_value = self.diff
            result = asyncio.run(
                verifier.verify(work_package, "/tmp/repo", "a" * 40, "b" * 40)
            )
        return result, docker

    def test_the_work_packages_contract_is_run(self):
        result, docker = self._run({
            "id": "dwp-1",
            "true_goal": "Implement the Go handler for the upload endpoint",
            "true_goal_summary": "go handler",
            "acceptance_commands": ["go test ./...", "go vet ./..."],
        })
        self.assertEqual(docker.commands, ["go test ./...", "go vet ./..."])
        self.assertEqual(result.acceptance_contract_source, "work-package")
        self.assertEqual(result.acceptance_commands, ["go test ./...", "go vet ./..."])
        self.assertEqual(result.decision, VerificationDecision.PASS)

    def test_the_configured_default_is_used_when_there_is_no_contract(self):
        result, docker = self._run({
            "id": "dwp-2",
            "true_goal": "Implement the login API route",
            "true_goal_summary": "login route",
        })
        self.assertEqual(docker.commands, list(self.config.test_commands))
        self.assertEqual(result.acceptance_contract_source, "config")
        self.assertEqual(result.acceptance_commands, list(self.config.test_commands))

    def test_a_criterion_without_a_code_block_falls_back_to_the_default(self):
        result, docker = self._run({
            "id": "dwp-3",
            "true_goal": "Implement the login API route",
            "true_goal_summary": "login route",
            "acceptance_criteria": ["POST /login returns a token"],
        })
        self.assertEqual(docker.commands, list(self.config.test_commands))
        self.assertEqual(result.acceptance_contract_source, "config")

    def test_the_intent_package_carries_the_contract(self):
        intent = IntentPackage.from_dict({
            "id": "dwp-4",
            "true_goal": "Implement the Go handler for the upload endpoint",
            "true_goal_summary": "go handler",
            "acceptance_commands": ["go test ./..."],
        })
        self.assertEqual(intent.acceptance_commands, ["go test ./..."])


if __name__ == "__main__":
    unittest.main()
