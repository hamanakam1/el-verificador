from app.api import list_users


def test_list_users():
    assert list_users() == []
