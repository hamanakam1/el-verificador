from app.api import create_user_route, list_users


def test_list_users():
    assert list_users() == []


def test_create_user_route():
    assert create_user_route()["name"] == "ada"


def test_create_user_route_rejects_bad_input():
    with pytest.raises(ValueError):
        create_user_route({"name": ""})
