"""
The acceptance contract — what "done" means for one work package.

The verifier used to run a hardcoded list of three commands
(`pytest -q`, `ruff check .`, `bandit -r . -q`) regardless of what the work
actually asked for. A ticket that asked for `npm test && npm run lint` was
"verified" by running pytest, which proves nothing about the ticket.

The acceptance contract is carried by the work package — `acceptance_commands`
when the distiller produced a list, `acceptance_criteria` when it produced
prose — and the ticket's own acceptance field is the fallback. The contract is
resolved once per run, and the resolution records which source it came from,
so the audit trail says whether the run checked the right thing or fell back
to the default.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .schemas import IntentPackage, VerificationConfig

# A command-shaped line: a known tool, optionally with arguments.
_COMMAND_START = re.compile(
    r"^\s*(pytest|py\.test|tox|nox|ruff|flake8|black|mypy|bandit|coverage|"
    r"npm|pnpm|yarn|bun|node|jest|vitest|mocha|go|cargo|make|just|gradle|"
    r"mvn|dotnet|rspec|phpunit|pytest\.ini|python\s+-m\s+pytest)\b"
)

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)


class AcceptanceSource(str, Enum):
    """Where the acceptance commands came from."""
    WORK_PACKAGE = "work_package"  # an explicit command list on the work package
    CRITERIA = "criteria"          # parsed out of the work package's criteria
    CONFIG = "config"              # the configured default


class AcceptanceContract(BaseModel):
    """The commands that must succeed for this work package to pass."""
    model_config = ConfigDict(extra="forbid")

    commands: List[str] = Field(default_factory=list)
    source: AcceptanceSource = AcceptanceSource.CONFIG
    criteria: Optional[str] = None
    notes: str = ""

    @property
    def is_default(self) -> bool:
        return self.source is AcceptanceSource.CONFIG


def extract_commands(criteria: str) -> List[str]:
    """Pull command lines out of a free-text acceptance criteria block.

    Only two shapes are accepted, both of which are unambiguous:

    1. a fenced code block, whose non-comment lines are commands;
    2. a bare line that starts with a known tool.

    Anything else yields nothing, and the caller falls back to the configured
    default. Guessing here would mean running commands the ticket never
    asked for.
    """
    if not criteria:
        return []

    commands: List[str] = []
    for block in _FENCE.findall(criteria):
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            commands.append(stripped)

    if not commands:
        for line in criteria.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                stripped = stripped[2:].strip()
            if _COMMAND_START.match(stripped) and " " in stripped or (
                _COMMAND_START.match(stripped) and len(stripped.split()) == 1
            ):
                commands.append(stripped)

    # Preserve order, drop duplicates.
    seen = set()
    unique: List[str] = []
    for command in commands:
        if command not in seen:
            seen.add(command)
            unique.append(command)
    return unique


def resolve_acceptance(
    intent: IntentPackage, config: VerificationConfig
) -> AcceptanceContract:
    """Resolve the acceptance contract for one work package.

    The order of preference is: an explicit command list on the work package,
    then commands parsed out of its acceptance criteria, then the configured
    default.
    """
    if intent.acceptance_commands:
        return AcceptanceContract(
            commands=list(intent.acceptance_commands),
            source=AcceptanceSource.WORK_PACKAGE,
            criteria=intent.acceptance_criteria,
            notes="command list carried by the work package",
        )

    if intent.acceptance_criteria:
        parsed = extract_commands(intent.acceptance_criteria)
        if parsed:
            return AcceptanceContract(
                commands=parsed,
                source=AcceptanceSource.CRITERIA,
                criteria=intent.acceptance_criteria,
                notes="commands parsed out of the work package's acceptance criteria",
            )

    return AcceptanceContract(
        commands=list(config.test_commands),
        source=AcceptanceSource.CONFIG,
        notes="the configured default; the work package carried no acceptance contract",
    )


__all__ = [
    "AcceptanceContract",
    "AcceptanceSource",
    "extract_commands",
    "resolve_acceptance",
]
