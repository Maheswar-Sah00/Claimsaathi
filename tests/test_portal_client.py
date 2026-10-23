"""Portal adapter against the live mock portal (real uvicorn + headless Chromium).

The tests play the human with the phone: they read the OTP from the portal's own
server-side state, never from the page, and pass it to `submit_otp`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from agent import consent as cs
from agent import repository as repo
from agent.db import Database
from agent.portal_client import (
    CaptchaDetected,
    ClaimMismatch,
    ClaimNotFound,
    LayoutChanged,
    LoginState,
    NotApproved,
    OtpRejected,
    OtpRequired,
    PlaywrightTpaDriver,
    PortalClient,
    PortalUnavailable,
    SessionExpired,
    UnsafeFile,
)
from agent.seed import init_database
from portal.app import create_app as create_portal
from portal.config import PortalSettings
from tests.conftest import JSON, serve
from tests.conftest import matched

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parent.parent


# ---------------- fixtures ----------------

@pytest.fixture
def portal_app(portal_settings: PortalSettings) -> FastAPI:
    return create_portal(replace(portal_settings, fault_delay_seconds=2.5))


@pytest.fixture
def portal_url(portal_app: FastAPI) -> Iterator[str]:
    with serve(portal_app) as url:
        yield url


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, tmp_path / "vault")
    return database


def make_client(db: Database, base_url: str, tmp_path: Path) -> PortalClient:
    driver = PlaywrightTpaDriver(base_url, timeout_ms=8000)
    return PortalClient(db, driver, evidence_dir=tmp_path / "evidence", vault_dir=tmp_path / "vault",
                        username="arjun", password="demo123", upload_timeout_ms=1000)


@pytest.fixture
def client(db: Database, portal_url: str, tmp_path: Path) -> Iterator[PortalClient]:
    c = make_client(db, portal_url, tmp_path)
    yield c
    c.close()


def human_otp(portal_app: FastAPI) -> str:
    """What the person reads on their (simulated) phone: the portal's own pending OTP."""
    challenges = portal_app.state.store.otp_challenges
    assert challenges, "no OTP was issued"
    return list(challenges.values())[-1].code


def sign_in(client: PortalClient, portal_app: FastAPI) -> None:
    status = client.login()
    assert status.state is LoginState.OTP_REQUIRED
    assert client.submit_otp(human_otp(portal_app)).state is LoginState.AUTHENTICATED


def allowed_run(db: Database, requester: str, claim_id: str, action: str = "read_status") -> str:
    run = cs.start_run(db, requester, claim_id)
    assert cs.check_consent(db, requester, claim_id, action, run_id=run.id).allowed
    return run.id


def evidence_rows(db: Database, run_id: str) -> list[repo.Evidence]:
    with db.read() as conn:
        return [e for e in repo.evidence_for_run(conn, run_id) if e.kind != "consent_record"]


def assert_evidence_files(ev: repo.Evidence, evidence_dir: Path) -> None:
    shot = evidence_dir / ev.screenshot_path
    assert shot.read_bytes().startswith(b"\x89PNG")
    html = (evidence_dir / ev.extracted["html_file"]).read_bytes()
    assert hashlib.sha256(html).hexdigest() == ev.html_sha256
    assert not Path(ev.screenshot_path).is_absolute()


# ---------------- login and OTP ----------------

