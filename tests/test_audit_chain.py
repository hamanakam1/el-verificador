"""Tests for the audit trail's tamper evidence.

A hash chain that only links records to each other detects a *removed*
record. A chain that also re-hashes each record's contents detects an
*edited* one. The second is the property that makes the trail worth
keeping, so it is tested here against a real database.
"""
import sqlite3
import tempfile
import unittest
from datetime import UTC
from pathlib import Path

from el_verificador.schemas import (
    EdgeRecordLogger,
    VerificationAudit,
    VerificationDecision,
)


def make_audit(index: int, decision=VerificationDecision.PASS, timestamp=None) -> VerificationAudit:
    payload = dict(
        work_package_id=f"dwp-{index}",
        work_package_preview=f"preview {index}",
        repo_path="/tmp/repo",
        decision=decision,
        attempts=1,
        passed=decision is VerificationDecision.PASS,
        committed=False,
        rationale=f"Decision {index} was recorded with its supporting evidence",
        key_evidence=[f"checks_passed={index == 0}", f"attempt={index}"],
    )
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return VerificationAudit(**payload)


class TestAuditChain(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "ledger.sqlite"
        self.logger = EdgeRecordLogger(str(self.db))
        self.addCleanup(self.logger.close)

    def _write(self, count: int = 3) -> None:
        for index in range(count):
            self.logger.log_decision(make_audit(index))

    def _tamper(self, sql: str, *params) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_an_untouched_chain_verifies(self):
        self._write(3)
        ok, reason = self.logger.chain_status()
        self.assertTrue(ok, reason)

    def test_editing_a_rationale_breaks_the_chain(self):
        self._write(3)
        self._tamper(
            "UPDATE verification_audits SET rationale = ? WHERE id = ?",
            "A completely different decision",
            self.logger.get_audit_chain("dwp-1")[0]["id"],
        )
        ok, reason = self.logger.chain_status()
        self.assertFalse(ok)
        self.assertIn("no longer hashes", reason)

    def test_editing_the_evidence_breaks_the_chain(self):
        self._write(3)
        self._tamper(
            "UPDATE verification_audits SET key_evidence = ? WHERE id = ?",
            '["checks_passed=True"]',
            self.logger.get_audit_chain("dwp-0")[0]["id"],
        )
        ok, reason = self.logger.chain_status()
        self.assertFalse(ok)
        self.assertIn("no longer hashes", reason)

    def test_removing_a_record_breaks_the_chain(self):
        self._write(3)
        self._tamper(
            "DELETE FROM verification_audits WHERE work_package_id = ?", "dwp-1"
        )
        ok, reason = self.logger.chain_status()
        self.assertFalse(ok)
        self.assertIn("does not link", reason)

    def test_records_link_in_the_order_they_were_written(self):
        """Two decisions in the same millisecond must still chain."""
        from datetime import datetime

        moment = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
        for index in (0, 1):
            self.logger.log_decision(make_audit(index, timestamp=moment))
        ok, reason = self.logger.chain_status()
        self.assertTrue(ok, reason)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, content_hash, previous_audit_hash "
                "FROM verification_audits ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
        self.assertIsNone(rows[0]["previous_audit_hash"])
        self.assertEqual(rows[1]["previous_audit_hash"], rows[0]["content_hash"])

    def test_the_empty_chain_verifies(self):
        ok, reason = self.logger.chain_status()
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
