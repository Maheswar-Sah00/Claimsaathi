"""Seed data and the claim pages' deterministic selectors."""

from __future__ import annotations

import hashlib
import re

from fastapi.testclient import TestClient

from portal.config import PortalSettings
from tests.conftest import HTML, JSON


def text_of(html: str, element_id: str) -> str:
    """Inner text of the element with this id (no nested tags expected)."""
    match = re.search(rf'id="{element_id}"[^>]*>([^<]*)<', html)
    assert match, f"#{element_id} not found"
    return match.group(1).strip()


def test_claims_list_has_all_three(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    html = client.get("/claims", headers=HTML).text
    rows = re.findall(r'class="claim-row" data-claim-id="([^"]+)"', html)
    assert rows == ["CLM-2026-0142", "CLM-2026-0150", "CLM-2026-0163"]
    assert text_of(html, "policy-no") == "POL-HDF-778812"
    for name in ("Ramesh, 62 (Father)", "Lakshmi, 58 (Mother)", "Arjun, 31 (Self)"):
        assert name in html


def test_claim_0142_query_raised_with_pharmacy_invoice_request(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    html = client.get("/claims/CLM-2026-0142", headers=HTML).text
    assert text_of(html, "claim-id") == "CLM-2026-0142"
    assert text_of(html, "claim-status") == "Query Raised"
    assert text_of(html, "patient-name") == "Ramesh"
    assert text_of(html, "claim-route") == "Cashless"
    assert text_of(html, "admission-date") == "2026-10-14"
    assert text_of(html, "discharge-date") == "2026-10-18"
    assert 'datetime="2026-10-21T14:05:00+05:30"' in html
    assert 'data-req-id="REQ-0142-01" data-state="Pending" data-doc-type="pharmacy_invoice"' in html


def test_claim_0150_under_review_without_preauth(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    html = client.get("/claims/CLM-2026-0150", headers=HTML).text
    assert text_of(html, "claim-status") == "Under Review"
    assert text_of(html, "patient-name") == "Lakshmi"
    assert text_of(html, "claim-route") == "Reimbursement"
    assert 'id="preauth-none"' in html


def test_claim_0163_preauth_approved_but_claim_pending(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    html = client.get("/claims/CLM-2026-0163", headers=HTML).text
    assert text_of(html, "claim-status") == "Under Process - Pending Final Authorisation"
    assert text_of(html, "preauth-status") == "APPROVED"
    assert text_of(html, "preauth-amount") == "Rs 50,000"
    assert 'data-amount="50000"' in html


def test_uploaded_docs_show_name_size_hash_time_matching_disk(
    signed_in: tuple[TestClient, str], portal_settings: PortalSettings
) -> None:
    client, _ = signed_in
    html = client.get("/claims/CLM-2026-0150", headers=HTML).text
    rows = re.findall(
        r'class="uploaded-doc"[^>]*data-sha256="([0-9a-f]{64})".*?class="doc-filename">([^<]+)<'
        r'.*?class="doc-size[^"]*" data-bytes="(\d+)".*?class="doc-uploaded-at[^"]*"><time datetime="([^"]+)"',
        html,
        re.S,
    )
    assert [r[1] for r in rows] == ["claim_form.pdf", "hospital_bill.pdf", "discharge_summary.pdf"]
    for sha, _, size, stamp in rows:
        stored = portal_settings.upload_dir / "CLM-2026-0150" / f"{sha}.pdf"
        data = stored.read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha and len(data) == int(size)
        assert stamp.endswith("+05:30")


def test_unknown_and_malformed_claim_ids_404(signed_in: tuple[TestClient, str]) -> None:
    client, _ = signed_in
    assert client.get("/claims/CLM-2026-9999", headers=JSON).json()["error"]["code"] == "claim_not_found"
    resp = client.get("/claims/..%2F..%2Fetc", headers=HTML)
    assert resp.status_code == 404


def test_template_output_is_escaped(signed_in: tuple[TestClient, str]) -> None:
    client, csrf = signed_in
    client.post(
        "/admin/status",
        data={"claim_id": "CLM-2026-0150", "raw_status": "<script>alert(1)</script>", "csrf_token": csrf},
        headers=JSON,
    )
    html = client.get("/claims/CLM-2026-0150", headers=HTML).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
