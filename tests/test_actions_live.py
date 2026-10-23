"""Phase 8 against the live mock portal (real uvicorn, headless Chromium, real client timeouts).

TC1: an approved upload ends VERIFIED only after a fresh read lists the file.
TC4: a timeout after the portal stored the file is UNKNOWN, then VERIFIED by the re-read,
     with exactly one stored file and no second upload.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import actions as act
from agent import consent as cs
from agent import repository as repo
from agent.db import Database
from agent.extract import extract_claim
from agent.matcher import match_claim
from agent.portal_client import PortalClient
from agent.seed import init_database
from portal.app import create_app as create_portal
from portal.config import PortalSettings
from tests.conftest import HTML, JSON, serve
from tests.test_portal_client import human_otp, make_client, sign_in

pytestmark = pytest.mark.live
CLAIM, REQ = "CLM-2026-0142", "REQ-0142-01"


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


@pytest.fixture
def client(db: Database, portal_url: str, portal_app: FastAPI, tmp_path: Path) -> Iterator[PortalClient]:
    c = make_client(db, portal_url, tmp_path)  # upload bound: 1000 ms
    sign_in(c, portal_app)
    yield c
    c.close()


@pytest.fixture
def policy(tmp_path: Path, portal_url: str) -> act.UploadPolicy:
    return act.UploadPolicy(vault_dir=tmp_path / "vault", portal_base_url=portal_url, upload_timeout_ms=1000)


@pytest.fixture
def approved(db: Database, client: PortalClient, policy: act.UploadPolicy, tmp_path: Path) -> repo.Action:
    """Real read -> extraction -> match -> proposal -> the person's approval of the card."""
    run = cs.start_run(db, "P-ARJUN", CLAIM, test_case="TC1")
    assert cs.check_consent(db, "P-ARJUN", CLAIM, "read_status", run_id=run.id).allowed
    read = client.read_claim(CLAIM, run_id=run.id)
    assert extract_claim(db, run_id=run.id, evidence_id=read.evidence_id, claim_id=CLAIM).valid
    item = next(i for i in match_claim(db, run_id=run.id, requester_id="P-ARJUN", vault_dir=tmp_path / "vault").items
                if i.requirement_id == REQ)
    action, _ = act.propose_from_match(db, run_id=run.id, decision_id=item.decision_id, person_id="P-ARJUN")
    card = act.approval_card(db, action.id, person_id="P-ARJUN", policy=policy)
    assert card.can_approve
    return act.approve_upload(db, action.id, person_id="P-ARJUN", card_sha256=card.card_sha256, policy=policy)[0]


@pytest.fixture
def executor(db: Database, client: PortalClient, policy: act.UploadPolicy) -> act.UploadExecutor:
    return act.UploadExecutor(db, client, policy)


def stored(portal_app: FastAPI, sha: str) -> list:
    return [u for u in portal_app.state.store.get_claim(CLAIM).uploads if u.sha256 == sha]


def history(db: Database, action_id: str) -> tuple[list[repo.ActionAttempt], list[repo.Decision]]:
    with db.read() as conn:
        return repo.attempts_for(conn, action_id), repo.verifications_for(conn, action_id)


def test_tc1_live_upload_is_verified_after_reread(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                                  portal_app: FastAPI, tmp_path: Path) -> None:
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.path == ("APPROVED", "EXECUTING", "PENDING_VERIFY", "VERIFIED") and report.uploaded
    (attempt,), (verification,) = history(db, approved.id)
    assert (attempt.outcome, attempt.http_status) == ("accepted", 200)
    assert verification.result == "VERIFIED"
    observed = verification.input_refs["observed"]
    assert observed["requirement_state"] == "Received" and observed["raw_status"] == "Query Raised"
    with db.read() as conn:
        reread = repo.get_evidence(conn, verification.evidence_id)
    assert reread.kind == "document_list" and (tmp_path / "evidence" / reread.screenshot_path).is_file()
    assert [e.outcome for e in portal_app.state.store.upload_log] == ["stored"]
    assert len(stored(portal_app, approved.file_sha256)) == 1


def test_tc4_live_timeout_after_store_is_unknown_then_verified(db: Database, approved: repo.Action,
                                                               executor: act.UploadExecutor,
                                                               portal_app: FastAPI) -> None:
    portal_app.state.store.set_fault("timeout_after_store", once=True)
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.path == ("APPROVED", "EXECUTING", "UNKNOWN", "VERIFIED")
    (attempt,), (verification,) = history(db, approved.id)
    assert attempt.outcome == "timeout" and attempt.deadline_ms == 1000
    assert (verification.input_refs["state_before"], verification.input_refs["state_after"]) == ("UNKNOWN", "VERIFIED")

    again = executor.execute(approved.id, requester_id="P-ARJUN")
    assert not again.uploaded and again.action.state == "VERIFIED" and again.action.attempts == 1
    log = portal_app.state.store.upload_log
    assert len(log) == 1 and log[0].outcome.startswith("stored")
    assert len(stored(portal_app, approved.file_sha256)) == 1  # TC4: never a duplicate


