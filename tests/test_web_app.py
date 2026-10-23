"""Phase 10: the family web app over the real workflow (dashboard, claim view, trace, evidence, test runs,
patient consent API) and the server-side guarantees the browser cannot get around."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import repository as repo
from tests.conftest import HTML, JSON, csrf_from
from tests.test_consent_web import sign_in, sign_out
from tests.test_graph import OtpStoreDriver, make_flow, portal_client

C142, C150, C163 = "CLM-2026-0142", "CLM-2026-0150", "CLM-2026-0163"


@pytest.fixture
def store(tmp_path: Path):  # noqa: ANN201
    from portal.store import PortalStore

    s = PortalStore(upload_dir=tmp_path / "portal")
    s.reset()
    return s


@pytest.fixture
def driver(store) -> OtpStoreDriver:  # noqa: ANN001
    return OtpStoreDriver(store)


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: OtpStoreDriver) -> Iterator[TestClient]:
    from agent import config as agent_config
    from agent.server import create_app

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("PORTAL_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as client:
        app = client.app
        portal = portal_client(app.state.db, driver, tmp_path, tmp_path / "webvault")
        app.state.workflow = make_flow(app.state.db, portal, tmp_path / "webvault")
        yield client


def start(agent: TestClient, csrf: str, claim: str, intent: str = "status", policy: str = "POL-HDF-778812") -> str:
    page = agent.post("/workflows", data={"claim_id": claim, "intent": intent, "policy_no": policy, "csrf_token": csrf},
                      headers=HTML)
    assert page.status_code == 200, page.text
    return str(page.url).rsplit("/", 1)[-1].split("?")[0]


def type_otp(agent: TestClient, csrf: str, run_id: str, driver: OtpStoreDriver) -> str:
    return agent.post(f"/workflows/{run_id}/otp", data={"otp": driver.code, "csrf_token": csrf}, headers=HTML).text


# ---------------- walkthrough ----------------

def test_walkthrough_is_public_and_step_by_step(agent: TestClient) -> None:
    page = agent.get("/walkthrough", headers=HTML)
    assert page.status_code == 200
    html = page.text
    assert html.count('class="card tour-step"') == 13
    for needle in ('id="tour-next"', 'id="tour-prev"', "walkthrough.js", 'aria-current="page">How it works',
                   C142, C150, C163, "http://127.0.0.1:8001/admin"):
        assert needle in html, needle
    assert 'href="/walkthrough"' in agent.get("/", headers=HTML).text
    assert agent.get("/static/walkthrough.js").status_code == 200


# ---------------- dashboard and claim view ----------------

def test_dashboard_start_form_and_consent_panel(agent: TestClient) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    page = agent.get("/dashboard", headers=HTML).text
    for needle in ('id="start-form"', 'value="Arjun (P-ARJUN)" readonly', 'name="policy_no"', 'name="claim_id"',
                   'id="consent-panel"', 'data-consent-id="CON-SEED-01"', 'data-consent-id="CON-SEED-02"',
                   'aria-current="page">Dashboard'):
        assert needle in page, needle
    assert agent.get("/workflows", headers=HTML).text.count('id="start-form"') == 1
    bad = agent.post("/workflows", data={"claim_id": "0142", "policy_no": "", "csrf_token": csrf}, headers=HTML)
    assert 'data-error-code="invalid_request"' in bad.text


def test_claim_view_status_preauth_documents_and_trace(agent: TestClient, driver: OtpStoreDriver) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    run_id = start(agent, csrf, C163)  # TC3 through the web
    page = type_otp(agent, csrf, run_id, driver)
    assert 'data-canonical="UNDER_REVIEW"' in page and "“Under Process - Pending Final Authorisation”" in page
    assert 'data-preauth="APPROVED"' in page and "Rs 50,000" in page and "not the final claim decision" in page
    assert 'id="status-stamp">22 Oct 2026, 09:15 IST' in page
    assert page.count('id="reply-next"') == 1  # exactly one recommended next step
    assert 'id="progress"' in page and 'data-stage="Your approval" data-state="skipped"' in page
    assert 'id="steps" hidden' in page  # the trace pane exists, behind its tab

    trace = agent.get(f"/workflows/{run_id}?tab=trace", headers=HTML).text
    assert 'id="claim-pane" hidden' in trace and 'aria-selected="true" id="tab-trace"' in trace
    assert 'data-node="read_claim" data-result="OK"' in trace and 'class="trace-evidence"><a href="/evidence/EV-' in trace

    run_id = start(agent, csrf, C142)
    page = type_otp(agent, csrf, run_id, driver)
    for needle in ("<th>Requested item</th><th>Matched file</th><th>State</th><th>Reason</th><th>Next action</th>",
                   'data-req-id="REQ-0142-01" data-match="MATCHED"', "pharmacy_invoice_ramesh.pdf",
                   "Ask me to upload this file", 'data-req-id="REQ-0142-02" data-match="HOSPITAL_SHOULD_PROVIDE"',
                   "Send the drafted note to the hospital", "Drafted note to the hospital"):
        assert needle in page, needle


def test_approval_card_on_the_claim_view(agent: TestClient, driver: OtpStoreDriver) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    run_id = start(agent, csrf, C142, intent="upload")
    page = type_otp(agent, csrf, run_id, driver)
    for needle in ('id="waiting-approval"', 'id="card-preview"', 'src="/vault/files/VF-SEED-01"', 'id="card-sha256"',
                   'data-req-id="REQ-0142-01"', 'href="/evidence/EV-', 'id="approve-button"', 'id="reject-button"',
                   'data-stage="Your approval" data-state="waiting"'):
        assert needle in page, needle
    view = agent.get(f"/api/workflows/{run_id}").json()
    done = agent.post(f"/workflows/{run_id}/decision", headers=HTML, data={
        "decision": "approve", "action_id": view["waiting"]["action_id"], "card_sha256": view["waiting"]["card_sha256"],
        "csrf_token": csrf}).text
    assert 'data-req-id="REQ-0142-01" data-match="MATCHED" data-action-state="VERIFIED"' in done
    assert 'data-stage="Upload" data-state="done"' in done and len(driver.sent) == 1


def test_consent_block_shows_the_patient_flow_and_the_api_grants(agent: TestClient, driver: OtpStoreDriver) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    run_id = start(agent, csrf, C150)  # TC2 through the web
    page = agent.get(f"/workflows/{run_id}", headers=HTML).text
    assert 'id="consent-blocked" data-consent-result="NEEDS_CONSENT"' in page and 'id="patient-flow"' in page
    assert driver.logins == 0 and driver.fetches == 0
    request_id = agent.get(f"/api/workflows/{run_id}").json()["final_response"]["consent"]["request_id"]
    refused = agent.post(f"/api/consent-requests/{request_id}/approve", json={}, headers={**JSON, "x-csrf-token": csrf})
    assert refused.status_code == 403  # the requester cannot grant their own access
    sign_out(agent, csrf)

    lakshmi = sign_in(agent, "P-LAKSHMI")
    granted = agent.post(f"/api/consent-requests/{request_id}/approve", headers={**JSON, "x-csrf-token": lakshmi},
                         json={"actions": ["read_status"], "duration_days": 1})
    assert granted.status_code == 200 and granted.json()["actions"] == ["read_status"]
    sign_out(agent, lakshmi)

    csrf = sign_in(agent, "P-ARJUN")
    again = agent.post("/api/workflows", json={"claim_id": C150}, headers={**JSON, "x-csrf-token": csrf}).json()
    assert again["status"] == "waiting_for_otp"  # consent now allows the read


# ---------------- evidence ----------------

def test_evidence_is_owner_only_and_hash_checked(agent: TestClient, driver: OtpStoreDriver, tmp_path: Path) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    run_id = start(agent, csrf, C142)
    type_otp(agent, csrf, run_id, driver)
    with agent.app.state.db.read() as conn:
        page_ev = next(e for e in repo.evidence_for_run(conn, run_id) if e.kind == "portal_page")
    data = agent.get(f"/api/evidence/{page_ev.id}").json()
    assert data["kind"] == "portal_page" and data["html_sha256"] == page_ev.html_sha256 and data["html_hash_matches"]
    assert "visible_text" not in data["extracted"] and data["html_url"] == f"/evidence/{page_ev.id}/html"
    html = agent.get(data["html_url"])
    assert html.headers["content-type"].startswith("text/plain") and html.headers["x-content-type-options"] == "nosniff"
    assert html.headers["x-evidence-sha256"] == html.headers["x-computed-sha256"] == page_ev.html_sha256
    assert 'id="hash-check">file matches' in agent.get(f"/evidence/{page_ev.id}", headers=HTML).text
    assert agent.get(f"/evidence/{page_ev.id}/screenshot").status_code == 404  # this driver takes none

    shot_dir = tmp_path / "evidence" / run_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    with agent.app.state.db.transaction() as conn:
        shot = repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="http://portal/x",
                                 screenshot_path=f"{run_id}/shot.png", extracted={"ok": True}, extraction_source="rule")
    image = agent.get(f"/evidence/{shot.id}/screenshot")
    assert image.headers["content-type"] == "image/png" and image.content.startswith(b"\x89PNG")
    assert 'id="evidence-screenshot"' in agent.get(f"/evidence/{shot.id}", headers=HTML).text
    sign_out(agent, csrf)

    sign_in(agent, "P-LAKSHMI")
    assert agent.get(f"/api/evidence/{page_ev.id}").status_code == 404
    assert agent.get(f"/evidence/{shot.id}/screenshot").status_code == 404


# ---------------- test runs ----------------

def test_test_runs_come_from_the_runs_table(agent: TestClient) -> None:
    db = agent.app.state.db
    with db.transaction() as conn:
        passed = repo.create_run(conn, requester_id="P-ARJUN", claim_id=C142, test_case="TC1")
        repo.record_scenario_result(conn, test_case="TC1", title="TC1 title", run_id=passed.id, input={"a": 1},
                                    expected="verified", actual="verified", passed=True,
                                    checks=[{"check": "x", "ok": True}], evidence=[])
        unverified = repo.create_run(conn, requester_id="P-ARJUN", claim_id=C163, test_case="TC3")
        with pytest.raises(repo.ValidationError):  # a verdict cannot claim PASS with a failed check
            repo.record_scenario_result(conn, test_case="TC3", title="t", run_id=unverified.id, input={}, expected="e",
                                        actual="a", passed=True, checks=[{"check": "x", "ok": False}], evidence=[])
    sign_in(agent, "P-ARJUN")
    data = agent.get("/api/test-runs").json()
    by_case = {r["test_case"]: r for r in data["runs"]}
    assert by_case["TC1"]["verdict"] == "PASS" and by_case["TC3"]["verdict"] is None
    assert (data["passed"], data["failed"], data["unverified"]) == (1, 0, 1)
    assert data["official_without_runs"] == ["TC2", "TC4"]
    page = agent.get("/test-runs", headers=HTML).text
    assert 'data-test-case="TC1" data-verdict="PASS"' in page and 'data-test-case="TC3" data-verdict="NONE"' in page
    assert "No run recorded yet for: TC2, TC4" in page


# ---------------- what the browser cannot do ----------------

def test_identifiers_are_validated(agent: TestClient) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    headers = {**JSON, "x-csrf-token": csrf}
    for path in ("/api/workflows/RUN-XYZ", "/api/workflows/run-0123456789ab", "/api/actions/ACT-123",
                 "/api/evidence/EV-0123456789AB", "/api/consent-requests/CRQ-x"):
        assert agent.get(path, headers=headers).status_code == 422, path
    assert agent.get("/api/evidence/EV-..%2F..", headers=headers).status_code == 404  # no route even matches
    assert agent.get("/api/workflows/RUN-0123456789ab").status_code == 404  # well-formed but not yours
    bad_body = agent.post("/api/workflows", json={"claim_id": C142, "requester_id": "P-RAMESH"}, headers=headers)
    assert bad_body.status_code == 422  # identity comes from the session, never the body


def test_the_browser_cannot_set_an_action_state(agent: TestClient, driver: OtpStoreDriver) -> None:
    csrf = sign_in(agent, "P-ARJUN")
    headers = {**JSON, "x-csrf-token": csrf}
    run_id = start(agent, csrf, C142, intent="upload")
    type_otp(agent, csrf, run_id, driver)
    action_id = agent.get(f"/api/workflows/{run_id}").json()["waiting"]["action_id"]
    for path, body in ((f"/api/workflows/{run_id}/resume", {"state": "VERIFIED"}),
                       (f"/api/workflows/{run_id}/resume", {"decision": "approve", "state": "VERIFIED"}),
                       (f"/api/actions/{action_id}/decision", {"decision": "approve", "state": "VERIFIED"}),
                       (f"/api/actions/{action_id}/decision", {"decision": "verified"})):
        assert agent.post(path, json=body, headers=headers).status_code == 422, (path, body)
    unapproved = agent.post(f"/api/actions/{action_id}/execute", headers=headers)
    assert unapproved.status_code == 409 and unapproved.json()["error"]["code"] == "approval_required"
    with agent.app.state.db.read() as conn:
        assert repo.get_action(conn, action_id).state == "PROPOSED"
    assert driver.sent == []

    routes = {(m, r.path) for r in agent.app.routes for m in getattr(r, "methods", ()) if m in ("POST", "PUT", "PATCH")}
    assert not [p for _, p in routes if "verif" in p or "state" in p]  # no endpoint sets a state
    assert {m for r in agent.app.routes for m in getattr(r, "methods", ())} <= {"GET", "HEAD", "POST"}
    assert agent.post(f"/api/workflows/{run_id}/resume", json={"otp": "123456"}).status_code in (401, 403)  # no CSRF
    assert csrf_from(agent.get("/dashboard", headers=HTML).text)
