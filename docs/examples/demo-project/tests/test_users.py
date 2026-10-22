"""Acceptance tests for the users registration route."""
from app.routes.users import (
    create_user,
    get_user,
    hash_password,
    register_user,
    validate_payload,
)


def test_validate_payload_rejects_short_username():
    errors, payload = validate_payload({"username": "ab", "password": "correcthorse"})
    assert payload is None
    assert any("username" in err for err in errors)


def test_validate_payload_accepts_good_payload():
    errors, payload = validate_payload(
        {"username": "ada", "password": "correcthorse", "email": "ada@example.com"}
    )
    assert errors == []
    assert payload is not None
    assert payload.username == "ada"


def test_hash_password_is_deterministic():
    first = hash_password("correcthorse")
    second = hash_password("correcthorse")
    assert first == second
    assert first != "correcthorse"


def test_register_user_creates_active_record():
    status, body = register_user(
        {"username": "grace", "password": "correcthorse", "email": "grace@example.com"}
    )
    assert status == 201
    assert body["username"] == "grace"
    assert body["active"] is True


def test_register_user_rejects_invalid_payload():
    status, body = register_user({"username": "x", "password": "y"})
    assert status == 422
    assert body["errors"]


def test_get_user_returns_registered_record():
    payload = validate_payload({"username": "linus", "password": "correcthorse"})[1]
    assert payload is not None
    create_user(payload)
    record = get_user("linus")
    assert record is not None
    assert record.username == "linus"
