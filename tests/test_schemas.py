"""Tests for El Verificador data contracts."""
import json
import unittest

from el_verificador.schemas import (
    CheckExecution,
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
        self.assertEqual(config.scope_mode, "warn")

    def test_custom(self):
        config = VerificationConfig(max_retries=5, dry_run=True)
        self.assertEqual(config.max_retries, 5)
        self.assertTrue(config.dry_run)

    def test_paths_are_expanded(self):
        """`model_post_init` runs; the old `__post_init__` never did."""
        config = VerificationConfig(edgerecord_db="~/x/ledger.sqlite")
        self.assertNotIn("~", config.edgerecord_db)
        self.assertTrue(config.edgerecord_db.endswith("/x/ledger.sqlite"))

    def test_check_fixture_is_expanded(self):
        config = VerificationConfig(check_fixture="~/checks.json")
        self.assertNotIn("~", config.check_fixture)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(Exception):
            VerificationConfig.model_validate({"nonsense": 1})


class TestCheckResult(unittest.TestCase):
    def test_a_skipped_check_never_passes(self):
        result = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            execution=CheckExecution.SKIPPED,
        )
        self.assertFalse(result.passed)
        self.assertFalse(result.executed)

    def test_a_replayed_check_can_pass(self):
        result = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=0,
            execution=CheckExecution.REPLAY,
        )
        self.assertTrue(result.passed)
        self.assertTrue(result.executed)

    def test_a_ran_check_that_failed_does_not_pass(self):
        result = CheckResult(
            kind=CheckKind.UNIT_TESTS,
            command="pytest -q",
            exit_code=1,
            execution=CheckExecution.CONTAINER,
        )
        self.assertFalse(result.passed)


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

    def test_acceptance_criteria_are_kept(self):
        """The acceptance contract used to be dropped on the floor."""
        pkg = IntentPackage.from_dict({
            "id": "dwp-abc",
            "true_goal": "Implement the user login API route",
            "true_goal_summary": "Add login route",
            "acceptance_criteria": "pytest -q and ruff check .",
            "acceptance_commands": ["pytest -q", "ruff check ."],
        })
        self.assertEqual(pkg.acceptance_criteria, "pytest -q and ruff check .")
        self.assertEqual(pkg.acceptance_commands, ["pytest -q", "ruff check ."])


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

    def test_render_lists_violated_obligations(self):
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Build login route",
            task_goal_summary="login route",
            violations=[
                {
                    "id": "no_stub",
                    "kind": "no_stub",
                    "statement": "No changed symbol is a placeholder",
                    "status": "violated",
                    "detail": "stub symbol(s): login",
                }
            ],
        )
        rendered = prompt.render()
        self.assertIn("Obligations Not Satisfied", rendered)
        self.assertIn("no_stub", rendered)
        self.assertIn("stub symbol(s): login", rendered)

    def test_render_includes_the_narrative(self):
        prompt = CorrectionPrompt(
            attempt=1,
            max_retries=3,
            remaining_attempts=2,
            task_goal="Build login route",
            task_goal_summary="login route",
            narrative="The route body is a placeholder.",
        )
        self.assertIn("The route body is a placeholder.", prompt.render())


