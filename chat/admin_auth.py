"""Check the admin password. The same values as NGINX: ADMIN_USER and ADMIN_PASSWORD in .env."""

import base64
import secrets


def basic_username(header: str | None) -> str:
    """User name from a Basic header. The password is never returned."""
    if not header or not header.startswith("Basic "):
        return ""
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""
    user, separator, _password = decoded.partition(":")
    if not separator:
        return ""
    return user


def basic_auth_matches(header: str | None, user: str, password: str) -> bool:
    """True when the header is Basic auth for this user and password."""
    if not header or not header.startswith("Basic ") or not user or not password:
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return False
    got_user, separator, got_password = decoded.partition(":")
    if not separator:
        return False
    if len(got_user) != len(user) or len(got_password) != len(password):
        return False
    return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_password, password)
