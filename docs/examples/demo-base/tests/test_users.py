"""Acceptance tests for the users registration route."""
from app.routes.users import get_user


def test_get_user_returns_none_for_unknown_user():
    assert get_user("nobody") is None
