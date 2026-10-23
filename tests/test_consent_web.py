"""Consent over HTTP: identity comes from the OTP-signed session, never from the request."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import config as agent_config
from agent.server import create_app
from tests.conftest import HTML, JSON, csrf_from, otp_from


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("PORTAL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as client:
        yield client


def sign_in(client: TestClient, person_id: str) -> str:
    """Sign in as a synthetic person via the demo OTP; returns the CSRF token."""
    page = client.post("/auth/login", data={"person_id": person_id}, headers=HTML)
    assert "DEMO SMS — SIMULATION" in page.text
    done = client.post("/auth/otp", data={"otp": otp_from(page.text)}, headers=HTML)
    assert done.status_code == 200 and f'data-person-id="{person_id}"' in done.text
    return csrf_from(done.text)


def sign_out(client: TestClient, csrf: str) -> None:
    client.post("/auth/logout", data={"csrf_token": csrf}, headers=HTML)


def access_check(client: TestClient, csrf: str, claim_id: str, action: str = "read_status"):
    return client.post("/api/access-check", json={"claim_id": claim_id, "action": action},
                       headers={**JSON, "x-csrf-token": csrf})


def test_api_requires_session_and_csrf(agent: TestClient) -> None:
    assert agent.post("/api/access-check", json={"claim_id": "CLM-2026-0150", "action": "read_status"},
                      headers=JSON).status_code == 401
    sign_in(agent, "P-ARJUN")
    assert access_check(agent, "forged", "CLM-2026-0150").status_code == 403


def test_body_cannot_choose_the_requester_or_patient(agent: TestClient) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    resp = agent.post("/api/access-check",
                      json={"claim_id": "CLM-2026-0150", "action": "read_status", "requester_id": "P-LAKSHMI",
                            "patient_id": "P-ARJUN"},
                      headers={**JSON, "x-csrf-token": csrf})
    assert resp.status_code == 403 and resp.json()["decision"]["result"] == "NEEDS_CONSENT"


def test_tc2_over_http(agent: TestClient) -> None:
    # Arjun asks for his mother's claim: blocked, request generated.
    csrf = sign_in(agent, "P-ARJUN")
    blocked = access_check(agent, csrf, "CLM-2026-0150")
    assert blocked.status_code == 403
    body = blocked.json()
    assert body["decision"]["result"] == "NEEDS_CONSENT"
    review_url = body["consent_request"]["review_url"]
    request_id = body["consent_request"]["id"]

    # Arjun can see the request but gets no decision form, and a forged approval is refused.
    page = agent.get(review_url, headers=HTML)
    assert page.status_code == 200 and 'id="patient-only"' in page.text and 'id="approve-form"' not in page.text
    forged = agent.post(f"/consent/requests/{request_id}/approve",
                        data={"csrf_token": csrf, "actions": "read_status", "duration_days": "7"}, headers=JSON)
    assert forged.status_code == 403 and forged.json()["error"]["code"] == "forbidden"
    sign_out(agent, csrf)

    # Ramesh (another family member) cannot even view it.
    csrf = sign_in(agent, "P-RAMESH")
    assert agent.get(review_url, headers=JSON).status_code == 403
    sign_out(agent, csrf)

    # Lakshmi reviews the scope and approves this claim only.
    csrf = sign_in(agent, "P-LAKSHMI")
    page = agent.get(review_url, headers=HTML)
    assert 'id="approve-form"' in page.text and "CLM-2026-0150" in page.text and "Arjun" in page.text
    done = agent.post(f"/consent/requests/{request_id}/approve",
                      data={"csrf_token": csrf, "actions": "read_status", "duration_days": "7", "scope": "claim"},
                      headers=HTML)
    assert "Consent granted." in done.text
    consent_id = re.search(r'id="consents-given".*?data-consent-id="([^"]+)" data-active="true"', done.text, re.S).group(1)
    sign_out(agent, csrf)

    # Arjun now passes.
    csrf = sign_in(agent, "P-ARJUN")
    assert access_check(agent, csrf, "CLM-2026-0150").json()["decision"]["result"] == "ALLOW"
    status = agent.get(f"/api/consent-requests/{request_id}", headers=JSON).json()
    assert status["status"] == "approved"
    sign_out(agent, csrf)

    # Lakshmi revokes; Arjun is blocked again at once.
    csrf = sign_in(agent, "P-LAKSHMI")
    agent.post(f"/consent/consents/{consent_id}/revoke", data={"csrf_token": csrf}, headers=HTML)
    sign_out(agent, csrf)
    csrf = sign_in(agent, "P-ARJUN")
    again = access_check(agent, csrf, "CLM-2026-0150")
    assert again.json()["decision"]["result"] == "EXPIRED_OR_REVOKED"


def test_dashboard_lists_seed_consents_for_grantee(agent: TestClient) -> None:
    sign_in(agent, "P-ARJUN")
    page = agent.get("/consent", headers=HTML).text
    assert 'data-consent-id="CON-SEED-01" data-active="true"' in page
    assert 'data-consent-id="CON-SEED-02" data-active="true"' in page


def test_pages_redirect_to_sign_in(agent: TestClient) -> None:
    resp = agent.get("/consent", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/login")
