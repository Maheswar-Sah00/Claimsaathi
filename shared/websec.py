"""Generic web-session helpers: response negotiation, CSRF tokens, safe redirects, demo OTP.

Used by the agent app. (The portal predates this module and keeps its own equivalents.)
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import Request

from shared.errors import AppError

SESSION_CSRF = "csrf"


class NotAuthenticated(Exception):
    """Raised by a `require_*` dependency; browsers are redirected to sign in, API clients get 401."""

    def __init__(self, next_path: str) -> None:
        super().__init__("sign-in required")
        self.next_path = next_path


def wants_html(request: Request) -> bool:
    """True for browser-style requests; JSON is returned to everything else."""
    return "text/html" in request.headers.get("accept", "")


def csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use."""
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(24)
        request.session[SESSION_CSRF] = token
    return str(token)


def rotate_csrf(request: Request) -> None:
    """Issue a new CSRF token (after sign-in)."""
    request.session[SESSION_CSRF] = secrets.token_urlsafe(24)


def check_csrf(request: Request, submitted: str | None) -> None:
    """Reject a state-changing request whose token does not match the session's."""
    expected = request.session.get(SESSION_CSRF)
    if not expected or not submitted or not secrets.compare_digest(str(expected), submitted):
        raise AppError("csrf_failed", "Form expired or invalid. Reload the page and try again.", 403)


def safe_next(path: str | None, default: str) -> str:
    """Only allow same-site relative redirects."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return default


OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5


@dataclass
class OtpChallenge:
    """A pending demo OTP, kept server-side so the code never travels in a cookie."""

    subject: str
    destination: str
    code: str
    expires_at: datetime
    attempts: int = 0


@dataclass
class OtpStore:
    """In-memory demo OTP challenges (single process)."""

    challenges: dict[str, OtpChallenge] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def new(self, subject: str, destination: str) -> str:
        """Create a challenge for `subject`, 'sent' to `destination`; returns the challenge id."""
        now = datetime.now(timezone.utc)
        challenge_id = secrets.token_urlsafe(16)
        with self.lock:
            self.challenges = {k: v for k, v in self.challenges.items() if v.expires_at > now}
            self.challenges[challenge_id] = OtpChallenge(
                subject, destination, f"{secrets.randbelow(1_000_000):06d}", now + OTP_TTL
            )
        return challenge_id

    def get(self, challenge_id: str | None) -> OtpChallenge | None:
        """A live challenge, or None if missing or expired."""
        challenge = self.challenges.get(challenge_id or "")
        if challenge is None or challenge.expires_at <= datetime.now(timezone.utc):
            return None
        return challenge

    def verify(self, challenge_id: str | None, code: str) -> str:
        """Check a typed code; returns the subject on success, raises AppError otherwise."""
        with self.lock:
            challenge = self.get(challenge_id)
            if challenge is None:
                raise AppError("otp_expired", "The OTP has expired. Please sign in again.", 401)
            challenge.attempts += 1
            if challenge.attempts > OTP_MAX_ATTEMPTS:
                self.challenges.pop(challenge_id or "", None)
                raise AppError("otp_locked", "Too many wrong attempts. Please sign in again.", 401)
            if not secrets.compare_digest(challenge.code, code.strip()):
                raise AppError("otp_invalid", f"Incorrect OTP. {OTP_MAX_ATTEMPTS - challenge.attempts} attempt(s) left.", 401)
            self.challenges.pop(challenge_id or "", None)
            return challenge.subject
