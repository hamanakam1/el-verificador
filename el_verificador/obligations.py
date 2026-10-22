"""
Obligations — the real intent-alignment mechanism for El Verificador.

The old design computed a single "intent coverage" number by counting how many
words of the work package's `true_goal` appeared in a set of symbol names that
was, by construction, empty for every healthy change. The number was
decorative, and no decision ever consulted it.

This module replaces it with **obligations**: individually checkable claims
derived from the distilled intent, each of which resolves to a status backed by
concrete evidence.

    VIOLATED       -> the change contradicts the intent
    UNPROVEN       -> the claim could not be established (no verdict)
    SATISFIED      -> the claim holds, with evidence
    NOT_APPLICABLE -> the intent does not constrain this dimension

The decision is a pure function of the obligation list:

    any VIOLATED   -> FAIL
    any UNPROVEN   -> INCONCLUSIVE
    otherwise      -> PASS

Every obligation here is evaluated **deterministically**. A local model may
propose obligations (see `proposer.py`); it never decides one.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import List, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    CheckResult,
    IntentPackage,
    SemanticDiff,
    VerificationDecision,
)

# Words that carry no intent. Deliberately small: a large stopword list would
# quietly remove real signal from short goals.
_STOPWORDS: Set[str] = {
    "add", "added", "adding", "also", "allow", "allows", "and", "any", "are",
    "build", "built", "but", "can", "code", "does", "each", "ensure",
    "ensures", "for", "from", "function", "functions", "have", "implement",
    "implemented", "implementing", "into", "its", "let", "lets", "make",
    "made", "method", "methods", "must", "need", "needs", "new", "not", "now",
    "off", "one", "only", "our", "out", "own", "per", "provide", "provided",
    "set", "should", "so", "some", "such", "support", "supported", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "through", "to", "up", "use", "used", "using", "very", "want",
    "wants", "was", "way", "we", "were", "what", "when", "which", "while",
    "will", "with", "would", "you", "your",
}


class ObligationKind(str, Enum):
    """The kind of claim an obligation makes."""
    CHANGE_PRESENT = "change_present"
    ACCEPTANCE_COMMAND = "acceptance_command"
    NO_STUB = "no_stub"
    TESTS_NOT_WEAKENED = "tests_intact"
    SCOPE_RESPECTED = "scope_respected"
    SYMBOL_PRESENT = "symbol"
    GOAL_TERMS_PRESENT = "goal_terms"


class ObligationStatus(str, Enum):
    """How an obligation resolved."""
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNPROVEN = "unproven"
    NOT_APPLICABLE = "not_applicable"


class Obligation(BaseModel):
    """One checkable claim about the change, with its resolution."""
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ObligationKind
    statement: str
    source: str = Field(default="intent", description="What produced this obligation")
    status: ObligationStatus
    detail: str = ""
    evidence_refs: List[str] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True when this obligation prevents a PASS."""
        return self.status in (
            ObligationStatus.VIOLATED,
            ObligationStatus.UNPROVEN,
        )


# --------------------------------------------------------------------------- #
# Intent extraction helpers
# --------------------------------------------------------------------------- #

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+(?:[A-Z][a-z0-9]*)*")


