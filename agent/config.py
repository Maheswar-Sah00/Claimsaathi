"""Agent configuration, loaded from environment variables (and `.env` if present)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALID_BROWSERS = ("local", "browserbase")
_PLACEHOLDER_SECRET = "change-me-to-a-long-random-string"
logger = logging.getLogger("agent.config")


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets are held here but never logged or returned by an endpoint."""

    gemini_api_key: str | None
    gemini_model: str
    browser: str
    portal_base_url: str
    agent_base_url: str
    database_path: Path
    vault_dir: Path
    evidence_dir: Path
    portal_username: str
    portal_password: str
    portal_headless: bool
    portal_timeout_ms: int
    portal_upload_timeout_ms: int
    policy_pre_admission_days: int | None
    policy_post_admission_days: int | None
    log_level: str
    session_secret: str
    https_only_cookies: bool
    browserbase_api_key: str | None
    browserbase_project_id: str | None

    @property
    def gemini_configured(self) -> bool:
        """True when a Gemini key is set. Nothing calls the model at startup either way."""
        return bool(self.gemini_api_key)


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _optional_int(name: str) -> int | None:
    """An optional non-negative integer setting; unset or empty means 'not configured'."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be zero or more")
    return value


def load_settings() -> Settings:
    """Read settings from the environment. Raises ValueError on invalid values."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    browser = os.getenv("BROWSER", "local").strip().lower()
    if browser not in VALID_BROWSERS:
        raise ValueError(f"BROWSER must be one of {VALID_BROWSERS}, got {browser!r}")
    session_secret = os.getenv("AGENT_SESSION_SECRET", "")
    if not session_secret or session_secret == _PLACEHOLDER_SECRET:
        session_secret = secrets.token_urlsafe(32)
        logger.warning("AGENT_SESSION_SECRET not set; using a random secret (sessions reset on restart)")
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        browser=browser,
        portal_base_url=os.getenv("PORTAL_BASE_URL", "http://127.0.0.1:8001").rstrip("/"),
        agent_base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        database_path=_resolve(os.getenv("DATABASE_PATH", "data/claimsaathi.db")),
        vault_dir=_resolve(os.getenv("VAULT_DIR", "vault_samples")),
        evidence_dir=_resolve(os.getenv("EVIDENCE_DIR", "evidence")),
        portal_username=os.getenv("PORTAL_USERNAME", "arjun"),
        portal_password=os.getenv("PORTAL_PASSWORD", "demo123"),
        portal_headless=os.getenv("PORTAL_HEADLESS", "true").lower() != "false",
        portal_timeout_ms=int(os.getenv("PORTAL_TIMEOUT_MS", "15000")),
        portal_upload_timeout_ms=int(os.getenv("PORTAL_UPLOAD_TIMEOUT_MS", "10000")),
        policy_pre_admission_days=_optional_int("POLICY_PRE_ADMISSION_DAYS"),
        policy_post_admission_days=_optional_int("POLICY_POST_ADMISSION_DAYS"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        session_secret=session_secret,
        https_only_cookies=os.getenv("AGENT_HTTPS_ONLY_COOKIES", "false").lower() == "true",
        browserbase_api_key=os.getenv("BROWSERBASE_API_KEY") or None,
        browserbase_project_id=os.getenv("BROWSERBASE_PROJECT_ID") or None,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for the running process. Tests call `get_settings.cache_clear()`."""
    return load_settings()
