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
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from el_verificador.receipt import (
    ReceiptChain,
    ReceiptChainError,
    artifact_records,
    build_receipt,
    canonical_bytes,
    fingerprint,
    outcome_for_decision,
    sort_keys,
    validate_receipt,
)

_ERA_TOOL_PATH = os.environ.get("EL_VERIFICADOR_FINGERPRINT_TOOL")
ERA_TOOL = Path(_ERA_TOOL_PATH) if _ERA_TOOL_PATH else None


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


def _append_process_batch(directory, worker_id, ready, start):
    """Spawn-safe writer; widen the old predecessor-read race deliberately."""
    chain = ReceiptChain(directory)
    read_receipts = chain._read_receipts

    def slow_read():
        records = read_receipts()
        time.sleep(0.01)
        return records

    ready.put(worker_id)
    if not start.wait(10):
        raise RuntimeError("parallel writer start timed out")
    with patch.object(chain, "_read_receipts", side_effect=slow_read):
        for index in range(3):
            chain.append(_receipt(receipt_id=f"worker-{worker_id}-{index}"))


def _exit_with_receipt_lock(directory, locked, exit_now):
    chain = ReceiptChain(directory)
    with chain._locked(exclusive=True):
        locked.set()
        if not exit_now.wait(10):
            raise RuntimeError("lock-holder exit timed out")
        os._exit(0)  # Abrupt exit bypasses context-manager cleanup.


def _stop_process(process):
    if process.is_alive():
        process.terminate()
    process.join(timeout=5)


def _append_thread_batch(directory):
    """Run threads in a killable child so a deadlock cannot hang the suite."""
    chain = ReceiptChain(directory)
    ready = Barrier(8)
    read_receipts = chain._read_receipts

    def slow_read():
        records = read_receipts()
        time.sleep(0.01)
        return records

    def append(index):
        ready.wait(timeout=5)
        return chain.append(_receipt(receipt_id=f"thread-{index}"))

    with patch.object(chain, "_read_receipts", side_effect=slow_read):
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert len(list(pool.map(append, range(8), timeout=10))) == 8


