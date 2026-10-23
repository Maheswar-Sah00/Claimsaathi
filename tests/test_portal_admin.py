"""Demo admin: reset, status changes and every upload fault mode."""

from __future__ import annotations

import hashlib
import time

from fastapi.testclient import TestClient

from portal.config import PortalSettings
from tests.conftest import FAST_DELAY, HTML, JSON, pdf, upload


def set_fault(client: TestClient, csrf: str, mode: str, once: bool = True) -> None:
    data = {"mode": mode, "csrf_token": csrf}
    if once:
        data["once"] = "1"
    assert client.post("/admin/fault", data=data, headers=JSON).status_code == 200


def current_fault(client: TestClient) -> str:
    html = client.get("/admin", headers=HTML).text
    return html.split('id="current-fault" data-fault="', 1)[1].split('"', 1)[0]


def is_listed(client: TestClient, data: bytes) -> bool:
    sha = hashlib.sha256(data).hexdigest()
    return f'data-sha256="{sha}"' in client.get("/claims/CLM-2026-0142", headers=HTML).text


def test_admin_page_is_labelled_demo_only(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    html = client.get("/admin", headers=HTML).text
    assert "DEMO CONTROL PANEL" in html and 'id="synthetic-tag"' in html


def test_status_change_updates_text_and_stamp(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    before = client.get("/claims/CLM-2026-0150", headers=HTML).text
    resp = client.post(
        "/admin/status", data={"claim_id": "CLM-2026-0150", "raw_status": "Sent to Medical Team", "csrf_token": csrf},
        headers=JSON,
    )
    assert resp.status_code == 200
    after = client.get("/claims/CLM-2026-0150", headers=HTML).text
    assert 'data-raw-status="Sent to Medical Team"' in after
    stamp = lambda h: h.split('id="status-updated-at" datetime="', 1)[1].split('"', 1)[0]  # noqa: E731
    assert stamp(after) != stamp(before)
    bad = client.post("/admin/status", data={"claim_id": "CLM-2026-0150", "raw_status": " ", "csrf_token": csrf},
                      headers=JSON)
    assert bad.status_code == 400


def test_reset_restores_seed_and_removes_uploads(
    signed_in: tuple[TestClient, str], portal_settings: PortalSettings
) -> None:
    client, csrf = signed_in
    data = pdf("to be wiped")
    upload(client, csrf, data)
    client.post("/admin/status", data={"claim_id": "CLM-2026-0142", "raw_status": "Settled", "csrf_token": csrf},
                headers=JSON)
    set_fault(client, csrf, "http_500", once=False)

    assert client.post("/admin/reset", data={"csrf_token": csrf}, headers=JSON).status_code == 200
    html = client.get("/claims/CLM-2026-0142", headers=HTML).text
    assert 'data-raw-status="Query Raised"' in html and not is_listed(client, data)
    sha = hashlib.sha256(data).hexdigest()
    assert not (portal_settings.upload_dir / "CLM-2026-0142" / f"{sha}.pdf").exists()
    assert current_fault(client) == "none"


def test_fault_http_500_does_not_store(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    set_fault(client, csrf, "http_500")
    data = pdf("500 case")
    resp = upload(client, csrf, data)
    assert resp.status_code == 500 and resp.json()["error"]["code"] == "simulated_server_error"
    assert not is_listed(client, data)
    assert current_fault(client) == "none"  # one-shot switched itself off
    assert upload(client, csrf, data).status_code == 201


def test_fault_timeout_before_store_delays_and_does_not_store(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    set_fault(client, csrf, "timeout_before_store")
    data = pdf("timeout before store")
    start = time.perf_counter()
    resp = upload(client, csrf, data)
    assert time.perf_counter() - start >= FAST_DELAY
    assert resp.status_code == 504 and resp.json()["error"]["code"] == "simulated_timeout"
    assert not is_listed(client, data)


def test_fault_timeout_after_store_stores_then_delays(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    set_fault(client, csrf, "timeout_after_store")
    data = pdf("timeout after store")
    start = time.perf_counter()
    resp = upload(client, csrf, data)
    assert time.perf_counter() - start >= FAST_DELAY
    assert resp.status_code == 201
    assert is_listed(client, data)
    log = client.get("/admin", headers=HTML).text
    assert 'data-outcome="stored; response delayed (simulated timeout)"' in log


def test_persistent_fault_applies_to_every_upload(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    set_fault(client, csrf, "http_500", once=False)
    assert upload(client, csrf, pdf("a")).status_code == 500
    assert upload(client, csrf, pdf("b")).status_code == 500
    assert current_fault(client) == "http_500"


def test_invalid_fault_mode_rejected(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    resp = client.post("/admin/fault", data={"mode": "chaos", "csrf_token": csrf}, headers=JSON)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_fault_mode"
