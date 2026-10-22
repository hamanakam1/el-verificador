"""
Ticket Board adapter — flagging an escalation for a human.

When verification escalates, the operator has to find out. The durable
receipt records the decision, but a receipt nobody reads is not a flag.

Two constraints shaped this adapter.

**The write is a record, never an authorization.** It adds the
`needs-human` tag and appends to the ticket's notes. It never changes the
ticket's status, because a status change can imply that work may proceed.

**The write is never attributed to the operator.** The dashboard's own
`/api/tix/update` route hardcodes `actor="operator"`, and the dashboard's
code carries a warning about exactly that: an untagged coordination post
landed on a ticket as `sender="Mike", role="human"`, data nobody owned. This
adapter therefore uses the workboard route, which records the change in the
case event log without claiming a person did it, and it says who it was.

The adapter is **off by default**. Turning it on is one config line; leaving
it off means the escalation is recorded in the receipt and nowhere else, which
is the safe default for a component writing to somebody else's board.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

# The tag the factory already uses for "a human needs to look at this".
NEEDS_HUMAN_TAG = "needs-human"


@dataclass
class FlagOutcome:
    """What happened when the escalation was flagged."""
    delivered: bool = False
    ticket: Optional[str] = None
    detail: str = ""
    fields_written: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "delivered": self.delivered,
            "ticket": self.ticket,
            "detail": self.detail,
            "fields_written": self.fields_written,
            "error": self.error,
        }


class TicketBoardAdapter:
    """Flags an escalation on the local dashboard's ticket board."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7878",
        timeout: int = 10,
        tag: str = NEEDS_HUMAN_TAG,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.tag = tag

    # ------------------------------------------------------------------ #
    def flag(
        self,
        ticket: str,
        reason: str,
        extra_tags: Sequence[str] = (),
    ) -> FlagOutcome:
        """Flag a ticket for human attention.

        Never raises. A board that is down, unreachable or refusing the write
        produces an outcome that says so, and the escalation is still
        recorded in the receipt.
        """
        outcome = FlagOutcome(ticket=ticket)
        if not ticket:
            outcome.error = "no ticket to flag"
            return outcome

        existing = self._read_ticket(ticket)
        if existing is None:
            outcome.error = (
                "the ticket board did not return the ticket; the escalation is "
                "recorded in the receipt only"
            )
            return outcome

        tags = list(existing.get("tags", []))
        for candidate in (self.tag, *extra_tags):
            if candidate and candidate not in tags:
                tags.append(candidate)

        fields: Dict[str, Any] = {"tags": tags}

        # Notes are replaced by the board's update, so they are only written
        # when the existing text was read and can be preserved. A write that
        # would wipe the operator's notes is not a write.
        notes = existing.get("notes")
        if isinstance(notes, str) and notes:
            fields["notes"] = (notes + "\n" + self._note(reason)).strip()[:4000]

        outcome.fields_written = fields

        try:
            response = self._post("/api/tickets/update", {"ticket": ticket, "fields": fields})
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"the ticket board write failed: {exc}"
            return outcome

        if response.get("ok"):
            outcome.delivered = True
            outcome.detail = f"flagged {ticket} with the {self.tag} tag"
            return outcome

        outcome.error = str(response.get("error", "the ticket board refused the write"))
        return outcome

    # ------------------------------------------------------------------ #
    def _read_ticket(self, ticket: str) -> Optional[Dict[str, Any]]:
        """The ticket's current tags and notes, so the write is additive.

        `None` means the board did not return the ticket, and the caller must
        not write anything that could replace what is already there.
        """
        payload = self._get(f"/api/tix/ticket?key={ticket}")
        if payload is None:
            # The Ticket Center route may not carry the board's own view.
            # Fall back to the workboard's read.
            payload = self._get(f"/api/tix?key={ticket}")
        if payload is None:
            return None

        body = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            body = payload if isinstance(payload, dict) else {}

        raw_tags = body.get("tags") or []
        if isinstance(raw_tags, str):
            tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
        else:
            tags = [str(part) for part in raw_tags]
        return {"tags": tags, "notes": body.get("notes", "")}

    def _note(self, reason: str) -> str:
        return f"El Verificador escalation: {reason}".strip()[:2000]

    # ------------------------------------------------------------------ #
    def _get(self, path: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # The board answers a refused write with a JSON body explaining
            # why. That explanation is more useful than the status line.
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(detail)
            except ValueError:
                return {"ok": False, "error": f"HTTP {exc.code}"}
            if isinstance(result, dict):
                result.setdefault("error", f"HTTP {exc.code}")
                return result
            return {"ok": False, "error": f"HTTP {exc.code}"}
        try:
            result = json.loads(body)
        except ValueError:
            return {"ok": False, "error": "the ticket board returned non-JSON"}
        return result if isinstance(result, dict) else {"ok": False}


__all__ = ["FlagOutcome", "NEEDS_HUMAN_TAG", "TicketBoardAdapter"]
