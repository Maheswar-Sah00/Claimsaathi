"""Phase 9 acceptance against the live mock portal: the workflow in headless Chromium, with the
person typing the real OTP and approving the real card, checkpointed in SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import actions as act
from agent import repository as repo
from agent.db import Database
from agent.graph import ClaimWorkflow, WorkflowDeps
from agent.portal_client import PortalClient
from agent.seed import init_database
from portal.app import create_app as create_portal
from portal.config import PortalSettings
from tests.conftest import HTML, JSON, serve
from tests.test_portal_client import human_otp, make_client

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
def client(db: Database, portal_url: str, tmp_path: Path) -> Iterator[PortalClient]:
    c = make_client(db, portal_url, tmp_path)  # not signed in: the workflow asks for the OTP
    yield c
    c.close()


@pytest.fixture
def flow(db: Database, client: PortalClient, portal_url: str, tmp_path: Path) -> Iterator[ClaimWorkflow]:
    policy = act.UploadPolicy(vault_dir=tmp_path / "vault", portal_base_url=portal_url, upload_timeout_ms=1000)
    wf = ClaimWorkflow(WorkflowDeps(db=db, portal_for=lambda _person: client, policy=policy))
    yield wf
    wf.close()


def upload_run(flow: ClaimWorkflow, portal_app: FastAPI):  # noqa: ANN201
    view = flow.start("P-ARJUN", CLAIM, intent="upload", test_case="TC1")
    assert view.status == "waiting_for_otp" and view.portal_state == "otp_required"
    view = flow.resume(view.run_id, person_id="P-ARJUN", answer={"otp": human_otp(portal_app)})
    assert view.status == "waiting_for_approval", view.steps
    w = view.waiting
    return flow.resume(view.run_id, person_id="P-ARJUN",
                       answer={"decision": "approve", "card_sha256": w["card_sha256"], "action_id": w["action_id"]})


def stored(portal_app: FastAPI, sha: str) -> list:
    return [u for u in portal_app.state.store.get_claim(CLAIM).uploads if u.sha256 == sha]


def test_tc1_live_workflow_with_human_otp_and_approval(flow: ClaimWorkflow, portal_app: FastAPI, db: Database) -> None:
    view = upload_run(flow, portal_app)
    assert view.status == "completed" and view.run_state == "completed"
    with db.read() as conn:
        (action,) = [repo.Action(**dict(r)) for r in conn.execute("SELECT * FROM actions")]
        evidence = {e.id: e.kind for e in repo.evidence_for_run(conn, view.run_id)}
    assert action.state == "VERIFIED" and len(stored(portal_app, action.file_sha256)) == 1
    f = view.final_response
    assert f["canonical_status"] == "QUERY_RAISED" and f["raw_status"] == "Query Raised" and f["status_stamp"]
    doc = next(d for d in f["documents"] if d["requirement_id"] == REQ)
    assert doc["portal_state"] == "Received" and doc["action_state"] == "VERIFIED"
    assert {evidence[e] for e in f["evidence"] if e in evidence} >= {"portal_page", "document_list"}
    assert [s["node"] for s in view.steps].count("read_claim") == 1  # verify_and_reply re-read separately


def test_tc4_live_workflow_timeout_after_store(flow: ClaimWorkflow, portal_app: FastAPI, db: Database) -> None:
    portal_app.state.store.set_fault("timeout_after_store", once=True)
    view = upload_run(flow, portal_app)
    with db.read() as conn:
        (action,) = [repo.Action(**dict(r)) for r in conn.execute("SELECT * FROM actions")]
    assert view.status == "completed" and action.state == "VERIFIED" and action.attempts == 1
    step = next(s for s in view.steps if s["node"] == "execute_upload")
    assert "UNKNOWN -> VERIFIED" in step["decision"]
    assert len(portal_app.state.store.upload_log) == 1 and len(stored(portal_app, action.file_sha256)) == 1


def test_live_web_workflow(portal_app: FastAPI, portal_url: str, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own wiring: SessionPortal on the portal thread, OTP screen, interrupts over HTTP."""
    from agent import config as agent_config
    from agent.server import create_app
    from tests.test_consent_web import sign_in

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "webevidence"))
    monkeypatch.setenv("PORTAL_BASE_URL", portal_url)
    monkeypatch.setenv("PORTAL_UPLOAD_TIMEOUT_MS", "3000")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as agent:
        csrf = sign_in(agent, "P-ARJUN")
        headers = {**JSON, "x-csrf-token": csrf}
        view = agent.post("/api/workflows", json={"claim_id": CLAIM, "intent": "upload"}, headers=headers).json()
        run_id = view["run_id"]
        assert view["status"] == "waiting_for_otp"
        screen = agent.get(view["waiting"]["otp_screen_url"])
        assert screen.headers["content-type"] == "image/png"  # the person sees the portal's OTP screen
        view = agent.post(f"/api/workflows/{run_id}/resume", json={"otp": human_otp(portal_app)}, headers=headers).json()
        assert view["status"] == "waiting_for_approval"
        w = view["waiting"]
        view = agent.post(f"/api/workflows/{run_id}/resume", headers=headers, json={
            "decision": "approve", "card_sha256": w["card_sha256"], "action_id": w["action_id"]}).json()
        assert view["status"] == "completed" and view["final_response"]["fetch"] == "OK"
        page = agent.get(f"/workflows/{run_id}", headers=HTML).text
        assert 'data-action-state="VERIFIED"' in page and 'data-node="execute_upload" data-result="OK"' in page
        assert len(stored(portal_app, w["card"]["file"]["sha256"])) == 1
