"""The admin password check. No server and no .env."""

from chat.admin_auth import basic_auth_matches, basic_username


def header(user: str, password: str) -> str:
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def test_missing_header_is_rejected():
    assert basic_auth_matches(None, "admin", "secret") is False
    assert basic_auth_matches("", "admin", "secret") is False


def test_wrong_password_is_rejected():
    assert basic_auth_matches(header("admin", "nope"), "admin", "secret") is False


def test_wrong_user_is_rejected():
    assert basic_auth_matches(header("other", "secret"), "admin", "secret") is False


def test_correct_password_is_accepted():
    assert basic_auth_matches(header("admin", "secret"), "admin", "secret") is True


def test_username_helper_does_not_return_the_password():
    assert basic_username(header("admin", "s3cret-value")) == "admin"
    assert basic_username(None) == ""
