"""Portal configuration from environment variables (and `.env` if present)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER_SECRET = "change-me-to-a-long-random-string"
logger = logging.getLogger("portal.config")


@dataclass(frozen=True)
class PortalSettings:
    """Portal runtime settings."""

    session_secret: str
    data_dir: Path
    max_upload_bytes: int
    fault_delay_seconds: float
    https_only_cookies: bool
    log_level: str

    @property
    def upload_dir(self) -> Path:
        """Root folder for stored uploads; every stored path must resolve inside it."""
        return self.data_dir / "uploads"


def load_portal_settings() -> PortalSettings:
    """Read settings. A missing or placeholder session secret is replaced by a random one per process."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    secret = os.getenv("PORTAL_SESSION_SECRET", "")
    if not secret or secret == _PLACEHOLDER_SECRET:
        secret = secrets.token_urlsafe(32)
        logger.warning("PORTAL_SESSION_SECRET not set; using a random secret (sessions reset on restart)")
    data_dir = Path(os.getenv("PORTAL_DATA_DIR", "data/portal"))
    return PortalSettings(
        session_secret=secret,
        data_dir=data_dir if data_dir.is_absolute() else PROJECT_ROOT / data_dir,
        max_upload_bytes=int(float(os.getenv("PORTAL_MAX_UPLOAD_MB", "5")) * 1024 * 1024),
        fault_delay_seconds=float(os.getenv("PORTAL_FAULT_DELAY_SECONDS", "30")),
        https_only_cookies=os.getenv("PORTAL_HTTPS_ONLY_COOKIES", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
