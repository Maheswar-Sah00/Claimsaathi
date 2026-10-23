"""Shared fixtures: isolated temp data dirs, a fast fault delay, and a signed-in portal client."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portal.app import create_app as create_portal
from portal.config import PortalSettings
from portal.seed import synthetic_pdf

HTML = {"accept": "text/html"}
JSON = {"accept": "application/json"}
FAST_DELAY = 0.3


@pytest.fixture
def portal_settings(tmp_path: Path) -> PortalSettings:
    """Portal settings pointing at a temp folder, with a short fault delay."""
    return PortalSettings(
        session_secret="test-secret-not-for-production",
        data_dir=tmp_path / "portal",
        max_upload_bytes=1024 * 1024,
        fault_delay_seconds=FAST_DELAY,
        https_only_cookies=False,
        log_level="WARNING",
    )


@pytest.fixture
def portal(portal_settings: PortalSettings) -> Iterator[TestClient]:
    """Unauthenticated portal client."""
    with TestClient(create_portal(portal_settings)) as client:
        yield client


def csrf_from(html: str) -> str:
    """Pull the CSRF token out of any rendered form."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token on page"
    return match.group(1)


def otp_from(html: str) -> str:
    """Read the code from the simulated SMS panel, the way a human would."""
    match = re.search(r'id="demo-sms-otp">(\d{6})<', html)
    assert match, "no demo SMS OTP on page"
    return match.group(1)


def login(client: TestClient) -> str:
    """Sign in with password + OTP; returns a CSRF token for later posts."""
    resp = client.post("/login", data={"username": "arjun", "password": "demo123"}, headers=HTML)
    assert resp.status_code == 200 and "demo-sms-panel" in resp.text
    resp = client.post("/otp", data={"otp": otp_from(resp.text)}, headers=HTML)
    assert resp.status_code == 200 and str(resp.url).endswith("/claims")
    return csrf_from(resp.text)


@pytest.fixture
def signed_in(portal: TestClient) -> tuple[TestClient, str]:
    """(client, csrf) for a signed-in session."""
    return portal, login(portal)


def pdf(label: str) -> bytes:
    """A small synthetic PDF with unique content."""
    return synthetic_pdf(["SYNTHETIC TEST FILE", label])


def upload(client: TestClient, csrf: str, data: bytes, filename: str = "pharmacy_invoice.pdf",
           claim_id: str = "CLM-2026-0142", req_id: str = "REQ-0142-01", headers: dict | None = None):
    """POST a multipart upload."""
    return client.post(
        f"/claims/{claim_id}/documents",
        data={"requirement_id": req_id, "csrf_token": csrf},
        files={"file": (filename, data, "application/octet-stream")},
        headers=headers or JSON,
    )


@pytest.fixture
def small_upload_settings(portal_settings: PortalSettings) -> PortalSettings:
    """Settings with a 2 KB upload cap, for size-limit tests."""
    return replace(portal_settings, max_upload_bytes=2048)


def allow_portal(conn, run_id: str) -> None:
    """Record the consent ALLOW that portal evidence in this run requires (see trg_evidence_requires_consent)."""
    from agent import repository as repo
    repo.record_decision(conn, run_id=run_id, kind="consent", result="ALLOW", reason="test: consent granted",
                         evidence_state="not_applicable")


@contextmanager
def serve(app, *, startup_timeout: float = 15.0) -> Iterator[str]:
    """Run an ASGI app under uvicorn on a free local port; yields its base URL."""
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + startup_timeout
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def matched(conn, run_id: str, requirement_id: str, sha256: str, vault_file_id: str | None = None) -> str:
    """Record the MATCHED match decision an upload proposal requires (see trg_actions_require_match)."""
    from agent import repository as repo
    return repo.record_decision(
        conn, run_id=run_id, kind="match", result="MATCHED", reason="test: matched", subject_ref=requirement_id,
        evidence_state="not_applicable",
        input_refs={"matched_sha256": sha256, "matched_vault_file_id": vault_file_id},
    ).id
