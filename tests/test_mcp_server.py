"""Tests for the MCP surface.

The point of exposing the verifier as a tool is that agents stop
reimplementing checks. The tools are tested here directly, without the MCP
transport, because the logic is what matters and the transport is a thin
wrapper.
"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from el_verificador.mcp_server import TOOL_NAMES, VerificationTools
from el_verificador.receipt import ReceiptChain, build_receipt
from tests.support import TempStore


class TestVerificationTools(unittest.TestCase):
    def setUp(self):
        self.store = TempStore()
        self.addCleanup(self.store.cleanup)
        self.tools = VerificationTools(self.store.config(dry_run=True))

    def test_the_tool_names_are_stable(self):
        self.assertEqual(
            TOOL_NAMES,
            (
                "verify_work_package",
                "verification_status",
                "verification_evidence",
                "verify_receipt_chain",
            ),
        )

    def test_an_unknown_work_package_has_no_status(self):
        status = self.tools.verification_status("dwp-nobody")
        self.assertFalse(status["found"])

    def test_the_chain_of_an_empty_store_is_valid(self):
        result = self.tools.verify_receipt_chain()
        self.assertTrue(result["valid"])
        self.assertEqual(result["receipts"], 0)

    def test_evidence_for_an_unknown_receipt(self):
        result = self.tools.verification_evidence("va-audit-nothing")
        self.assertFalse(result["found"])

    def test_corrupt_chain_reports_invalid_instead_of_an_older_success(self):
        chain = ReceiptChain(self.tools.config.receipt_dir)
        chain.append(build_receipt("first", "verification", "success",
                                   work_package_id="dwp-damaged"))
        newest = chain.append(build_receipt("second", "verification", "failed",
                                            work_package_id="dwp-damaged"))
        newest.write_text('{"private_output": "synthetic-sensitive-fragment"', encoding="utf-8")

        status = self.tools.verification_status("dwp-damaged")
        evidence = self.tools.verification_evidence("first")
        validity = self.tools.verify_receipt_chain()

        for response in (status, evidence, validity):
            self.assertFalse(response["valid"])
            self.assertEqual(response["error"], "receipt chain integrity check failed")
            self.assertNotIn("synthetic-sensitive-fragment", json.dumps(response))
        self.assertFalse(status["found"])
        self.assertNotIn("outcome", status)
        self.assertFalse(evidence["found"])
        self.assertNotIn("artifacts", evidence)
        self.assertIsNone(validity["receipts"])

    def test_verify_returns_a_verdict(self):
        """The tool's whole job: a verdict, with the evidence behind it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "repo"
            repo.mkdir()

            def git(*args):
                result = subprocess.run(
                    ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
                )
                assert result.returncode == 0, result.stderr
                return result.stdout.strip()

            git("init", "-q", ".")
            git("config", "user.email", "mcp@el-verificador.invalid")
            git("config", "user.name", "El Verificador MCP")
            (repo / "demo.py").write_text(
                'def add(a, b):\n    """Return the sum."""\n    return 0\n'
            )
            git("add", "-A")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (repo / "demo.py").write_text(
                'def add(a, b):\n    """Return the sum."""\n    return a + b\n'
            )
            git("add", "-A")
            git("commit", "-qm", "head")
            head = git("rev-parse", "HEAD")

            outcome = asyncio.run(
                self.tools.verify_work_package(
                    {
                        "id": "dwp-mcp",
                        "true_goal": "Implement the add function that returns the sum",
                        "true_goal_summary": "add returns the sum",
                        "related_files": ["demo.py"],
                    },
                    str(repo),
                    base,
                    head,
                )
            )

        self.assertIn(outcome["decision"], ("pass", "fail", "inconclusive"))
        self.assertIn("obligations", outcome)
        self.assertIn("evidence_hashes", outcome)
        self.assertIn("receipt_id", outcome)
        json.dumps(outcome)

    def test_a_verification_is_recorded_and_findable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "repo"
            repo.mkdir()

            def git(*args):
                result = subprocess.run(
                    ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
                )
                assert result.returncode == 0, result.stderr
                return result.stdout.strip()

            git("init", "-q", ".")
            git("config", "user.email", "mcp@el-verificador.invalid")
            git("config", "user.name", "El Verificador MCP")
            (repo / "demo.py").write_text(
                'def add(a, b):\n    """Return the sum."""\n    return 0\n'
            )
            git("add", "-A")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (repo / "demo.py").write_text(
                'def add(a, b):\n    """Return the sum."""\n    return a + b\n'
            )
            git("add", "-A")
            git("commit", "-qm", "head")
            head = git("rev-parse", "HEAD")

            outcome = asyncio.run(
                self.tools.verify_work_package(
                    {
                        "id": "dwp-mcp",
                        "true_goal": "Implement the add function that returns the sum",
                        "true_goal_summary": "add returns the sum",
                        "related_files": ["demo.py"],
                    },
                    str(repo),
                    base,
                    head,
                )
            )

        status = self.tools.verification_status("dwp-mcp")
        self.assertTrue(status["found"])
        self.assertEqual(status["receipt_id"], outcome["receipt_id"])

        evidence = self.tools.verification_evidence(outcome["receipt_id"])
        self.assertTrue(evidence["found"])
        self.assertGreater(evidence["count"], 0)

        self.assertTrue(self.tools.verify_receipt_chain()["valid"])


if __name__ == "__main__":
    unittest.main()
