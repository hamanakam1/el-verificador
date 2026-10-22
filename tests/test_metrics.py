"""Tests for the metrics.

A verifier that blocks good work gets switched off, and a verifier that
blocks nothing is theatre. Both failure modes are invisible without numbers,
so the numbers are computed from the durable record rather than asserted.
"""
import unittest

from el_verificador.metrics import collect
from el_verificador.receipt import ReceiptChain, build_receipt
from tests.support import TempStore


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.store = TempStore()
        self.addCleanup(self.store.cleanup)
        self.chain = ReceiptChain(self.store.receipt_dir)

    def _write(self, receipt_id, outcome, work_package_id="dwp-1", changes=None):
        self.chain.append(
            build_receipt(
                receipt_id=receipt_id,
                kind="verification",
                outcome=outcome,
                work_package_id=work_package_id,
                detected_changes=changes or [],
            )
        )

    def test_an_empty_chain_reports_zeroes(self):
        report = collect(self.store.config()).as_dict()
        self.assertEqual(report["total_verifications"], 0)
        self.assertEqual(report["pass_rate"], 0.0)
        self.assertEqual(report["escalation_rate"], 0.0)

    def test_outcomes_are_counted(self):
        self._write("va-audit-0000000000000001", "success")
        self._write("va-audit-0000000000000002", "failed")
        self._write("va-audit-0000000000000003", "blocked")
        self._write("va-audit-0000000000000004", "success")
        report = collect(self.store.config()).as_dict()
        self.assertEqual(report["total_verifications"], 4)
        self.assertEqual(report["outcomes"]["success"], 2)
        self.assertEqual(report["outcomes"]["failed"], 1)
        self.assertEqual(report["outcomes"]["blocked"], 1)
        self.assertEqual(report["pass_rate"], 0.5)
        self.assertEqual(report["escalation_rate"], 0.25)

    def test_work_packages_are_counted(self):
        self._write("va-audit-0000000000000001", "success", work_package_id="dwp-a")
        self._write("va-audit-0000000000000002", "failed", work_package_id="dwp-a")
        self._write("va-audit-0000000000000003", "success", work_package_id="dwp-b")
        report = collect(self.store.config()).as_dict()
        self.assertEqual(report["work_packages"], 2)

    def test_blocking_obligations_are_counted(self):
        self._write(
            "va-audit-0000000000000001",
            "failed",
            changes=["no_stub: stub symbol(s): create_user_route"],
        )
        self._write(
            "va-audit-0000000000000002",
            "failed",
            changes=["no_stub: stub symbol(s): other"],
        )
        report = collect(self.store.config()).as_dict()
        self.assertEqual(report["blocking_obligations"]["no_stub"], 2)

    def test_the_window_is_reported(self):
        self._write("va-audit-0000000000000001", "success")
        self._write("va-audit-0000000000000002", "success")
        report = collect(self.store.config()).as_dict()
        self.assertTrue(report["first_verification_at"])
        self.assertTrue(report["last_verification_at"])

    def test_the_report_is_json_serialisable(self):
        import json

        self._write("va-audit-0000000000000001", "success")
        json.dumps(collect(self.store.config()).as_dict())


if __name__ == "__main__":
    unittest.main()
