"""Session login, CSRF tokens and response-format negotiation for the portal."""

from __future__ import annotations

import secrets

from fastapi import Request

from shared.errors import AppError

SESSION_USER = "user"
SESSION_OTP = "otp_challenge"
SESSION_CSRF = "csrf"


class NotAuthenticated(Exception):
    """Raised by `require_user`; browsers are redirected to /login, API clients get 401."""

    def __init__(self, next_path: str) -> None:
        super().__init__("login required")
        self.next_path = next_path


def wants_html(request: Request) -> bool:
    """True for browser-style requests; JSON is returned to everything else."""
    return "text/html" in request.headers.get("accept", "")


def require_user(request: Request) -> str:
    """FastAPI dependency: the signed-in username, or NotAuthenticated."""
    user = request.session.get(SESSION_USER)
    if not user:
        raise NotAuthenticated(request.url.path)
    return str(user)


def csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use."""
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(24)
        request.session[SESSION_CSRF] = token
    return str(token)


def check_csrf(request: Request, submitted: str | None) -> None:
    """Reject a state-changing form post whose token does not match the session's."""
    expected = request.session.get(SESSION_CSRF)
    if not expected or not submitted or not secrets.compare_digest(str(expected), submitted):
        raise AppError("csrf_failed", "Form expired or invalid. Reload the page and try again.", 403)


def safe_next(path: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/claims"


def start_session(request: Request, username: str) -> None:
    """Mark the session as signed in and rotate the CSRF token."""
    request.session.pop(SESSION_OTP, None)
    request.session[SESSION_USER] = username
    request.session[SESSION_CSRF] = secrets.token_urlsafe(24)
