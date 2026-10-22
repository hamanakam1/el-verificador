"""Tests for the content-addressed evidence store.

"Every decision cites evidence" is only true if the evidence still exists. A
hash that points at bytes nobody kept is a receipt, not a proof.
"""
import json
import tempfile
import unittest
from pathlib import Path

from el_verificador.evidence import (
    EvidenceKind,
    EvidenceStore,
    summarise_diff,
)
from el_verificador.schemas import ASTChange, ChangeType, SemanticDiff, SymbolKind


class TestEvidenceStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(self._tmp.name)

    def test_an_artifact_is_addressed_by_its_hash(self):
        artifact = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        self.assertEqual(len(artifact.sha256), 64)
        self.assertEqual(artifact.sha256, artifact.id)
        self.assertTrue(Path(artifact.path).exists())
        self.assertEqual(artifact.bytes, 5)

    def test_identical_bytes_land_in_the_same_place(self):
        first = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        second = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        self.assertEqual(first.path, second.path)

    def test_different_bytes_land_in_different_places(self):
        first = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        second = self.store.write_text(EvidenceKind.CHECK_STDOUT, "goodbye")
        self.assertNotEqual(first.path, second.path)

    def test_an_artifact_verifies(self):
        artifact = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        self.assertTrue(self.store.verify(artifact))
        self.assertEqual(self.store.verify_all([artifact]), [])

    def test_tampering_is_detected(self):
        artifact = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        Path(artifact.path).write_text("tampered")
        self.assertFalse(self.store.verify(artifact))
        self.assertEqual(self.store.verify_all([artifact]), [artifact.sha256])

    def test_a_missing_artifact_is_detected(self):
        artifact = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        Path(artifact.path).unlink()
        self.assertFalse(self.store.verify(artifact))

    def test_json_artifacts_round_trip(self):
        artifact = self.store.write_json(
            EvidenceKind.CHECK_RESULT, {"exit_code": 1, "command": "pytest -q"}
        )
        payload = json.loads(Path(artifact.path).read_text())
        self.assertEqual(payload["exit_code"], 1)
        self.assertTrue(artifact.verify())

    def test_the_store_is_created_if_it_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nested" / "store"
            store = EvidenceStore(str(root))
            self.assertTrue(root.is_dir())
            store.write_text(EvidenceKind.CHECK_STDOUT, "x")

    def test_an_artifact_can_be_looked_up_by_digest(self):
        artifact = self.store.write_text(EvidenceKind.CHECK_STDOUT, "hello")
        found = self.store.get(artifact.sha256)
        self.assertIsNotNone(found)
        self.assertEqual(found.sha256, artifact.sha256)

    def test_a_digest_that_was_never_stored_is_not_found(self):
        self.assertIsNone(self.store.get("f" * 64))


class TestSummariseDiff(unittest.TestCase):
    def test_the_summary_is_json_serialisable(self):
        diff = SemanticDiff(
            repo_path="/tmp/repo",
            base_commit="a" * 40,
            head_commit="b" * 40,
            files_changed=["app/api.py"],
            ast_changes=[
                ASTChange(
                    change_type=ChangeType.MODIFIED,
                    symbol_kind=SymbolKind.FUNCTION,
                    name="create_user_route",
                    file_path="app/api.py",
                    line_no=3,
                )
            ],
            stub_symbols=["app/api.py::stub"],
            weakened_tests=["tests/test_api.py::test_bad"],
        )
        summary = summarise_diff(diff)
        text = json.dumps(summary)
        self.assertIn("create_user_route", text)
        self.assertIn("weakened_tests", text)
        self.assertEqual(summary["ast_changes"][0]["change_type"], "modified")


if __name__ == "__main__":
    unittest.main()
