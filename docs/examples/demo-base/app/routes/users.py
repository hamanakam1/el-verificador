"""User registration routes.

This is the BASE (pre-implementation) revision of the module. The demo
repository's parent commit carries this stub so the semantic diff has a real
before/after pair to compare.
"""
from __future__ import annotations


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    raise NotImplementedError


def get_user(username: str) -> dict | None:
    """Look up a registered user by username."""
    return None
