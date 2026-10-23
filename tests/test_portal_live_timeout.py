"""TC4 simulation over real HTTP: the client times out, but the server already stored the file."""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import httpx
import pytest
import uvicorn

from portal.app import create_app
from portal.config import PortalSettings
from tests.conftest import csrf_from, otp_from, pdf

SERVER_DELAY = 2.0
CLIENT_TIMEOUT = 0.5


@pytest.fixture
def live_portal(portal_settings: PortalSettings) -> Iterator[str]:
    """Run the portal on a free local port in a background thread."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(replace(portal_settings, fault_delay_seconds=SERVER_DELAY))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "portal did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=SERVER_DELAY + 5)


@pytest.mark.live
def test_timeout_after_store_client_times_out_but_file_is_listed(live_portal: str) -> None:
    html = {"accept": "text/html"}
    with httpx.Client(base_url=live_portal, follow_redirects=True, headers=html, timeout=5) as client:
        otp_page = client.post("/login", data={"username": "arjun", "password": "demo123"})
        claims = client.post("/otp", data={"otp": otp_from(otp_page.text)})
        csrf = csrf_from(claims.text)
        client.post("/admin/fault", data={"mode": "timeout_after_store", "once": "1", "csrf_token": csrf})

        data = pdf("TC4 live timeout")
        with pytest.raises(httpx.ReadTimeout):
            client.post(
                "/claims/CLM-2026-0142/documents",
                data={"requirement_id": "REQ-0142-01", "csrf_token": csrf},
                files={"file": ("pharmacy_invoice.pdf", data, "application/pdf")},
                timeout=CLIENT_TIMEOUT,
            )

        # What the agent must do next: re-read the document list before any retry.
        page = client.get("/claims/CLM-2026-0142").text
        assert f'data-sha256="{hashlib.sha256(data).hexdigest()}"' in page