def test_live_timeout_before_store_permits_a_guarded_retry(db: Database, approved: repo.Action,
                                                           executor: act.UploadExecutor, portal_app: FastAPI) -> None:
    portal_app.state.store.set_fault("timeout_before_store", once=True)
    first = executor.execute(approved.id, requester_id="P-ARJUN")
    assert first.path == ("APPROVED", "EXECUTING", "UNKNOWN", "RETRY_ALLOWED")
    assert stored(portal_app, approved.file_sha256) == []
    retry = executor.execute(approved.id, requester_id="P-ARJUN")
    assert retry.path == ("RETRY_ALLOWED", "EXECUTING", "PENDING_VERIFY", "VERIFIED")
    assert [a.outcome for a in history(db, approved.id)[0]] == ["timeout", "accepted"]
    assert len(stored(portal_app, approved.file_sha256)) == 1


def test_live_http_500_is_handled_honestly(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                           portal_app: FastAPI) -> None:
    portal_app.state.store.set_fault("http_500", once=True)
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.path == ("APPROVED", "EXECUTING", "UNKNOWN", "RETRY_ALLOWED")
    (attempt,), _ = history(db, approved.id)
    assert (attempt.outcome, attempt.http_status, attempt.error_code) == ("rejected", 500, "simulated_server_error")
    assert executor.execute(approved.id, requester_id="P-ARJUN").action.state == "VERIFIED"
    assert len(stored(portal_app, approved.file_sha256)) == 1


def test_live_web_flow_card_approve_execute(portal_app: FastAPI, portal_url: str, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole HTTP path: card, the person's approval, execution on the portal thread, read-back."""
    from agent import config as agent_config
    from agent.server import create_app
    from tests.test_consent_web import sign_in as agent_sign_in

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "webevidence"))
    monkeypatch.setenv("PORTAL_BASE_URL", portal_url)
    monkeypatch.setenv("PORTAL_UPLOAD_TIMEOUT_MS", "1000")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as agent:
        csrf = agent_sign_in(agent, "P-ARJUN")
        headers = {**JSON, "x-csrf-token": csrf}
        assert agent.post("/api/portal/session", headers=headers).json()["state"] == "otp_required"
        assert agent.post("/api/portal/session/otp", json={"otp": human_otp(portal_app)},
                          headers=headers).json()["state"] == "authenticated"

        app_db, session = agent.app.state.db, agent.app.state.portal_session
        run = cs.start_run(app_db, "P-ARJUN", CLAIM)
        assert cs.check_consent(app_db, "P-ARJUN", CLAIM, "read_status", run_id=run.id).allowed
        read = session.call("read_claim", CLAIM, run_id=run.id)
        assert extract_claim(app_db, run_id=run.id, evidence_id=read.evidence_id, claim_id=CLAIM).valid
        report = match_claim(app_db, run_id=run.id, requester_id="P-ARJUN", vault_dir=tmp_path / "webvault")
        decision = next(i for i in report.items if i.requirement_id == REQ).decision_id

        proposed = agent.post(f"/api/runs/{run.id}/actions", json={"decision_id": decision}, headers=headers).json()
        action_id = proposed["action"]["id"]
        assert proposed["card"]["destination"]["upload_url"] == f"{portal_url}/claims/{CLAIM}/documents"
        portal_app.state.store.set_fault("timeout_after_store", once=True)
        assert agent.post(f"/api/actions/{action_id}/execute", headers=headers).status_code == 409  # not approved
        assert portal_app.state.store.upload_log == []

        ok = agent.post(f"/api/actions/{action_id}/decision", headers=headers,
                        json={"decision": "approve", "card_sha256": proposed["card_sha256"]})
        assert ok.json()["action"]["state"] == "APPROVED"
        done = agent.post(f"/api/actions/{action_id}/execute", headers=headers).json()
        assert done["path"] == ["APPROVED", "EXECUTING", "UNKNOWN", "VERIFIED"] and done["uploaded"]

        page = agent.get(f"/actions/{action_id}", headers=HTML).text
        assert 'data-state="VERIFIED"' in page and 'data-outcome="timeout"' in page
        assert 'data-result="VERIFIED"' in page
        assert len(stored(portal_app, proposed["card"]["file"]["sha256"])) == 1
