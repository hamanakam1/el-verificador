"""Tests for the Ticket Board adapter.

The adapter talks to the dashboard over HTTP, so these tests run a real HTTP
server on a loopback port and point the adapter at it. A mocked `urllib` would
not prove the request is well-formed.

Two properties matter most, and both are about not damaging somebody else's
board:

- the write is **additive** — the operator's existing tags and notes survive;
- the write is **never attributed to the operator**.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from el_verificador.ticket_board import NEEDS_HUMAN_TAG, TicketBoardAdapter


class _Board(BaseHTTPRequestHandler):
    """A stand-in for the dashboard's ticket endpoints."""

    ticket = {
        "ticket": 1234,
        "title": "Do the thing",
        "tags": ["no-auto", "backend"],
        "notes": "operator notes that must survive",
    }
    writes = []
    fail_writes = False

    def log_message(self, *args):  # noqa: A003
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/tix/ticket"):
            self._send(200, {"ok": True, "ticket": dict(type(self).ticket)})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        type(self).writes.append({"path": self.path, "payload": payload})
        if type(self).fail_writes:
            self._send(400, {"ok": False, "error": "bad status"})
            return
        self._send(200, {"ok": True, "ticket": payload.get("ticket")})


class TestTicketBoardAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Board)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Board.writes = []
        _Board.fail_writes = False
        _Board.ticket = {
            "ticket": 1234,
            "title": "Do the thing",
            "tags": ["no-auto", "backend"],
            "notes": "operator notes that must survive",
        }
        self.adapter = TicketBoardAdapter(base_url=self.base_url, timeout=5)

    def test_a_flag_adds_the_needs_human_tag(self):
        outcome = self.adapter.flag("1234", "verification exhausted")
        self.assertTrue(outcome.delivered, outcome.error)
        self.assertEqual(len(_Board.writes), 1)
        fields = _Board.writes[0]["payload"]["fields"]
        self.assertIn(NEEDS_HUMAN_TAG, fields["tags"])

    def test_the_existing_tags_survive(self):
        self.adapter.flag("1234", "verification exhausted")
        fields = _Board.writes[0]["payload"]["fields"]
        self.assertIn("no-auto", fields["tags"])
        self.assertIn("backend", fields["tags"])

    def test_the_operators_notes_survive(self):
        self.adapter.flag("1234", "verification exhausted")
        fields = _Board.writes[0]["payload"]["fields"]
        self.assertIn("operator notes that must survive", fields["notes"])
        self.assertIn("verification exhausted", fields["notes"])

    def test_the_write_never_changes_the_status(self):
        """A status change can imply that work may proceed."""
        self.adapter.flag("1234", "verification exhausted")
        fields = _Board.writes[0]["payload"]["fields"]
        self.assertNotIn("status", fields)
        self.assertNotIn("priority", fields)

    def test_the_write_is_not_attributed_to_the_operator(self):
        """The write must not claim a person made it.

        The dashboard carries a warning about an untagged coordination post
        landing on a ticket as `sender="Mike", role="human"`: data nobody
        owned. A pipeline write must not repeat that.
        """
        self.adapter.flag("1234", "verification exhausted")
        payload = _Board.writes[0]["payload"]
        self.assertNotIn("actor", payload)
        notes = payload["fields"].get("notes", "").lower()
        self.assertNotIn("by operator", notes)
        self.assertNotIn("sender", payload)
        # The write names the component that made it.
        self.assertIn("el verificador", notes)

    def test_an_unreachable_board_does_not_raise(self):
        adapter = TicketBoardAdapter(base_url="http://127.0.0.1:1", timeout=2)
        outcome = adapter.flag("1234", "verification exhausted")
        self.assertFalse(outcome.delivered)
        self.assertTrue(outcome.error)

    def test_a_refused_write_is_reported(self):
        _Board.fail_writes = True
        outcome = self.adapter.flag("1234", "verification exhausted")
        self.assertFalse(outcome.delivered)
        self.assertIn("bad status", outcome.error)

    def test_a_missing_ticket_is_reported(self):
        outcome = self.adapter.flag("", "verification exhausted")
        self.assertFalse(outcome.delivered)
        self.assertIn("no ticket", outcome.error)

    def test_the_outcome_is_serialisable(self):
        outcome = self.adapter.flag("1234", "verification exhausted")
        payload = outcome.as_dict()
        self.assertEqual(payload["ticket"], "1234")
        self.assertTrue(payload["delivered"])
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
