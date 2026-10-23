"""Multipart uploads: validation, hashing, duplicates and safe storage."""

from __future__ import annotations

import hashlib
import re

from fastapi.testclient import TestClient

from portal.app import create_app
from portal.config import PortalSettings
from tests.conftest import HTML, JSON, login, pdf, upload

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_valid_upload_is_hashed_stored_and_listed(
    signed_in: tuple[TestClient, str], portal_settings: PortalSettings
) -> None:
    client, csrf = signed_in
    data = pdf("pharmacy invoice for Ramesh, 2026-10-18")
    resp = upload(client, csrf, data)
    assert resp.status_code == 201
    doc = resp.json()["uploaded"]
    sha = hashlib.sha256(data).hexdigest()
    assert doc["sha256"] == sha and doc["size"] == len(data) and doc["requirement_id"] == "REQ-0142-01"
    assert (portal_settings.upload_dir / "CLM-2026-0142" / f"{sha}.pdf").read_bytes() == data

    html = client.get("/claims/CLM-2026-0142", headers=HTML).text
    assert f'data-sha256="{sha}" data-requirement-id="REQ-0142-01"' in html
    assert 'data-req-id="REQ-0142-01" data-state="Received"' in html


def test_browser_upload_redirects_to_claim_with_result(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    data = pdf("browser flow")
    resp = upload(client, csrf, data, headers=HTML)
    assert resp.status_code == 200 and "/claims/CLM-2026-0142?uploaded=" in str(resp.url)
    assert f'id="upload-result" class="success" role="status"' in resp.text
    assert hashlib.sha256(data).hexdigest() in resp.text


def test_duplicate_for_same_requirement_rejected_but_other_requirement_allowed(
    signed_in: tuple[TestClient, str],
) -> None:
    client, csrf = signed_in
    data = pdf("dup check")
    assert upload(client, csrf, data).status_code == 201
    again = upload(client, csrf, data, filename="renamed.pdf")
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate_upload"
    assert upload(client, csrf, data, req_id="REQ-0142-02").status_code == 201
    html = client.get("/claims/CLM-2026-0142", headers=HTML).text
    sha = hashlib.sha256(data).hexdigest()
    assert html.count(f'data-sha256="{sha}"') == 2


def test_rejects_disallowed_type_and_mismatched_content(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    exe = upload(client, csrf, b"MZ\x90\x00", filename="tool.exe")
    assert exe.status_code == 415 and exe.json()["error"]["code"] == "unsupported_file_type"
    fake_pdf = upload(client, csrf, PNG, filename="looks_like.pdf")
    assert fake_pdf.status_code == 415 and fake_pdf.json()["error"]["code"] == "file_content_mismatch"
    assert upload(client, csrf, PNG, filename="photo.png").status_code == 201


def test_rejects_empty_and_oversized_files(small_upload_settings: PortalSettings) -> None:
    with TestClient(create_app(small_upload_settings)) as client:
        csrf = login(client)
        assert upload(client, csrf, b"").json()["error"]["code"] == "empty_file"
        big = upload(client, csrf, b"%PDF-" + b"0" * 4096)
        assert big.status_code == 413 and big.json()["error"]["code"] == "file_too_large"


def test_rejects_unknown_requirement_and_claim_without_requests(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    bad_req = upload(client, csrf, pdf("x"), req_id="REQ-9999-99")
    assert bad_req.status_code == 400 and bad_req.json()["error"]["code"] == "unknown_requirement"
    other_claim = upload(client, csrf, pdf("y"), claim_id="CLM-2026-0150", req_id="REQ-0142-01")
    assert other_claim.status_code == 400


def test_requires_login_and_csrf(portal: TestClient) -> None:
    assert upload(portal, "anything", pdf("z")).status_code == 401
    login(portal)
    forged = upload(portal, "forged-token", pdf("z"))
    assert forged.status_code == 403 and forged.json()["error"]["code"] == "csrf_failed"


def test_client_filename_is_never_used_as_a_path(
    signed_in: tuple[TestClient, str], portal_settings: PortalSettings
) -> None:
    client, csrf = signed_in
    data = pdf("traversal attempt")
    resp = upload(client, csrf, data, filename="..\\..\\..\\windows\\evil<x>.pdf")
    assert resp.status_code == 201
    assert resp.json()["uploaded"]["filename"] == "evil_x_.pdf"
    sha = hashlib.sha256(data).hexdigest()
    stored = list(portal_settings.data_dir.rglob("*.pdf"))
    assert portal_settings.upload_dir / "CLM-2026-0142" / f"{sha}.pdf" in stored
    assert all(portal_settings.upload_dir in p.parents for p in stored)
    assert not any(re.search("evil", p.name) for p in stored)
