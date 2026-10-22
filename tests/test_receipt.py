"""Tests for the EdgeRecord receipt.

The verifier wrote its audit to a SQLite table it invented. The real
EdgeRecord is file-based, with a canonical hashing rule, and the receipts this
module produces must be verifiable with the same tooling that checks the
operator's own cycle journal.

The non-ASCII case is not academic: ERA-000314 shipped a fingerprint that the
reference verifier rejected, because Python's `json.dumps` escapes non-ASCII by
default and the canonical rule requires it to be left as literal UTF-8.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from el_verificador.receipt import (
    ReceiptChain,
    artifact_records,
    build_receipt,
    canonical_bytes,
    fingerprint,
    outcome_for_decision,
    sort_keys,
    validate_receipt,
)

ERA_TOOL = Path.home() / "Projects" / "edgerecord-main" / "bin" / "era-fingerprint.py"


def _receipt(**kwargs):
    base = dict(
        receipt_id="va-audit-0123456789abcdef",
        kind="verification",
        outcome="success",
        notes="A note with an em-dash — and a curly quote ’",
        summary="Summary with an em-dash — that broke ERA-000314",
    )
    base.update(kwargs)
    return build_receipt(**base)


class TestCanonicalHashing(unittest.TestCase):
    def test_keys_are_sorted_recursively(self):
        body = {"b": 1, "a": {"z": 1, "y": {"q": 0, "p": 1}}}
        self.assertEqual(
            list(sort_keys(body).keys()), ["a", "b"]
        )
        self.assertEqual(list(sort_keys(body)["a"].keys()), ["y", "z"])
        self.assertEqual(list(sort_keys(body)["a"]["y"].keys()), ["p", "q"])

    def test_non_ascii_is_left_as_literal_utf8(self):
        """The rule that broke ERA-000314."""
        body = {"summary": "em-dash — here"}
        raw = canonical_bytes(body)
        self.assertIn("—".encode(), raw)
        self.assertNotIn(b"\\u2014", raw)

    def test_the_fingerprint_fields_are_excluded(self):
        receipt = _receipt()
        body = {
            k: v
            for k, v in receipt.items()
            if k not in ("proof_fingerprint", "full_hash")
        }
        proof, full = fingerprint(receipt)
        import hashlib

        expected = hashlib.sha256(canonical_bytes(body)).hexdigest()
        self.assertEqual(full, expected)
        self.assertEqual(proof, full[:16])

    def test_the_fingerprint_is_derived_not_typed(self):
        receipt = _receipt()
        self.assertEqual(len(receipt["full_hash"]), 64)
        self.assertEqual(len(receipt["proof_fingerprint"]), 16)
        self.assertTrue(receipt["full_hash"].startswith(receipt["proof_fingerprint"]))

    def test_a_receipt_validates(self):
        self.assertEqual(validate_receipt(_receipt()), [])

    def test_a_tampered_receipt_fails_validation(self):
        receipt = _receipt()
        receipt["summary"] = "something else"
        self.assertTrue(validate_receipt(receipt))

    def test_a_missing_required_field_fails_validation(self):
        receipt = _receipt()
        del receipt["receipt_id"]
        problems = validate_receipt(receipt)
        self.assertTrue(any("receipt_id" in p for p in problems))

    def test_an_unknown_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            build_receipt(receipt_id="x", kind="verification", outcome="maybe")

    @unittest.skipUnless(ERA_TOOL.exists(), "the EdgeRecord fingerprint tool is not present")
    def test_the_reference_tool_agrees(self):
        """Byte-for-byte agreement with EdgeRecord's own tool."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            receipt = _receipt()
            path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
            result = subprocess.run(
                [sys.executable, str(ERA_TOOL), "--check", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK", result.stdout)


class TestReceiptChain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.chain = ReceiptChain(self._tmp.name)

    def test_a_receipt_is_written(self):
        path = self.chain.append(_receipt())
        self.assertTrue(path.exists())
        self.assertEqual(len(self.chain.receipts()), 1)

    def test_receipts_are_chained(self):
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000001"))
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000002"))
        records = self.chain.receipts()
        self.assertIsNone(records[0]["previous_receipt_id"])
        self.assertEqual(
            records[1]["previous_receipt_id"], "va-audit-0000000000000001"
        )
        self.assertEqual(
            records[1]["previous_fingerprint"], records[0]["proof_fingerprint"]
        )

    def test_the_chain_verifies(self):
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000001"))
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000002"))
        self.assertTrue(self.chain.verify_chain())

    def test_a_removed_receipt_breaks_the_chain(self):
        """Removing a receipt from the middle is detectable."""
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000001"))
        middle = self.chain.append(_receipt(receipt_id="va-audit-0000000000000002"))
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000003"))
        Path(middle).unlink()
        self.assertFalse(self.chain.verify_chain())

    def test_a_rewritten_receipt_breaks_the_chain(self):
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000001"))
        second = self.chain.append(_receipt(receipt_id="va-audit-0000000000000002"))
        payload = json.loads(Path(second).read_text())
        payload["summary"] = "quietly rewritten"
        Path(second).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        self.assertFalse(self.chain.verify_chain())

    def test_an_invalid_receipt_is_refused(self):
        receipt = _receipt()
        receipt["proof_fingerprint"] = "0" * 16
        with self.assertRaises(ValueError):
            self.chain.append(receipt)

    def test_receipts_can_be_found_by_ticket(self):
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000001", ticket="M-7"))
        self.chain.append(_receipt(receipt_id="va-audit-0000000000000002", ticket="M-8"))
        self.assertEqual(len(self.chain.for_ticket("M-7")), 1)


class TestOutcomeMapping(unittest.TestCase):
    def test_decisions_map_onto_the_contract_vocabulary(self):
        self.assertEqual(outcome_for_decision("pass"), "success")
        self.assertEqual(outcome_for_decision("fail"), "failed")
        self.assertEqual(outcome_for_decision("escalate"), "blocked")
        self.assertEqual(outcome_for_decision("inconclusive"), "partial")

    def test_artifact_records_carry_the_hash(self):
        records = artifact_records(
            [{"path": "/tmp/x", "kind": "check_stdout", "bytes": 3, "sha256": "a" * 64}]
        )
        self.assertEqual(records[0]["sha256"], "a" * 64)
        self.assertEqual(records[0]["type"], "check_stdout")


if __name__ == "__main__":
    unittest.main()