def test_login_stops_at_otp_until_a_human_answers(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    status = client.login()
    assert status.state is LoginState.OTP_REQUIRED
    assert status.screenshot.startswith(b"\x89PNG")  # shown to the person, never parsed

    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    with pytest.raises(OtpRequired):
        client.read_claim("CLM-2026-0142", run_id=run_id)

    code = human_otp(portal_app)
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(OtpRejected):
        client.submit_otp(wrong)
    assert client.state is LoginState.OTP_REQUIRED
    with pytest.raises(OtpRejected):
        client.submit_otp("12ab")
    assert client.submit_otp(code).state is LoginState.AUTHENTICATED


def test_adapter_never_reads_the_otp_element() -> None:
    source = (ROOT / "agent" / "portal_client.py").read_text(encoding="utf-8")
    assert "demo-sms-otp" not in source and "demo-sms-panel" not in source


# ---------------- reads ----------------

def test_reads_all_three_claims_with_evidence(client: PortalClient, portal_app: FastAPI, db: Database,
                                              tmp_path: Path) -> None:
    sign_in(client, portal_app)
    expected = {
        ("P-ARJUN", "CLM-2026-0142"): ("Query Raised", "Ramesh", 2, "Approved"),
        ("P-LAKSHMI", "CLM-2026-0150"): ("Under Review", "Lakshmi", 0, None),
        ("P-ARJUN", "CLM-2026-0163"): ("Under Process - Pending Final Authorisation", "Ramesh", 0, "APPROVED"),
    }
    for (requester, claim_id), (status, patient, n_req, preauth) in expected.items():
        run_id = allowed_run(db, requester, claim_id)
        result = client.read_claim(claim_id, run_id=run_id)
        snap = result.snapshot
        assert (snap.claim_id, snap.policy_no, snap.raw_status, snap.patient_name) == (
            claim_id, "POL-HDF-778812", status, patient)
        assert len(snap.requirements) == n_req
        assert (snap.preauth.status if snap.preauth else None) == preauth
        assert snap.status_updated_at_utc.endswith("Z")
        (ev,) = evidence_rows(db, run_id)
        assert ev.id == result.evidence_id and ev.kind == "portal_page" and ev.extracted["ok"] is True
        assert ev.extracted["claim"]["raw_status"] == status
        assert_evidence_files(ev, tmp_path / "evidence")

    snap_0163 = client.read_claim("CLM-2026-0163", run_id=allowed_run(db, "P-ARJUN", "CLM-2026-0163")).snapshot
    assert snap_0163.preauth.amount_inr == 50000 and snap_0163.discharge_date is None


def test_list_documents_reads_uploaded_metadata(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    run_id = allowed_run(db, "P-LAKSHMI", "CLM-2026-0150")
    docs = client.list_documents("CLM-2026-0150", run_id=run_id)
    assert [d.filename for d in docs.uploaded] == ["claim_form.pdf", "hospital_bill.pdf", "discharge_summary.pdf"]
    assert all(len(d.sha256) == 64 and d.size_bytes > 0 for d in docs.uploaded)
    assert docs.raw_status == "Under Review"
    (ev,) = evidence_rows(db, run_id)
    assert ev.kind == "document_list" and len(ev.extracted["uploaded"]) == 3


def test_no_read_without_a_current_allow(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    url_before = client.driver._page.url
    blocked = cs.start_run(db, "P-ARJUN", "CLM-2026-0150")
    assert not cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status", run_id=blocked.id).allowed
    with pytest.raises(NotApproved):
        client.read_claim("CLM-2026-0150", run_id=blocked.id)
    other = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    with pytest.raises(NotApproved, match="is for CLM-2026-0142"):
        client.read_claim("CLM-2026-0150", run_id=other)
    assert client.driver._page.url == url_before  # the browser never moved
    assert evidence_rows(db, blocked.id) == []


def test_invalid_claim_records_failure_evidence(client: PortalClient, portal_app: FastAPI, db: Database,
                                               tmp_path: Path) -> None:
    sign_in(client, portal_app)
    with db.transaction() as conn:
        repo.register_claim(conn, claim_id="CLM-2026-0999", policy_no="POL-HDF-778812", patient_id="P-RAMESH",
                            source="portal_list")
    run_id = allowed_run(db, "P-RAMESH", "CLM-2026-0999")
    with pytest.raises(ClaimNotFound):
        client.read_claim("CLM-2026-0999", run_id=run_id)
    (ev,) = evidence_rows(db, run_id)
    assert ev.extracted == {**ev.extracted, "ok": False, "error": "claim_not_found"}
    assert_evidence_files(ev, tmp_path / "evidence")


def test_page_for_a_different_policy_is_rejected(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    with db.transaction() as conn:
        conn.execute("UPDATE claims SET policy_no = 'POL-OTHER-1' WHERE claim_id = 'CLM-2026-0150'")
    run_id = allowed_run(db, "P-LAKSHMI", "CLM-2026-0150")
    with pytest.raises(ClaimMismatch, match="policy"):
        client.read_claim("CLM-2026-0150", run_id=run_id)
    (ev,) = evidence_rows(db, run_id)
    assert ev.extracted["error"] == "claim_mismatch"


def test_session_expiry_is_reported_not_hidden(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    client.driver.expire_session()
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    with pytest.raises(SessionExpired):
        client.read_claim("CLM-2026-0142", run_id=run_id)
    assert client.state is LoginState.LOGGED_OUT
    (ev,) = evidence_rows(db, run_id)
    assert ev.extracted["error"] == "session_expired" and ev.extracted["blocked"] is True


# ---------------- outages, CAPTCHA, layout changes ----------------

def test_portal_down_before_and_during_session(db: Database, tmp_path: Path, portal_settings: PortalSettings) -> None:
    dead = make_client(db, "http://127.0.0.1:9", tmp_path)
    try:
        with pytest.raises(PortalUnavailable):
            dead.login()
    finally:
        dead.close()

    app = create_portal(portal_settings)
    with serve(app) as url:
        c = make_client(db, url, tmp_path)
        sign_in(c, app)
    try:  # the server is now stopped
        run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
        with pytest.raises(PortalUnavailable):
            c.read_claim("CLM-2026-0142", run_id=run_id)
        (ev,) = evidence_rows(db, run_id)
        assert ev.extracted["error"] == "portal_unavailable" and ev.screenshot_path is None
    finally:
        c.close()


def odd_portal(page: str) -> FastAPI:
    app = FastAPI()

    @app.get("/login", response_class=HTMLResponse)
    def login() -> str:
        return page

    return app


@pytest.mark.parametrize("page, error", [
    ('<form id="login-form"></form><div class="g-recaptcha">I am not a robot</div>', CaptchaDetected),
    ('<form id="sign-in-v2"><input name="user"></form>', LayoutChanged),
])
def test_captcha_and_unknown_layout_stop_the_agent(page: str, error: type, db: Database, tmp_path: Path) -> None:
    with serve(odd_portal(f"<html><body>{page}</body></html>")) as url:
        c = make_client(db, url, tmp_path)
        try:
            with pytest.raises(error):
                c.login()
            assert c.state is LoginState.LOGGED_OUT
        finally:
            c.close()


# ---------------- uploads ----------------

def executing_upload(db: Database, vault_file_id: str = "VF-SEED-01") -> str:
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142", "upload_docs")
    with db.read() as conn:
        vf = repo.get_vault_file(conn, vault_file_id)
    with db.transaction() as conn:
        action, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142", requirement_id="REQ-0142-01",
                                        file_sha256=vf.sha256, vault_file_id=vf.id,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", vf.sha256))
    cs.guarded_approve_upload(db, action.id, approved_by="P-ARJUN")
    cs.guarded_start_upload(db, action.id, requester_id="P-ARJUN")
    return action.id


def test_upload_needs_an_executing_approved_action(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142", "upload_docs")
    with db.read() as conn:
        vf = repo.get_vault_file(conn, "VF-SEED-01")
    with db.transaction() as conn:
        action, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142", requirement_id="REQ-0142-01",
                                        file_sha256=vf.sha256, vault_file_id=vf.id,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", vf.sha256))
    with pytest.raises(NotApproved, match="PROPOSED"):
        client.upload(action_id=action.id)
    assert portal_app.state.store.upload_log == []  # nothing reached the portal


def test_upload_through_the_portal_form(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    sign_in(client, portal_app)
    action_id = executing_upload(db)
    result = client.upload(action_id=action_id)
    with db.read() as conn:
        action = repo.get_action(conn, action_id)
    assert result.outcome == "accepted" and result.confirmed_sha256 == action.file_sha256
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    assert client.list_documents("CLM-2026-0142", run_id=run_id).has_file(action.file_sha256, "REQ-0142-01")
    log = portal_app.state.store.upload_log
    assert [e.outcome for e in log] == ["stored"] and log[0].filename == "pharmacy_invoice_ramesh.pdf"


def test_upload_timeout_after_store_is_reported_as_timeout(client: PortalClient, portal_app: FastAPI,
                                                          db: Database) -> None:
    sign_in(client, portal_app)
    portal_app.state.store.set_fault("timeout_after_store", once=True)
    action_id = executing_upload(db)
    result = client.upload(action_id=action_id)
    assert result.outcome == "timeout" and result.confirmed_sha256 is None
    with db.read() as conn:
        sha = repo.get_action(conn, action_id).file_sha256
    # The adapter claimed nothing; a re-read shows the portal did store it (TC4 building block).
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    assert client.list_documents("CLM-2026-0142", run_id=run_id).has_file(sha, "REQ-0142-01")


def test_tampered_vault_file_is_never_sent(client: PortalClient, portal_app: FastAPI, db: Database,
                                          tmp_path: Path) -> None:
    sign_in(client, portal_app)
    action_id = executing_upload(db)
    (tmp_path / "vault" / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").write_bytes(b"%PDF-1.4 swapped")
    with pytest.raises(UnsafeFile, match="do not match"):
        client.upload(action_id=action_id)
    assert portal_app.state.store.upload_log == []


# ---------------- the human interrupt over HTTP ----------------

def test_otp_interrupt_api(portal_app: FastAPI, portal_url: str, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import config as agent_config
    from agent.server import create_app
    from tests.test_consent_web import sign_in as agent_sign_in

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("PORTAL_BASE_URL", portal_url)
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as agent:
        csrf = agent_sign_in(agent, "P-ARJUN")
        headers = {**JSON, "x-csrf-token": csrf}
        started = agent.post("/api/portal/session", headers=headers).json()
        assert started["state"] == "otp_required" and started["otp_screen_url"]
        screen = agent.get(started["otp_screen_url"])
        assert screen.headers["content-type"] == "image/png" and screen.content.startswith(b"\x89PNG")

        code = human_otp(portal_app)
        wrong = "000000" if code != "000000" else "111111"
        rejected = agent.post("/api/portal/session/otp", json={"otp": wrong}, headers=headers)
        assert rejected.status_code == 401 and rejected.json()["error"]["code"] == "otp_rejected"
        done = agent.post("/api/portal/session/otp", json={"otp": code}, headers=headers)
        assert done.json()["state"] == "authenticated"
        assert agent.get("/api/portal/session").json()["state"] == "authenticated"


# ---------------- Phase 6 end to end: real read -> extraction -> status -> reply ----------------

def test_tc3_live_read_extract_status_reply(client: PortalClient, portal_app: FastAPI, db: Database) -> None:
    from agent.extract import extract_claim
    from agent.models import CanonicalStatus
    from agent.reply import compose_reply, facts_from
    from agent.status import record_status

    sign_in(client, portal_app)
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0163")
    read = client.read_claim("CLM-2026-0163", run_id=run_id)
    out = extract_claim(db, run_id=run_id, evidence_id=read.evidence_id, claim_id="CLM-2026-0163")
    assert out.valid, out.errors
    status = record_status(db, run_id=run_id, claim_id="CLM-2026-0163", evidence_id=read.evidence_id,
                           extraction_id=out.extraction_id, extraction_valid=True,
                           raw_status=out.data.raw_status, status_stamp=out.data.status_stamp)
    assert out.data.preauth_status == "APPROVED" and out.data.preauth_amount == 50000
    assert status.canonical is CanonicalStatus.UNDER_REVIEW
    reply = compose_reply(db, facts_from(out, status), run_id=run_id, evidence_id=read.evidence_id)
    assert reply.guard.passed and "has been approved" not in reply.text.lower()
    with db.read() as conn:
        assert repo.get_extraction(conn, out.extraction_id).raw_text.startswith("DemoTPA")


def test_phase7_live_0142_match(client: PortalClient, portal_app: FastAPI, db: Database, tmp_path: Path) -> None:
    from agent.extract import extract_claim
    from agent.matcher import match_claim
    from agent.models import MatchResult

    sign_in(client, portal_app)
    run_id = allowed_run(db, "P-ARJUN", "CLM-2026-0142")
    read = client.read_claim("CLM-2026-0142", run_id=run_id)
    assert extract_claim(db, run_id=run_id, evidence_id=read.evidence_id, claim_id="CLM-2026-0142").valid
    report = match_claim(db, run_id=run_id, requester_id="P-ARJUN", vault_dir=tmp_path / "vault")
    items = {i.requirement_id: i for i in report.items}
    assert items["REQ-0142-01"].result is MatchResult.MATCHED and items["REQ-0142-01"].vault_file_id == "VF-SEED-01"
    assert items["REQ-0142-02"].result is MatchResult.HOSPITAL_SHOULD_PROVIDE
    lakshmi = next(c for c in items["REQ-0142-01"].candidates if c.vault_file_id == "VF-SEED-02")
    assert not lakshmi.passed and any("Lakshmi" in f for f in lakshmi.failures)
