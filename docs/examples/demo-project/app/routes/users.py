"""User registration routes.

This is the HEAD (implemented) revision of the module. The parent commit of
the demo repository carries the stub version, so the semantic diff has a real
before/after pair to compare.
"""
from __future__ import annotations

import hashlib

from app.schemas import UserCreate, UserRecord

_REGISTRY: dict[str, UserRecord] = {}


def validate_payload(raw: dict) -> tuple[list[str], UserCreate | None]:
    """Validate an incoming registration payload.

    Returns the list of validation errors and, when the payload is valid,
    the parsed request object.
    """
    if not isinstance(raw, dict):
        return ["payload must be a JSON object"], None

    errors: list[str] = []
    username = str(raw.get("username", "")).strip()
    secret = str(raw.get("password", ""))
    email = str(raw.get("email", "")).strip()

    if len(username) < 3:
        errors.append("username must be at least 3 characters")
    if len(secret) < 8:
        errors.append("password must be at least 8 characters")
    if email and "@" not in email:
        errors.append("email must contain an @ sign")

    if errors:
        return errors, None
    return [], UserCreate(username=username, password=secret, email=email)


def hash_password(secret: str) -> str:
    """Hash a plaintext secret with SHA-256."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_user(payload: UserCreate) -> UserRecord:
    """Persist a validated user into the registry."""
    record = UserRecord(
        username=payload.username,
        password_hash=hash_password(payload.password),
        email=payload.email,
    )
    _REGISTRY[payload.username] = record
    return record


def register_user(raw: dict) -> tuple[int, dict]:
    """Handle POST /users/register."""
    errors, payload = validate_payload(raw)
    if errors:
        return 422, {"errors": errors}

    record = create_user(payload)
    return 201, {
        "username": record.username,
        "email": record.email,
        "active": record.active,
    }


def get_user(username: str) -> UserRecord | None:
    """Look up a registered user by username."""
    return _REGISTRY.get(username)
