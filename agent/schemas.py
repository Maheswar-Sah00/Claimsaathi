"""Pydantic response schemas for the agent API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness: the process is up. Does not touch the database or the portal."""

    status: Literal["ok"]
    app: str
    version: str


class DependencyCheck(BaseModel):
    """Result of checking one dependency."""

    ok: bool
    detail: str


class ReadinessResponse(BaseModel):
    """Readiness: can the agent reach its database and the portal right now."""

    status: Literal["ready", "degraded"]
    database: DependencyCheck
    portal: DependencyCheck
    gemini_configured: bool
    browser: str