def significant_terms(text: str) -> List[str]:
    """Content words of a goal, lowercased, stopwords removed."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 3]


def identifiers_in(text: str) -> Set[str]:
    """Identifier-like tokens in a goal, normalised to snake_case.

    A goal that literally names `create_user_route` produces that identifier;
    a goal written in prose ("create user route") produces nothing, and the
    symbol obligation then declines to apply rather than guessing.
    """
    found: Set[str] = set()
    for token in _IDENTIFIER_RE.findall(text):
        if "_" in token and len(token) > 2:
            found.add(token.lower())
        elif token.isupper() and len(token) > 2:
            found.add(token.lower())
    for match in _CAMEL_RE.finditer(text):
        token = match.group(0)
        if any(ch.isupper() for ch in token[1:]) and len(token) > 3:
            found.add(re.sub(r"(?<!^)(?=[A-Z])", "_", token).lower())
    return found


def _variants(term: str) -> Set[str]:
    """Spellings of a term to look for in the change.

    A goal says "returns" and the code says "return"; a goal says
    "create_user_route" and the code says `createUserRoute`. Without these
    variants a correct implementation shares no vocabulary with its own goal
    and the goal obligation fails a change that did exactly what was asked.
    """
    out = {term}
    if term.endswith("s") and len(term) > 4:
        out.add(term[:-1])
    else:
        out.add(term + "s")
    if "_" in term:
        parts = term.split("_")
        out.add("".join(parts))
        out.add("".join(p.capitalize() for p in parts))
    else:
        out.add(term.replace(" ", "_"))
    return out


def _haystack(diff: SemanticDiff) -> str:
    """The text the change produced, lowercased, for term matching.

    Only the *after* side counts. The code that was there before is not what
    the change delivered, and a goal that happens to match the old
    implementation has not been addressed.
    """
    parts: List[str] = []
    for change in diff.ast_changes:
        parts.append(change.name.lower())
        if change.after:
            parts.append(change.after.lower())
    for path in diff.files_changed:
        parts.append(path.lower())
    return "\n".join(parts)


def _symbol_names(diff: SemanticDiff) -> Set[str]:
    names: Set[str] = set()
    for change in diff.ast_changes:
        names.add(change.name.lower())
    return names


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #

class ObligationEvaluator:
    """Builds and resolves the obligation set for one verification attempt."""

    def __init__(self, scope_mode: str = "warn"):
        # "off" | "warn" | "enforce"
        if scope_mode not in ("off", "warn", "enforce"):
            raise ValueError(f"scope_mode must be off, warn or enforce; got {scope_mode!r}")
        self.scope_mode = scope_mode

    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        intent: IntentPackage,
        diff: SemanticDiff,
        check_results: Sequence[CheckResult],
        acceptance_source: str = "config",
    ) -> List[Obligation]:
        obligations: List[Obligation] = []
        obligations.extend(self._change_present(diff))
        obligations.extend(self._acceptance_commands(check_results, acceptance_source))
        obligations.extend(self._no_stub(diff))
        obligations.extend(self._tests_intact(diff))
        obligations.extend(self._scope_respected(intent, diff))
        obligations.extend(self._symbol_present(intent, diff))
        obligations.extend(self._goal_terms(intent, diff))
        return obligations

    # ------------------------------------------------------------------ #
    def _change_present(self, diff: SemanticDiff) -> List[Obligation]:
        if diff.files_changed:
            return [
                Obligation(
                    id="change_present",
                    kind=ObligationKind.CHANGE_PRESENT,
                    statement="The work package produced a change in the repository",
                    status=ObligationStatus.SATISFIED,
                    detail=(
                        f"{len(diff.files_changed)} file(s) changed, "
                        f"{len(diff.ast_changes)} symbol(s) added or modified"
                    ),
                )
            ]
        return [
            Obligation(
                id="change_present",
                kind=ObligationKind.CHANGE_PRESENT,
                statement="The work package produced a change in the repository",
                status=ObligationStatus.VIOLATED,
                detail="no files differ between base and head",
            )
        ]

    def _acceptance_commands(
        self, check_results: Sequence[CheckResult], acceptance_source: str
    ) -> List[Obligation]:
        obligations: List[Obligation] = []
        for index, check in enumerate(check_results):
            command = check.command
            origin = f" (from {acceptance_source})"
            if not check.executed:
                status = ObligationStatus.UNPROVEN
                detail = f"`{command}` did not run, so it carries no result{origin}"
            elif check.passed:
                status = ObligationStatus.SATISFIED
                detail = f"`{command}` exited 0{origin}"
            else:
                status = ObligationStatus.VIOLATED
                detail = f"`{command}` exited {check.exit_code}{origin}"
            obligations.append(
                Obligation(
                    id=f"acceptance_command_{index}",
                    kind=ObligationKind.ACCEPTANCE_COMMAND,
                    statement=f"The acceptance command `{command}` succeeds",
                    source="acceptance_contract",
                    status=status,
                    detail=detail,
                )
            )
        return obligations

    def _no_stub(self, diff: SemanticDiff) -> List[Obligation]:
        if not diff.has_todo_stubs:
            return [
                Obligation(
                    id="no_stub",
                    kind=ObligationKind.NO_STUB,
                    statement="No changed symbol is a placeholder implementation",
                    status=ObligationStatus.SATISFIED,
                    detail="no structural stubs among the changed symbols",
                )
            ]
        names = ", ".join(diff.stub_symbols)
        return [
            Obligation(
                id="no_stub",
                kind=ObligationKind.NO_STUB,
                statement="No changed symbol is a placeholder implementation",
                status=ObligationStatus.VIOLATED,
                detail=f"stub symbol(s): {names}",
            )
        ]

    def _tests_intact(self, diff: SemanticDiff) -> List[Obligation]:
        if not diff.weakened_tests:
            return [
                Obligation(
                    id="tests_intact",
                    kind=ObligationKind.TESTS_NOT_WEAKENED,
                    statement="No test was deleted, skipped, or de-asserted to force a pass",
                    status=ObligationStatus.SATISFIED,
                    detail="no test weakening detected",
                )
            ]
        names = ", ".join(diff.weakened_tests)
        return [
            Obligation(
                id="tests_intact",
                kind=ObligationKind.TESTS_NOT_WEAKENED,
                statement="No test was deleted, skipped, or de-asserted to force a pass",
                status=ObligationStatus.VIOLATED,
                detail=f"weakened test(s): {names}",
            )
        ]

    def _scope_respected(
        self, intent: IntentPackage, diff: SemanticDiff
    ) -> List[Obligation]:
        if self.scope_mode == "off" or not intent.related_files:
            return [
                Obligation(
                    id="scope_respected",
                    kind=ObligationKind.SCOPE_RESPECTED,
                    statement="Only files related to the work package were changed",
                    status=ObligationStatus.NOT_APPLICABLE,
                    detail=(
                        "no related_files in the work package"
                        if not intent.related_files
                        else "scope enforcement disabled"
                    ),
                )
            ]

        allowed = {p for p in intent.related_files}
        changed = set(diff.files_changed)
        out_of_scope = sorted(
            path
            for path in changed
            if path not in allowed
            and not self._is_test_path(path)
            and not self._is_ancillary(path)
        )
        if not out_of_scope:
            return [
                Obligation(
                    id="scope_respected",
                    kind=ObligationKind.SCOPE_RESPECTED,
                    statement="Only files related to the work package were changed",
                    status=ObligationStatus.SATISFIED,
                    detail="every changed file is in the work package's related_files",
                )
            ]

        detail = "changed file(s) outside the work package's related_files: " + ", ".join(
            out_of_scope
        )
        if self.scope_mode == "warn":
            return [
                Obligation(
                    id="scope_respected",
                    kind=ObligationKind.SCOPE_RESPECTED,
                    statement="Only files related to the work package were changed",
                    status=ObligationStatus.SATISFIED,
                    detail="WARNING: " + detail,
                )
            ]
        return [
            Obligation(
                id="scope_respected",
                kind=ObligationKind.SCOPE_RESPECTED,
                statement="Only files related to the work package were changed",
                status=ObligationStatus.VIOLATED,
                detail=detail,
            )
        ]

    def _symbol_present(
        self, intent: IntentPackage, diff: SemanticDiff
    ) -> List[Obligation]:
        expected = identifiers_in(intent.true_goal) | identifiers_in(
            intent.true_goal_summary
        )
        if not expected:
            return [
                Obligation(
                    id="symbol_present",
                    kind=ObligationKind.SYMBOL_PRESENT,
                    statement="Every symbol named in the goal exists in the change",
                    status=ObligationStatus.NOT_APPLICABLE,
                    detail="the goal names no specific symbol",
                )
            ]
        present = _symbol_names(diff)
        missing = sorted(s for s in expected if s not in present)
        if not missing:
            return [
                Obligation(
                    id="symbol_present",
                    kind=ObligationKind.SYMBOL_PRESENT,
                    statement="Every symbol named in the goal exists in the change",
                    status=ObligationStatus.SATISFIED,
                    detail=f"found: {', '.join(sorted(expected))}",
                )
            ]
        return [
            Obligation(
                id="symbol_present",
                kind=ObligationKind.SYMBOL_PRESENT,
                statement="Every symbol named in the goal exists in the change",
                status=ObligationStatus.VIOLATED,
                detail=f"missing symbol(s): {', '.join(missing)}",
            )
        ]

    def _goal_terms(
        self, intent: IntentPackage, diff: SemanticDiff
    ) -> List[Obligation]:
        terms = significant_terms(intent.true_goal) + significant_terms(
            intent.true_goal_summary
        )
        unique = sorted(set(terms))
        if len(unique) < 2:
            return [
                Obligation(
                    id="goal_terms_present",
                    kind=ObligationKind.GOAL_TERMS_PRESENT,
                    statement="The change addresses the terms of the distilled goal",
                    status=ObligationStatus.NOT_APPLICABLE,
                    detail="the goal carries too few content terms to check",
                )
            ]

        haystack = _haystack(diff)
        unmatched = [
            term for term in unique if not any(v in haystack for v in _variants(term))
        ]
        if not unmatched:
            return [
                Obligation(
                    id="goal_terms_present",
                    kind=ObligationKind.GOAL_TERMS_PRESENT,
                    statement="The change addresses the terms of the distilled goal",
                    status=ObligationStatus.SATISFIED,
                    detail=f"all {len(unique)} goal term(s) appear in the change",
                )
            ]
        if len(unmatched) == len(unique):
            return [
                Obligation(
                    id="goal_terms_present",
                    kind=ObligationKind.GOAL_TERMS_PRESENT,
                    statement="The change addresses the terms of the distilled goal",
                    status=ObligationStatus.VIOLATED,
                    detail=(
                        "no term of the goal appears in the change; "
                        f"goal term(s): {', '.join(unique)}"
                    ),
                )
            ]
        return [
            Obligation(
                id="goal_terms_present",
                kind=ObligationKind.GOAL_TERMS_PRESENT,
                statement="The change addresses the terms of the distilled goal",
                status=ObligationStatus.SATISFIED,
                detail=f"goal term(s) not found in the change: {', '.join(unmatched)}",
            )
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.lower()
        return (
            lowered.startswith("test")
            or "/test" in lowered
            or "/tests/" in lowered
            or lowered.endswith("_test.py")
            or lowered.endswith("test.py")
            or "/conftest.py" in lowered
        )

    @staticmethod
    def _is_ancillary(path: str) -> bool:
        """Files that are legitimately touched alongside the goal."""
        lowered = path.lower()
        return lowered in {
            "readme.md", "changelog.md", "agents.md", "spec.md", "roadmap.md",
            "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt",
            "poetry.lock", "uv.lock", "package.json", "package-lock.json",
        } or lowered.startswith(".github/")


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #

def decide(obligations: Sequence[Obligation]) -> VerificationDecision:
    """Pure decision function over resolved obligations.

    Any violation fails the run. Any unproven obligation means no verdict was
    reached, which is not a failure and not a pass. Otherwise the change
    satisfies the intent.
    """
    for obligation in obligations:
        if obligation.status is ObligationStatus.VIOLATED:
            return VerificationDecision.FAIL
    for obligation in obligations:
        if obligation.status is ObligationStatus.UNPROVEN:
            return VerificationDecision.INCONCLUSIVE
    return VerificationDecision.PASS


def coverage(obligations: Sequence[Obligation]) -> float:
    """Fraction of applicable obligations that were satisfied."""
    applicable = [
        o
        for o in obligations
        if o.status is not ObligationStatus.NOT_APPLICABLE
    ]
    if not applicable:
        return 1.0
    satisfied = sum(1 for o in applicable if o.status is ObligationStatus.SATISFIED)
    return round(satisfied / len(applicable), 4)


def violations(obligations: Sequence[Obligation]) -> List[Obligation]:
    """Only the obligations that block a PASS."""
    return [
        o
        for o in obligations
        if o.status in (ObligationStatus.VIOLATED, ObligationStatus.UNPROVEN)
    ]


__all__ = [
    "Obligation",
    "ObligationEvaluator",
    "ObligationKind",
    "ObligationStatus",
    "coverage",
    "decide",
    "significant_terms",
    "identifiers_in",
    "violations",
]