class TestSemanticDiff(unittest.TestCase):
    def _diff(self, **kwargs):
        base = dict(
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
        base.update(kwargs)
        return SemanticDiff(**base)

    def test_aligned_when_files_and_no_stubs(self):
        self.assertTrue(self._diff().aligned)

    def test_not_aligned_with_stubs(self):
        self.assertFalse(self._diff(has_todo_stubs=True, stub_symbols=["create_user"]).aligned)

    def test_not_aligned_with_weakened_tests(self):
        self.assertFalse(self._diff(weakened_tests=["tests/test_x.py::test_a"]).aligned)


class TestVerificationAudit(unittest.TestCase):
    def _audit(self, **kwargs):
        base = dict(
            work_package_id="dwp-abc",
            work_package_preview="preview",
            repo_path="/tmp/repo",
            decision=VerificationDecision.PASS,
            attempts=1,
            passed=True,
            committed=True,
            rationale="All obligations were satisfied for the work package",
            key_evidence=["checks_passed=True"],
        )
        base.update(kwargs)
        return VerificationAudit(**base)

    def test_compute_hash_is_sha256(self):
        import hashlib
        audit = self._audit()
        h = audit.compute_hash()
        expected = hashlib.sha256(audit.hashable_body().encode("utf-8")).hexdigest()
        self.assertEqual(h, expected)
        self.assertEqual(len(h), 64)
        int(h, 16)

    def test_the_hash_covers_the_evidence(self):
        """A hash that ignores the evidence attests to a sentence, not a proof."""
        first = self._audit(evidence_hashes=["a" * 64])
        second = self._audit(evidence_hashes=["b" * 64])
        self.assertNotEqual(first.compute_hash(), second.compute_hash())

    def test_the_hash_covers_the_obligations(self):
        first = self._audit(obligations=[{"id": "no_stub", "status": "satisfied"}])
        second = self._audit(obligations=[{"id": "no_stub", "status": "violated"}])
        self.assertNotEqual(first.compute_hash(), second.compute_hash())

    def test_the_hash_covers_the_repo_and_decision(self):
        first = self._audit()
        second = self._audit(decision=VerificationDecision.FAIL, passed=False)
        self.assertNotEqual(first.compute_hash(), second.compute_hash())


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

    def test_blocking_obligations(self):
        result = VerificationResult(
            work_package_id="dwp-abc",
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            decision=VerificationDecision.FAIL,
            obligations=[
                {"id": "no_stub", "status": "violated"},
                {"id": "change_present", "status": "satisfied"},
            ],
        )
        self.assertEqual([o["id"] for o in result.blocking_obligations], ["no_stub"])


class TestEdgeRecordLogger(unittest.TestCase):
    def _logger(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return EdgeRecordLogger(db_path=path)

    def _audit(self, **kwargs):
        base = dict(
            work_package_id="dwp-abc",
            work_package_preview="preview",
            repo_path="/tmp/repo",
            decision=VerificationDecision.PASS,
            attempts=1,
            passed=True,
            committed=True,
            rationale="All obligations were satisfied for the work package",
            key_evidence=["evidence"],
        )
        base.update(kwargs)
        return VerificationAudit(**base)

    def test_log_and_verify_chain(self):
        logger = self._logger()
        aid = logger.log_decision(self._audit())
        self.assertIsNotNone(aid)
        self.assertTrue(logger.verify_chain())
        chain = logger.get_audit_chain("dwp-abc")
        self.assertEqual(len(chain), 1)
        logger.close()

    def test_the_chain_links_every_record(self):
        logger = self._logger()
        logger.log_decision(self._audit())
        second = self._audit(decision=VerificationDecision.FAIL, passed=False)
        logger.log_decision(second)
        self.assertTrue(logger.verify_chain())
        chain = logger.get_audit_chain("dwp-abc")
        self.assertEqual(len(chain), 2)
        self.assertIsNone(chain[0]["previous_audit_hash"])
        self.assertEqual(chain[1]["previous_audit_hash"], chain[0]["content_hash"])
        logger.close()

    def test_the_stored_hash_matches_the_evidence(self):
        logger = self._logger()
        audit = self._audit(evidence_hashes=["c" * 64])
        logger.log_decision(audit)
        chain = logger.get_audit_chain("dwp-abc")
        self.assertEqual(chain[0]["content_hash"], audit.compute_hash())
        self.assertEqual(json.loads(chain[0]["evidence_hashes"]), ["c" * 64])
        logger.close()

    def test_tampering_is_detected(self):
        logger = self._logger()
        logger.log_decision(self._audit())
        logger.log_decision(self._audit(decision=VerificationDecision.FAIL, passed=False))
        # Rewrite a stored hash behind the logger's back.
        logger.conn.execute(
            "UPDATE verification_audits SET content_hash='deadbeef' WHERE seq=1"
        )
        logger.conn.commit()
        self.assertFalse(logger.verify_chain())
        logger.close()

    def test_a_legacy_ledger_is_migrated(self):
        """An existing ledger keeps its rows and gains a contiguous sequence."""
        import os
        import sqlite3
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        legacy = sqlite3.connect(path)
        legacy.execute("""
            CREATE TABLE verification_audits (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                work_package_id TEXT NOT NULL, work_package_preview TEXT,
                repo_path TEXT NOT NULL, decision TEXT NOT NULL,
                attempts INTEGER NOT NULL, passed INTEGER NOT NULL,
                committed INTEGER NOT NULL, rationale TEXT, key_evidence TEXT,
                content_hash TEXT NOT NULL, previous_audit_hash TEXT,
                total_duration_ms INTEGER, model_calls INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        legacy.execute(
            "INSERT INTO verification_audits VALUES "
            "('old-1','2026-01-01','dwp-old','p','/tmp/r','pass',1,1,1,'r','[]','"
            + "a" * 64 + "',NULL,0,0,'2026-01-01')"
        )
        legacy.commit()
        legacy.close()

        logger = EdgeRecordLogger(db_path=path)
        logger.log_decision(self._audit())
        self.assertTrue(logger.verify_chain())
        chain = logger.get_audit_chain("dwp-old")
        self.assertEqual(chain[0]["seq"], 1)
        logger.close()


if __name__ == "__main__":
    unittest.main()
