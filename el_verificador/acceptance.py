"""
The acceptance contract: which commands decide whether the work is done.

The default contract is three Python commands. That is the right default for
a Python repository and the wrong one for everything else — a Go project
verified with `pytest -q` is not verified at all, and the verdict is about
the tool rather than about the work.

So the work package may carry its own contract, and the verifier runs it.
Two shapes are understood:

- an explicit list, under `acceptance_commands`;
- a fenced code block inside the acceptance criteria, which is how a ticket
  written for a human usually states the commands to run.

When the work package carries no contract, the configured default is used.
Which one was used is recorded, because a decision that does not say what it
measured cannot be reviewed.
"""
from __future__ import annotations

import re
from typing import Any

# A fenced block, optionally tagged with a language.
_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)

# A command line, one per line. A line that is a comment, a heading, or a
# continuation of the previous command is not a command.
_NOT_A_COMMAND = re.compile(r"^\s*(#|//|<!--|\$|>|\||$)")

MAX_COMMANDS = 16
MAX_COMMAND_LENGTH = 500


def _clean(line: str) -> str:
    return line.strip().rstrip(";").strip()


def _from_code_block(text: str) -> list[str]:
    """Commands from the first fenced code block in a piece of prose."""
    match = _FENCE.search(text or "")
    if not match:
        return []
    commands: list[str] = []
    for line in match.group(1).splitlines():
        if _NOT_A_COMMAND.match(line):
            continue
        command = _clean(line)
        if not command or len(command) > MAX_COMMAND_LENGTH:
            continue
        commands.append(command)
        if len(commands) >= MAX_COMMANDS:
            break
    return commands


def _criteria_of(data: dict[str, Any]) -> list[str]:
    """The acceptance criteria, wherever the work package put them."""
    criteria: list[str] = []
    for key in ("acceptance_criteria", "acceptance"):
        value = data.get(key)
        if isinstance(value, str):
            criteria.append(value)
        elif isinstance(value, list):
            criteria.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    packet = data.get("context_packet")
    if isinstance(packet, dict):
        for key in ("acceptance_criteria", "acceptance"):
            value = packet.get(key)
            if isinstance(value, str):
                criteria.append(value)
            elif isinstance(value, list):
                criteria.extend(
                    str(item) for item in value if isinstance(item, (str, int, float))
                )
    return criteria


def _explicit_commands(data: dict[str, Any]) -> list[str]:
    """Commands the work package listed outright."""
    commands: list[str] = []
    for source in (data, data.get("context_packet") or {}):
        if not isinstance(source, dict):
            continue
        value = source.get("acceptance_commands")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, str):
                continue
            command = _clean(item)
            if command and len(command) <= MAX_COMMAND_LENGTH:
                commands.append(command)
        if commands:
            break
    return commands[:MAX_COMMANDS]


def acceptance_contract(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The commands and the criteria a work package declares.

    Returns the commands first, then the criteria. An empty command list
    means the work package declared no contract, and the caller falls back to
    the configured default.
    """
    criteria = _criteria_of(data)
    commands = _explicit_commands(data)
    if commands:
        return commands, criteria
    for criterion in criteria:
        commands = _from_code_block(criterion)
        if commands:
            break
    return commands, criteria
