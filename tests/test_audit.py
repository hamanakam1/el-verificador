"""
Tests for the audit ledger's tamper evidence.

The ledger is the only durable record of what the verifier decided. Two
properties have to hold:

1. the digest of a record covers the *content* of that record, not a summary
   of it; and
2. a record that has been altered after it was written is detected.

The second property is the one the chain alone does not give you. A chain
proves the rows were written consistently; only recomputing each digest from
the row's own content proves the rows still say what they said.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC
from pathlib import Path

from el_verificador.audit import ChainReport, chain_for, verify_ledger
from el_verificador.schemas import (
    EdgeRecordLogger,
    VerificationAudit,
    VerificationDecision,
)


def _audit_fields() -> dict:
    """The evidence-bearing fields of an audit record."""
    fields = dict(
        work_package_id="dwp-test-0001",
        work_package_preview="Add a registration endpoint that validates the body",
        repo_path="/tmp/repo",
        decision=VerificationDecision.PASS,
        attempts=1,
        passed=True,
        committed=True,
        rationale="All acceptance checks passed and intent aligned",
        key_evidence=["checks_passed=True", "intent_coverage=0.94"],
    )
    model = VerificationAudit.model_fields
    if "evidence_hashes" in model:
        fields["evidence_hashes"] = ["sha256:" + "0" * 64]
    if "obligations" in model:
        fields["obligations"] = [
            {"kind": "symbol", "statement": "register_user exists", "status": "satisfied"}
        ]
    return fields


def _audit(
    work_package_id: str = "dwp-test-0001",
    decision: str = "pass",
    rationale: str = "All acceptance checks passed and intent aligned",
    passed: bool = True,
    committed: bool = True,
    attempts: int = 1,
    key_evidence=None,
) -> VerificationAudit:
    kwargs = dict(
        work_package_id=work_package_id,
        work_package_preview="Add a registration endpoint that validates the body",
        repo_path="/tmp/repo",
        decision=VerificationDecision(decision),
        attempts=attempts,
        passed=passed,
        committed=committed,
        rationale=rationale,
        key_evidence=list(
            key_evidence
            if key_evidence is not None
            else ["checks_passed=True", "intent_coverage=0.94"]
        ),
    )
    fields = VerificationAudit.model_fields
    if "evidence_hashes" in fields:
        kwargs["evidence_hashes"] = ["sha256:" + "0" * 64]
    if "obligations" in fields:
        kwargs["obligations"] = [
            {"kind": "symbol", "statement": "register_user exists", "status": "satisfied"}
        ]
    return VerificationAudit(**kwargs)


class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "ledger.sqlite")
        self.logger = EdgeRecordLogger(self.db)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.logger.close)

    def _append(self, **overrides) -> VerificationAudit:
        audit = _audit(**overrides)
        self.logger.log_decision(audit)
        return audit

    def _ids(self) -> list:
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id FROM verification_audits"
            ).fetchall()
        return [row["id"] for row in rows]

    def _first_id(self) -> str:
        return self._ids()[0]

    def _second_id(self) -> str:
        return self._ids()[1]

    # ------------------------------------------------------------------ #
    # Hashing covers the content
    # ------------------------------------------------------------------ #
    def test_the_digest_covers_the_evidence(self):
        baseline = _audit().compute_hash()
        mutations = {
            "decision": {"decision": VerificationDecision.FAIL},
            "passed": {"passed": False},
            "committed": {"committed": False},
            "rationale": {"rationale": "a completely different explanation"},
            "attempts": {"attempts": 3},
            "work_package_id": {"work_package_id": "dwp-other"},
            "key_evidence": {"key_evidence": ["something else"]},
        }
        for field, override in mutations.items():
            with self.subTest(field=field):
                self.assertNotEqual(_audit(**override).compute_hash(), baseline)

    def test_the_digest_is_stable_for_identical_content(self):
        """The same decision content must always hash to the same digest."""
        from datetime import datetime

        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        first = VerificationAudit(
            **{
                **_audit_fields(),
                "timestamp": stamp,
            }
        )
        second = VerificationAudit(
            **{
                **_audit_fields(),
                "timestamp": stamp,
            }
        )
        self.assertEqual(first.compute_hash(), second.compute_hash())

    # ------------------------------------------------------------------ #
    # Chain integrity
    # ------------------------------------------------------------------ #
    def test_an_empty_ledger_verifies(self):
        report = verify_ledger(self.db)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.checked, 0)

    def test_a_written_ledger_verifies(self):
        for index in range(4):
            self._append(attempts=index + 1)
        report = verify_ledger(self.db)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.checked, 4)

    def test_records_link_to_their_predecessor(self):
        first = self._append()
        second = self._append(attempts=2)
        self.assertIsNone(first.previous_audit_hash)
        self.assertEqual(second.previous_audit_hash, first.content_hash)

    def test_an_edited_record_is_detected(self):
        """The record's content no longer matches its recorded digest."""
        self._append()
        self._append(attempts=2)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE verification_audits SET rationale = ? WHERE id = ?",
                ("quietly rewritten by someone", self._first_id()),
            )
        report = verify_ledger(self.db)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("does not match its recorded digest" in e for e in report.errors),
            report.errors,
        )

    def test_a_forged_digest_is_detected(self):
        """Rewriting the stored digest to match an edited row still fails."""
        self._append()
        self._append(attempts=2)
        forged = _audit(rationale="quietly rewritten by someone").compute_hash()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE verification_audits SET rationale = ?, content_hash = ? "
                "WHERE seq = 1",
                ("quietly rewritten by someone", forged),
            )
        report = verify_ledger(self.db)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("does not match its recorded digest" in e for e in report.errors),
            report.errors,
        )

    def test_an_edited_key_evidence_list_is_detected(self):
        self._append()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE verification_audits SET key_evidence = ? WHERE seq = 1",
                (json.dumps(["checks_passed=False"]),),
            )
        report = verify_ledger(self.db)
        self.assertFalse(report.ok)

    def test_a_removed_record_is_detected(self):
        self._append()
        self._append(attempts=2)
        self._append(attempts=3)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "DELETE FROM verification_audits WHERE id = ?", (self._second_id(),)
            )
        report = verify_ledger(self.db)
        self.assertFalse(report.ok)

    def test_chain_for_returns_the_records_for_one_work_package(self):
        self._append(work_package_id="dwp-a")
        self._append(work_package_id="dwp-b")
        self._append(work_package_id="dwp-a")
        records = chain_for(self.db, "dwp-a")
        self.assertEqual(len(records), 2)

    def test_report_summary_is_readable(self):
        self._append()
        report = verify_ledger(self.db)
        self.assertIn("Audit chain valid", report.summary())
        self.assertIsInstance(report, ChainReport)


if __name__ == "__main__":
    unittest.main()
