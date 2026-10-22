"""Tests for El Verificador data contracts."""
import json
import unittest

from el_verificador.schemas import (
    ASTChange,
    ChangeType,
    CheckKind,
    CheckResult,
    CorrectionPrompt,
    EdgeRecordLogger,
    IntentPackage,
    SemanticDiff,
    VerificationAudit,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)


class TestVerificationConfig(unittest.TestCase):
    def test_defaults(self):
        config = VerificationConfig()
        self.assertEqual(config.max_retries, 3)
        self.assertEqual(config.intent_coverage_threshold, 0.85)
        self.assertTrue(config.fail_on_todo_stubs)
        self.assertFalse(config.dry_run)

    def test_custom(self):
        config = VerificationConfig(max_retries=5, dry_run=True)
        self.assertEqual(config.max_retries, 5)
        self.assertTrue(config.dry_run)


class TestIntentPackage(unittest.TestCase):
    def test_from_dict(self):
        pkg = IntentPackage.from_dict({
            "id": "dwp-abc123",
            "true_goal": "Implement the user login API route",
            "true_goal_summary": "Add login route",
            "related_files": ["app/routes.py"],
            "estimated_tokens": 4000,
        })
        self.assertEqual(pkg.id, "dwp-abc123")
        self.assertEqual(pkg.true_goal, "Implement the user login API route")
        self.assertEqual(pkg.related_files, ["app/routes.py"])


class TestCorrectionPrompt(unittest.TestCase):
    def test_render_contains_sections(self):
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Build login route",
            task_goal_summary="login route",
            failed_checks=[],
            stack_traces=[],
            test_output="",
            semantic_diff=None,
        )
        rendered = prompt.render()
        self.assertIn("Correction Prompt", rendered)
        self.assertIn("Attempt 1 of 3", rendered)
        self.assertIn("Remaining retries: 2", rendered)
        self.assertIn("Build login route", rendered)


class TestSemanticDiff(unittest.TestCase):
    def test_aligned_when_files_and_no_stubs(self):
        diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/routes.py"],
            added_lines=10,
            removed_lines=2,
            ast_changes=[],
            intent_coverage=0.9,
            has_todo_stubs=False,
            stub_symbols=[],
        )
        self.assertTrue(diff.aligned)

    def test_not_aligned_with_stubs(self):
        diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/routes.py"],
            added_lines=10,
            removed_lines=0,
            ast_changes=[],
            intent_coverage=0.9,
            has_todo_stubs=True,
            stub_symbols=["create_user"],
        )
        self.assertFalse(diff.aligned)


class TestVerificationAudit(unittest.TestCase):
    def _audit(self, **overrides):
        payload = dict(
            work_package_id="dwp-abc",
            work_package_preview="preview",
            repo_path="/tmp/repo",
            decision=VerificationDecision.PASS,
            attempts=1,
            passed=True,
            committed=True,
            rationale="All acceptance checks passed and intent aligned",
            key_evidence=["checks_passed=True"],
        )
        payload.update(overrides)
        return VerificationAudit(**payload)

    def test_compute_hash_is_sha256(self):
        audit = self._audit()
        digest = audit.compute_hash()
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_the_hash_covers_the_evidence_not_only_the_sentence(self):
        """Changing anything the record says must change the hash."""
        base = self._audit().compute_hash()
        self.assertNotEqual(base, self._audit(key_evidence=["checks_passed=False"]).compute_hash())
        self.assertNotEqual(base, self._audit(rationale="Something else entirely").compute_hash())
        self.assertNotEqual(base, self._audit(attempts=3).compute_hash())
        self.assertNotEqual(base, self._audit(repo_path="/tmp/other").compute_hash())
        self.assertNotEqual(base, self._audit(passed=False).compute_hash())
        self.assertNotEqual(base, self._audit(committed=False).compute_hash())

    def test_the_hash_is_stable_for_the_same_content(self):
        from datetime import datetime

        first = self._audit()
        second = self._audit()
        second.id = first.id
        second.timestamp = datetime.fromisoformat(first.timestamp.isoformat())
        self.assertEqual(first.compute_hash(), second.compute_hash())

    def test_the_model_and_the_writer_agree_on_the_hash(self):
        """One definition. The writer must not recompute a second one."""
        import tempfile

        from el_verificador.schemas import EdgeRecordLogger

        audit = self._audit()
        model_hash = audit.content_hash
        with tempfile.TemporaryDirectory() as workdir:
            logger = EdgeRecordLogger(f"{workdir}/ledger.sqlite")
            logger.log_decision(audit)
            logger.close()
        self.assertEqual(audit.content_hash, model_hash)


class TestVerificationResult(unittest.TestCase):
    def test_passed_property(self):
        result = VerificationResult(
            work_package_id="dwp-abc",
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.PASS,
            passed=True,
        )
        self.assertTrue(result.passed)
        self.assertIsNone(result.final_attempt)

    def test_escalate_not_passed(self):
        result = VerificationResult(
            work_package_id="dwp-abc",
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.ESCALATE,
            passed=False,
        )
        self.assertFalse(result.passed)


class TestEdgeRecordLogger(unittest.TestCase):
    def test_log_and_verify_chain(self):
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            logger = EdgeRecordLogger(db_path=path)
            audit = VerificationAudit(
                work_package_id="dwp-abc",
                work_package_preview="preview",
                repo_path="/tmp/repo",
                decision=VerificationDecision.PASS,
                attempts=1,
                passed=True,
                committed=True,
                rationale="All acceptance checks passed and intent aligned",
                key_evidence=["evidence"],
            )
            aid = logger.log_decision(audit)
            self.assertIsNotNone(aid)
            self.assertTrue(logger.verify_chain())
            chain = logger.get_audit_chain("dwp-abc")
            self.assertEqual(len(chain), 1)
            logger.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
