"""
Metrics — how the gate is actually behaving.

A verifier that blocks good work gets switched off, and a verifier that
blocks nothing is theatre. Both failure modes are invisible without numbers,
so these numbers are computed from the receipt chain rather than asserted.

Everything here is derived from the durable record. Nothing is estimated, and
nothing is a judgement about the verifier itself: the metrics describe the
work the verifier saw, not how good the verifier is.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .receipt import ReceiptChain
from .schemas import VerificationConfig

# The receipt outcome vocabulary, mapped to the verification decisions.
_OUTCOMES = ("success", "failed", "blocked", "partial")


@dataclass
class VerificationMetrics:
    """The shape of the report."""
    total: int = 0
    outcomes: Dict[str, int] = field(default_factory=dict)
    work_packages: int = 0
    tickets_flagged: int = 0
    flagged_delivered: int = 0
    by_obligation: Dict[str, int] = field(default_factory=dict)
    by_work_package: List[Dict[str, Any]] = field(default_factory=list)
    first_at: Optional[str] = None
    last_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        pass_rate = (
            round(self.outcomes.get("success", 0) / self.total, 4)
            if self.total
            else 0.0
        )
        escalate_rate = (
            round(self.outcomes.get("blocked", 0) / self.total, 4)
            if self.total
            else 0.0
        )
        return {
            "total_verifications": self.total,
            "work_packages": self.work_packages,
            "outcomes": dict(self.outcomes),
            "pass_rate": pass_rate,
            "escalation_rate": escalate_rate,
            "inconclusive_rate": (
                round(self.outcomes.get("partial", 0) / self.total, 4)
                if self.total
                else 0.0
            ),
            "blocking_obligations": dict(self.by_obligation),
            "tickets_flagged": self.tickets_flagged,
            "flags_delivered": self.flagged_delivered,
            "first_verification_at": self.first_at,
            "last_verification_at": self.last_at,
            "recent": self.by_work_package,
        }


def collect(config: Optional[VerificationConfig] = None) -> VerificationMetrics:
    """Compute the metrics from the receipt chain."""
    config = config or VerificationConfig()
    chain = ReceiptChain(config.receipt_dir)
    receipts = chain.receipts()

    metrics = VerificationMetrics()
    metrics.outcomes = {outcome: 0 for outcome in _OUTCOMES}

    work_packages = set()
    per_package: Dict[str, Dict[str, Any]] = {}
    obligation_counts: "Counter[str]" = Counter()

    for receipt in receipts:
        metrics.total += 1
        outcome = str(receipt.get("outcome", "partial"))
        metrics.outcomes[outcome] = metrics.outcomes.get(outcome, 0) + 1

        package = str(receipt.get("work_package_id", ""))
        if package:
            work_packages.add(package)
            entry = per_package.setdefault(
                package,
                {
                    "work_package_id": package,
                    "verifications": 0,
                    "outcomes": Counter(),
                    "last_outcome": None,
                },
            )
            entry["verifications"] += 1
            entry["outcomes"][outcome] += 1
            entry["last_outcome"] = outcome

        for change in receipt.get("detected_changes", []) or []:
            kind = str(change).split(":", 1)[0].strip()
            if kind:
                obligation_counts[kind] += 1

        board = receipt.get("ticket_board")
        if isinstance(board, dict) and board.get("delivered"):
            metrics.flagged_delivered += 1

    metrics.work_packages = len(work_packages)
    metrics.by_obligation = dict(obligation_counts.most_common())

    if receipts:
        metrics.first_at = receipts[0].get("created_at")
        metrics.last_at = receipts[-1].get("created_at")

    for entry in per_package.values():
        metrics.by_work_package.append(
            {
                "work_package_id": entry["work_package_id"],
                "verifications": entry["verifications"],
                "last_outcome": entry["last_outcome"],
                "outcomes": dict(entry["outcomes"]),
            }
        )
    metrics.by_work_package.sort(
        key=lambda e: e["work_package_id"], reverse=True
    )
    metrics.by_work_package = metrics.by_work_package[:20]
    return metrics


__all__ = ["VerificationMetrics", "collect"]