def _read_during_publication(directory):
    chain = ReceiptChain(directory)
    chain.append(_receipt(receipt_id="first"))
    publishing = Event()
    publish = Event()
    reading = Event()
    real_link = os.link

    def paused_publish(source, target):
        publishing.set()
        if not publish.wait(5):
            raise RuntimeError("publication timed out")
        real_link(source, target)

    def read():
        reading.set()
        return ReceiptChain(directory).receipts()

    with patch("el_verificador.receipt.os.link", side_effect=paused_publish):
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(chain.append, _receipt(receipt_id="second"))
            try:
                assert publishing.wait(5)
                reader = pool.submit(read)
                assert reading.wait(5)
                try:
                    reader.result(timeout=0.05)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("reader did not wait for publication")
            finally:
                publish.set()
            writer.result(timeout=5)
            assert [r["receipt_id"] for r in reader.result(timeout=5)] == ["first", "second"]


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

    @unittest.skipUnless(
        ERA_TOOL is not None and ERA_TOOL.is_file(),
        "set EL_VERIFICADOR_FINGERPRINT_TOOL to opt into the reference-tool integration",
    )
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
        self.addCleanup(self._tmp.cleanup)
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

    def test_unreadable_or_nonobject_receipts_are_not_silently_omitted(self):
        path = self.chain.append(_receipt(receipt_id="first"))
        for damaged in (b"{truncated", b"\xff", b"[]", b"null", b"42", b'"text"',
                        b'{"summary": "\\ud800"}'):
            with self.subTest(damaged=damaged):
                path.write_bytes(damaged)
                self.assertFalse(self.chain.verify_chain())
                with self.assertRaises(ReceiptChainError):
                    self.chain.receipts()
                with self.assertRaises(ReceiptChainError):
                    self.chain.append(_receipt(receipt_id="next"))
                self.assertEqual(path.read_bytes(), damaged)
                self.assertEqual({p.name for p in Path(self._tmp.name).iterdir()},
                                 {path.name, ".receipt-chain.lock"})

    def test_a_truncated_tail_cannot_hide_a_newer_verdict(self):
        first = self.chain.append(_receipt(receipt_id="first"))
        second = self.chain.append(_receipt(receipt_id="second", outcome="failed"))
        first_bytes = first.read_bytes()
        second.write_text("{truncated", encoding="utf-8")
        self.assertFalse(self.chain.verify_chain())
        with self.assertRaises(ReceiptChainError):
            self.chain.last()
        with self.assertRaises(ReceiptChainError):
            self.chain.append(_receipt(receipt_id="third"))
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_text(), "{truncated")
        self.assertFalse((Path(self._tmp.name) / "third.json").exists())

    def test_a_valid_json_tamper_blocks_append(self):
        path = self.chain.append(_receipt())
        record = json.loads(path.read_text())
        record["outcome"] = "failed"
        damaged = json.dumps(record)
        path.write_text(damaged)
        with self.assertRaises(ReceiptChainError):
            self.chain.append(_receipt(receipt_id="next"))
        self.assertEqual(path.read_text(), damaged)
        self.assertFalse(self.chain.verify_chain())

    def test_receipt_read_failure_blocks_verification_and_append(self):
        self.chain.append(_receipt())
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertFalse(self.chain.verify_chain())
            with self.assertRaises(ReceiptChainError):
                self.chain.append(_receipt(receipt_id="next"))

    def test_directory_read_failure_is_not_an_empty_valid_chain(self):
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertFalse(self.chain.verify_chain())
            with self.assertRaises(ReceiptChainError):
                self.chain.append(_receipt(receipt_id="next"))

    def test_duplicate_receipt_id_preserves_the_original_bytes(self):
        path = self.chain.append(_receipt(receipt_id="duplicate"))
        original = path.read_bytes()
        with self.assertRaisesRegex(ReceiptChainError, "already exists"):
            self.chain.append(_receipt(receipt_id="duplicate", outcome="failed"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual({p.name for p in Path(self._tmp.name).iterdir()},
                         {path.name, ".receipt-chain.lock"})
        self.assertTrue(self.chain.verify_chain())

    def test_invalid_receipt_ids_are_rejected_before_writing(self):
        for receipt_id in ("../outside", "/outside", "nested/name", "nested\\name",
                           ".", "..", "", "bad\x00name", "bad\nname"):
            with self.subTest(receipt_id=receipt_id), self.assertRaises(ReceiptChainError):
                self.chain.append(_receipt(receipt_id=receipt_id))
            self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_a_concurrent_destination_is_not_overwritten(self):
        destination = Path(self._tmp.name) / "racing.json"
        real_link = os.link

        def competing_writer(source, target):
            Path(target).write_bytes(b"concurrent receipt bytes")
            real_link(source, target)

        with patch("el_verificador.receipt.os.link", side_effect=competing_writer):
            with self.assertRaisesRegex(ReceiptChainError, "already exists"):
                self.chain.append(_receipt(receipt_id="racing"))
        self.assertEqual(destination.read_bytes(), b"concurrent receipt bytes")
        self.assertEqual({p.name for p in Path(self._tmp.name).iterdir()},
                         {destination.name, ".receipt-chain.lock"})

    def test_parallel_processes_publish_one_contiguous_chain(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        workers = [context.Process(target=_append_process_batch,
                                   args=(self._tmp.name, index, ready, start))
                   for index in range(4)]
        try:
            for worker in workers:
                worker.start()
                self.addCleanup(_stop_process, worker)
            self.assertEqual({ready.get(timeout=15) for _ in workers}, set(range(4)))
            start.set()
            for worker in workers:
                worker.join(timeout=15)
                self.assertFalse(worker.is_alive(), "parallel receipt writer hung")
                self.assertEqual(worker.exitcode, 0)
        finally:
            start.set()
            ready.close()
        records = self.chain.receipts()
        self.assertEqual([record["seq"] for record in records], list(range(1, 13)))
        self.assertEqual(len({record["receipt_id"] for record in records}), 12)
        self.assertTrue(self.chain.verify_chain())
        self.assertFalse(list(Path(self._tmp.name).glob("*.tmp")))

    def test_parallel_threads_using_the_same_instance_serialize(self):
        self._run_bounded_child(_append_thread_batch)
        self.assertEqual([r["seq"] for r in self.chain.receipts()], list(range(1, 9)))
        self.assertTrue(self.chain.verify_chain())

    def test_reader_waits_for_complete_publication(self):
        self._run_bounded_child(_read_during_publication)
        self.assertEqual([r["receipt_id"] for r in self.chain.receipts()], ["first", "second"])
        self.assertTrue(self.chain.verify_chain())

    def _run_bounded_child(self, target):
        worker = multiprocessing.get_context("spawn").Process(target=target,
                                                               args=(self._tmp.name,))
        worker.start()
        self.addCleanup(_stop_process, worker)
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive(), "threaded receipt scenario hung")
        self.assertEqual(worker.exitcode, 0)

    def test_process_exit_releases_the_persistent_lock(self):
        context = multiprocessing.get_context("spawn")
        locked = context.Event()
        exit_now = context.Event()
        worker = context.Process(target=_exit_with_receipt_lock,
                                 args=(self._tmp.name, locked, exit_now))
        worker.start()
        self.addCleanup(_stop_process, worker)
        self.assertTrue(locked.wait(10))
        lock_path = Path(self._tmp.name) / ".receipt-chain.lock"
        inode = lock_path.stat().st_ino
        exit_now.set()
        worker.join(timeout=10)
        self.assertEqual(worker.exitcode, 0)
        self.chain.append(_receipt())
        self.assertEqual(lock_path.stat().st_ino, inode)
        self.assertTrue(self.chain.verify_chain())

    def test_lock_failure_preserves_existing_receipts(self):
        path = self.chain.append(_receipt())
        original = path.read_bytes()
        with patch("fcntl.flock", side_effect=OSError("lock unavailable")):
            self.assertFalse(self.chain.verify_chain())
            with self.assertRaisesRegex(ReceiptChainError, "cannot acquire"):
                self.chain.append(_receipt(receipt_id="blocked"))
        self.assertEqual(path.read_bytes(), original)
        self.chain.append(_receipt(receipt_id="after-lock-failure"))
        self.assertTrue(self.chain.verify_chain())

    def test_lock_open_failure_refuses_unlocked_operations(self):
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertFalse(self.chain.verify_chain())
            with self.assertRaisesRegex(ReceiptChainError, "cannot open"):
                self.chain.append(_receipt())
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_existing_lock_can_be_opened_read_only_for_readers(self):
        self.chain.append(_receipt())
        real_open = Path.open

        def read_only_lock(path, mode="r", *args, **kwargs):
            if path.name == ".receipt-chain.lock" and mode != "rb":
                raise PermissionError("lock is read-only")
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", read_only_lock):
            self.assertTrue(self.chain.verify_chain())
            self.assertEqual(len(self.chain.receipts()), 1)
            with self.assertRaisesRegex(ReceiptChainError, "cannot open"):
                self.chain.append(_receipt(receipt_id="requires-write-access"))

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
