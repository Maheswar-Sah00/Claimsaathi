"""Phase 9: the LangGraph workflow, its interrupts, persistence and recovery.

The real graph, SQLite checkpointer and `PortalClient` run over `OtpStoreDriver`: the portal's real
in-process store (validation, duplicates, fault modes) plus a login that stops at an OTP screen.
The tests play the person with the phone. `test_graph_live.py` runs the same flow in Chromium.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import actions as act
from agent import consent as cs
from agent import repository as repo
from agent.db import Database
from agent.graph import ClaimWorkflow, IntakeRejected, NotWaiting, NotYourRun, WorkflowDeps
from agent.portal_client import LoginState, LoginStatus, OtpRejected, PortalClient, PortalUnavailable, SessionExpired
from agent.seed import init_database
from portal.store import PortalStore
from tests.conftest import HTML, JSON
from tests.test_actions import StoreDriver

CLAIM, REQ = "CLM-2026-0142", "REQ-0142-01"
FILE = "pharmacy_invoice_ramesh.pdf"


class OtpStoreDriver(StoreDriver):
    """StoreDriver whose login stops at an OTP screen until the right code is typed."""

    def __init__(self, store: PortalStore) -> None:
        super().__init__(store)
        self.state = LoginState.LOGGED_OUT
        self.code: str | None = None
        self.logins = 0
        self.down = False

    def login(self, username: str, password: str) -> LoginStatus:
        if self.down:
            raise PortalUnavailable("portal did not respond (test)")
        self.logins += 1
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.state = LoginState.OTP_REQUIRED
        return LoginStatus(LoginState.OTP_REQUIRED, "OTP sent", b"\x89PNG\r\n\x1a\nfake")

    def submit_otp(self, otp: str) -> LoginStatus:
        if self.state is not LoginState.OTP_REQUIRED:
            raise SessionExpired("no OTP is pending")
        if otp != self.code:
            raise OtpRejected("Incorrect OTP.")
        self.state = LoginState.AUTHENTICATED
        return LoginStatus(LoginState.AUTHENTICATED, "Signed in")

    def fetch_claim(self, claim_id: str):  # noqa: ANN201
        if self.down:
            raise PortalUnavailable("portal did not respond (test)", url=f"{self.base_url}/claims/{claim_id}")
        if self.state is not LoginState.AUTHENTICATED:
            raise SessionExpired("not signed in")
        return super().fetch_claim(claim_id)


class Crash(BaseException):
    """Stands in for the process dying mid-node (not an Exception, so nothing catches it)."""


# ---------------- fixtures ----------------

@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def db(tmp_path: Path, vault: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, vault)
    return database


@pytest.fixture
def store(tmp_path: Path) -> PortalStore:
    s = PortalStore(upload_dir=tmp_path / "portal")
    s.reset()
    return s


@pytest.fixture
def driver(store: PortalStore) -> OtpStoreDriver:
    return OtpStoreDriver(store)


def portal_client(db: Database, driver: OtpStoreDriver, tmp_path: Path, vault: Path) -> PortalClient:
    return PortalClient(db, driver, evidence_dir=tmp_path / "evidence", vault_dir=vault, username="arjun",
                        password="demo123", upload_timeout_ms=1000)


def make_flow(db: Database, client: PortalClient, vault: Path, **policy: object) -> ClaimWorkflow:
    return ClaimWorkflow(WorkflowDeps(db=db, portal_for=lambda _person: client,
                                      policy=act.UploadPolicy(vault_dir=vault, portal_base_url="http://portal.test",
                                                              upload_timeout_ms=1000, **policy)))


@pytest.fixture
def flow(db: Database, driver: OtpStoreDriver, tmp_path: Path, vault: Path) -> Iterator[ClaimWorkflow]:
    wf = make_flow(db, portal_client(db, driver, tmp_path, vault), vault)
    yield wf
    wf.close()


def otp(flow: ClaimWorkflow, run_id: str, driver: OtpStoreDriver, code: str | None = None):  # noqa: ANN201
    """The person reads the OTP on their phone and types it."""
    return flow.resume(run_id, person_id="P-ARJUN", answer={"otp": code or driver.code})


def approve(flow: ClaimWorkflow, view, decision: str = "approve"):  # noqa: ANN001, ANN201
    w = view.waiting
    return flow.resume(view.run_id, person_id="P-ARJUN",
                       answer={"decision": decision, "card_sha256": w["card_sha256"], "action_id": w["action_id"]})


def nodes(view) -> list[tuple[str, str]]:  # noqa: ANN001
    return [(s["node"], s["result"]) for s in view.steps]


def stored(store: PortalStore, sha: str) -> list:
    return [u for u in store.get_claim(CLAIM).uploads if u.sha256 == sha]


def actions_in(db: Database) -> list[repo.Action]:
    with db.read() as conn:
        return [repo.Action(**dict(r)) for r in conn.execute("SELECT * FROM actions")]


def to_approval(flow: ClaimWorkflow, driver: OtpStoreDriver):  # noqa: ANN201
    view = flow.start("P-ARJUN", CLAIM, intent="upload")
    assert view.status == "waiting_for_otp"
    view = otp(flow, view.run_id, driver)
    assert view.status == "waiting_for_approval", view.steps
    return view


# ---------------- normal execution ----------------

def test_status_question_runs_end_to_end_and_never_uploads(flow: ClaimWorkflow, driver: OtpStoreDriver,
                                                           db: Database) -> None:
    view = flow.start("P-ARJUN", CLAIM, intent="status")
    assert view.status == "waiting_for_otp" and view.waiting["type"] == "otp" and view.run_state == "awaiting_human"
    assert driver.fetches == 0  # nothing is read before a person types the OTP

    view = otp(flow, view.run_id, driver)
    assert view.status == "completed" and view.run_state == "completed"
    assert nodes(view) == [
        ("intake_validate", "OK"), ("consent_gate", "OK"), ("portal_login", "OK"), ("otp_entry", "INTERRUPTED"),
        ("otp_entry", "OK"), ("read_claim", "OK"), ("extract_and_normalise", "OK"), ("match_documents", "OK"),
        ("verify_and_reply", "OK"),
    ]
    f = view.final_response
    assert (f["canonical_status"], f["raw_status"]) == ("QUERY_RAISED", "Query Raised")
    assert f["status_stamp"].startswith("2026-10-21T14:05")
    docs = {d["requirement_id"]: d for d in f["documents"]}
    assert docs[REQ]["match"] == "MATCHED" and FILE in docs[REQ]["note"]
    assert docs["REQ-0142-02"]["match"] == "HOSPITAL_SHOULD_PROVIDE" and "17(c)" in docs["REQ-0142-02"]["hospital_request"]
    assert f["next_step"].startswith(f"ask me to upload {FILE} for {REQ}")
    assert f["reply_guard"] == "PASSED" and CLAIM in f["text"] and "Next step" in f["text"]
    assert f["evidence"] and all(e.startswith("EV-") for e in f["evidence"])
    assert driver.sent == [] and actions_in(db) == []  # status questions never propose or upload

    with db.read() as conn:  # every node persisted what it was given, decided and found
        steps = repo.steps_for_run(conn, view.run_id)
        run = repo.get_run(conn, view.run_id)
    read = next(s for s in steps if s.node == "read_claim")
    assert read.evidence_ids and read.next_node == "extract_and_normalise" and "Query Raised" in read.decision
    assert next(s for s in steps if s.node == "consent_gate").output_refs["read_decision_id"].startswith("DEC-")
    assert run.final_status == "QUERY_RAISED" and run.finished_at


def test_invalid_requests_stop_at_intake(flow: ClaimWorkflow, driver: OtpStoreDriver) -> None:
    with pytest.raises(IntakeRejected, match="not a claim id"):
        flow.start("P-ARJUN", "0142")
    with pytest.raises(IntakeRejected, match="unknown requester"):
        flow.start("P-NOBODY", CLAIM)
    view = flow.start("P-ARJUN", CLAIM, policy_no="POL-OTHER-1")
    assert view.status == "blocked" and nodes(view) == [("intake_validate", "BLOCKED"), ("finish", "BLOCKED")]
    assert "different policy" in view.final_response["text"] and driver.logins == 0
    view = flow.start("P-ARJUN", "CLM-2026-0999")
    assert view.status == "blocked" and "not a claim on record" in view.final_response["blocked_reason"]


# ---------------- consent ----------------

def test_blocked_consent_never_opens_the_portal(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database) -> None:
    view = flow.start("P-ARJUN", "CLM-2026-0150")  # no consent from Lakshmi (TC2)
    assert view.status == "blocked" and view.run_state == "blocked"
    assert nodes(view) == [("intake_validate", "OK"), ("consent_gate", "BLOCKED"), ("finish", "BLOCKED")]
    f = view.final_response
    assert f["canonical_status"] is None and "have not looked at claim CLM-2026-0150" in f["text"]
    assert f["consent_request_id"] and f["consent_request_id"] in f["next_step"]
    assert driver.logins == 0 and driver.fetches == 0
    with db.read() as conn:
        request = repo.get_consent_request(conn, f["consent_request_id"])
        evidence = repo.evidence_for_run(conn, view.run_id)
    assert request.patient_id == "P-LAKSHMI" and request.status == "pending"
    assert {e.kind for e in evidence} == {"consent_record"}  # nothing from the portal


def test_upload_needs_its_own_consent(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database) -> None:
    view = flow.start("P-ARJUN", "CLM-2026-0163", intent="upload")  # read_status only (seed)
    view = otp(flow, view.run_id, driver)
    assert view.status == "completed" and driver.sent == [] and actions_in(db) == []
    assert view.final_response["canonical_status"] == "UNDER_REVIEW"  # the pre-auth APPROVED trap (TC3)


def test_consent_revoked_mid_run_stops_before_the_read(flow: ClaimWorkflow, driver: OtpStoreDriver) -> None:
    view = flow.start("P-ARJUN", CLAIM)
    cs.revoke(Database(flow.deps.db.path), "CON-SEED-01", by_person_id="P-RAMESH")
    view = otp(flow, view.run_id, driver)
    assert view.status == "blocked" and ("read_claim", "BLOCKED") in nodes(view) and driver.fetches == 0


# ---------------- manual OTP ----------------

def test_manual_otp_wrong_then_right(flow: ClaimWorkflow, driver: OtpStoreDriver) -> None:
    view = flow.start("P-ARJUN", CLAIM)
    view = otp(flow, view.run_id, driver, code="000000" if driver.code != "000000" else "111111")
    assert view.status == "waiting_for_otp" and view.waiting["last_error"] == "Incorrect OTP." and view.waiting["try"] == 2
    with pytest.raises(NotYourRun):  # only the person who asked can answer
        flow.resume(view.run_id, person_id="P-RAMESH", answer={"otp": driver.code})
    with pytest.raises(NotWaiting, match="OTP"):
        flow.resume(view.run_id, person_id="P-ARJUN", answer={"decision": "approve"})
    view = otp(flow, view.run_id, driver)
    assert view.status == "completed" and driver.logins == 1


def test_otp_retries_are_bounded(flow: ClaimWorkflow, driver: OtpStoreDriver) -> None:
    view = flow.start("P-ARJUN", CLAIM)
    for _ in range(20):
        if view.status != "waiting_for_otp":
            break
        view = otp(flow, view.run_id, driver, code="000000" if driver.code != "000000" else "111111")
    assert view.status == "blocked" and driver.logins == 3 and driver.fetches == 0
    assert "could not sign in" in view.final_response["text"]


# ---------------- approval ----------------

def test_rejected_approval_uploads_nothing(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database) -> None:
    view = to_approval(flow, driver)
    card = view.waiting["card"]
    assert card["file"]["filename"] == FILE and card["requirement"]["requirement_id"] == REQ
    assert driver.sent == []
    view = approve(flow, view, "reject")
    assert view.status == "completed" and driver.sent == []
    (action,) = actions_in(db)
    assert action.state == "REJECTED"
    doc = next(d for d in view.final_response["documents"] if d["requirement_id"] == REQ)
    assert doc["action_state"] == "REJECTED" and "chose not to upload" in doc["note"]


def test_successful_upload_is_verified_by_reread(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database,
                                                 store: PortalStore) -> None:
    view = to_approval(flow, driver)
    with pytest.raises(NotWaiting, match="different upload"):
        flow.resume(view.run_id, person_id="P-ARJUN", answer={"decision": "approve", "action_id": "ACT-other"})
    view = approve(flow, view)
    assert view.status == "completed", view.steps
    (action,) = actions_in(db)
    assert action.state == "VERIFIED" and action.attempts == 1 and len(stored(store, action.file_sha256)) == 1
    assert [n for n, _ in nodes(view)][-4:] == ["human_approval", "human_approval", "execute_upload", "verify_and_reply"]
    f = view.final_response
    doc = next(d for d in f["documents"] if d["requirement_id"] == REQ)
    assert doc["portal_state"] == "Received" and doc["action_state"] == "VERIFIED"  # from the fresh re-read
    assert f["next_step"].startswith("send the drafted note to the hospital TPA desk for REQ-0142-02")
    with db.read() as conn:
        verification = repo.verifications_for(conn, action.id)[-1]
    assert verification.evidence_id in f["evidence"]

    with pytest.raises(NotWaiting):  # a repeated approval cannot start anything
        flow.resume(view.run_id, person_id="P-ARJUN", answer={"decision": "approve", "card_sha256": "0" * 64})
    assert flow.continue_run(view.run_id, person_id="P-ARJUN").status == "completed"
    assert len(driver.sent) == 1


def test_mismatched_file_is_never_proposed(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database,
                                           vault: Path) -> None:
    (vault / "seed" / "arjun" / FILE).write_bytes(b"%PDF-1.4 swapped")  # the only good invoice no longer checks out
    view = flow.start("P-ARJUN", CLAIM, intent="upload")
    view = otp(flow, view.run_id, driver)
    assert view.status == "completed" and "human_approval" not in [n for n, _ in nodes(view)]
    assert actions_in(db) == [] and driver.sent == []
    doc = next(d for d in view.final_response["documents"] if d["requirement_id"] == REQ)
    assert doc["match"] == "MISMATCH" and "no suitable file" in doc["note"]
    assert view.final_response["next_step"].startswith("find a pharmacy invoice for REQ-0142-01")


# ---------------- honest reporting ----------------

def test_unknown_status_is_reported_as_uncertain(flow: ClaimWorkflow, driver: OtpStoreDriver,
                                                 store: PortalStore) -> None:
    store.set_status(CLAIM, "Escalated to medical panel")
    view = flow.start("P-ARJUN", CLAIM)
    f = otp(flow, view.run_id, driver).final_response
    assert f["canonical_status"] == "UNCERTAIN" and f["raw_status"] == "Escalated to medical panel"
    assert "could not confirm the status" in f["text"] and "approved" not in f["text"].lower()


def test_portal_failure_is_fetch_failed_not_no_change(flow: ClaimWorkflow, driver: OtpStoreDriver,
                                                      db: Database) -> None:
    view = flow.start("P-ARJUN", CLAIM)
    driver.down = True
    view = otp(flow, view.run_id, driver)
    assert view.status == "failed" and view.run_state == "failed"
    assert ("read_claim", "FETCH_FAILED") in nodes(view) and ("verify_and_reply", "FETCH_FAILED") in nodes(view)
    f = view.final_response
    assert f["fetch"] == "FETCH_FAILED" and f["canonical_status"] == "UNCERTAIN"
    assert "cannot say whether anything changed" in f["text"] and "no change" not in f["text"].lower()
    with db.read() as conn:
        status = [d for d in repo.decisions_for_run(conn, view.run_id) if d.kind == "status"]
    assert [(d.result, d.evidence_state) for d in status] == [("UNCERTAIN", "missing")]


# ---------------- timeouts ----------------

def test_timeout_after_store_recovers_by_rereading(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database,
                                                   store: PortalStore) -> None:
    view = to_approval(flow, driver)
    store.set_fault("timeout_after_store", once=True)
    view = approve(flow, view)
    (action,) = actions_in(db)
    assert view.status == "completed" and action.state == "VERIFIED" and action.attempts == 1
    step = next(s for s in view.steps if s["node"] == "execute_upload")
    assert "UNKNOWN -> VERIFIED" in step["decision"] and "no second upload" in step["decision"]
    assert len(driver.sent) == 1 and len(stored(store, action.file_sha256)) == 1


def test_timeout_before_store_asks_before_retrying(flow: ClaimWorkflow, driver: OtpStoreDriver, db: Database,
                                                   store: PortalStore) -> None:
    view = to_approval(flow, driver)
    store.set_fault("timeout_before_store", once=True)
    view = approve(flow, view)
    assert view.status == "waiting_for_retry" and view.waiting["type"] == "upload_retry"
    assert actions_in(db)[0].state == "RETRY_ALLOWED" and len(driver.sent) == 1
    view = approve(flow, view)
    (action,) = actions_in(db)
    assert view.status == "completed" and action.state == "VERIFIED" and action.attempts == 2
    assert len(stored(store, action.file_sha256)) == 1


def test_uncertain_upload_is_reread_and_never_retried_blind(flow: ClaimWorkflow, driver: OtpStoreDriver,
                                                            db: Database, store: PortalStore) -> None:
    view = to_approval(flow, driver)
    store.set_fault("timeout_after_store", once=True)
    driver.fail_reads_after_send = True  # the file landed, but nothing can be re-read
    view = approve(flow, view)
    (action,) = actions_in(db)
    f = view.final_response
    assert view.status == "completed" and action.state == "UNKNOWN" and len(driver.sent) == 1
    assert f["next_step"].startswith("I will read the portal again before any retry")
    assert "not a confirmation that nothing changed" in f["text"]
    driver.fail_reads_after_send = False  # later, from the Uploads page: a re-read settles it, no resend
    executor = act.UploadExecutor(db, flow.deps.portal_for("P-ARJUN"), flow.deps.policy)
    assert executor.reconcile(action.id, requester_id="P-ARJUN").action.state == "VERIFIED"
    assert len(driver.sent) == 1


# ---------------- persistence and restart ----------------

def test_resume_after_restart_cannot_start_a_duplicate_upload(db: Database, store: PortalStore, tmp_path: Path,
                                                              vault: Path) -> None:
    first_driver = OtpStoreDriver(store)
    first = make_flow(db, portal_client(db, first_driver, tmp_path, vault), vault)
    view = to_approval(first, first_driver)
    run_id, waiting = view.run_id, view.waiting
    first.close()  # the process stops while waiting for the person

    driver = OtpStoreDriver(store)  # a fresh process: new checkpointer connection, portal signed out
    second = make_flow(db, portal_client(db, driver, tmp_path, vault), vault)
    view = second.view(run_id, person_id="P-ARJUN")
    assert view.status == "waiting_for_approval" and view.waiting["action_id"] == waiting["action_id"]
    view = approve(second, view)
    assert view.status == "waiting_for_otp"  # the upload needs the portal: sign in again first
    assert ("execute_upload", "BLOCKED") in nodes(view)
    view = otp(second, run_id, driver)
    assert view.status == "completed"
    (action,) = actions_in(db)
    assert action.state == "VERIFIED" and len(first_driver.sent) + len(driver.sent) == 1
    second.close()

    third = make_flow(db, portal_client(db, OtpStoreDriver(store), tmp_path, vault), vault)
    with pytest.raises(NotWaiting):  # resuming the old approval again starts nothing
        third.resume(run_id, person_id="P-ARJUN", answer={"decision": "approve", "card_sha256": waiting["card_sha256"]})
    assert len(stored(store, action.file_sha256)) == 1
    third.close()


def test_crash_mid_upload_continues_without_a_second_send(db: Database, store: PortalStore, driver: OtpStoreDriver,
                                                          tmp_path: Path, vault: Path) -> None:
    client = portal_client(db, driver, tmp_path, vault)
    first = make_flow(db, client, vault)
    view = to_approval(first, driver)
    original = driver.submit_upload

    def store_then_die(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        original(*args, **kwargs)  # the portal stored the file...
        raise Crash()              # ...and the agent died before recording the answer
    driver.submit_upload = store_then_die
    with pytest.raises(Crash):
        approve(first, view)
    first.close()
    (action,) = actions_in(db)
    assert action.state == "EXECUTING"

    driver.submit_upload = original
    second = make_flow(db, client, vault, stale_after_seconds=0)
    stopped = second.view(view.run_id, person_id="P-ARJUN")
    assert stopped.status == "running" and stopped.next_nodes == ("execute_upload",)
    assert ("execute_upload", "FAILED") in nodes(stopped)
    done = second.continue_run(view.run_id, person_id="P-ARJUN")
    (action,) = actions_in(db)
    assert done.status == "completed" and action.state == "VERIFIED" and action.attempts == 1
    assert len(driver.sent) == 1 and len(stored(store, action.file_sha256)) == 1
    with db.read() as conn:
        assert [a.outcome for a in repo.attempts_for(conn, action.id)] == ["abandoned"]
    second.close()


def test_checkpoints_live_in_the_agent_database(flow: ClaimWorkflow, db: Database) -> None:
    view = flow.start("P-ARJUN", CLAIM)
    with db.read() as conn:
        threads = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert view.run_id in threads and mode == "wal"


# ---------------- the web UI ----------------

@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: OtpStoreDriver) -> Iterator[TestClient]:
    from agent import config as agent_config
    from agent.server import create_app

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("PORTAL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as client:
        app = client.app
        portal = portal_client(app.state.db, driver, tmp_path, tmp_path / "webvault")
        app.state.workflow = make_flow(app.state.db, portal, tmp_path / "webvault")
        yield client


def test_web_start_interrupts_and_steps(agent: TestClient, driver: OtpStoreDriver) -> None:
    from tests.conftest import csrf_from
    from tests.test_consent_web import sign_in, sign_out

    csrf = sign_in(agent, "P-ARJUN")
    headers = {**JSON, "x-csrf-token": csrf}
    started = agent.post("/api/workflows", json={"claim_id": CLAIM, "intent": "upload"}, headers=headers).json()
    run_id = started["run_id"]
    assert started["status"] == "waiting_for_otp" and started["portal_state"] == "otp_required"

    page = agent.get(f"/workflows/{run_id}", headers=HTML).text
    assert 'data-status="waiting_for_otp"' in page and 'id="otp-submit"' in page
    assert 'data-node="consent_gate" data-result="OK"' in page and 'data-node="otp_entry" data-result="INTERRUPTED"' in page

    page = agent.post(f"/workflows/{run_id}/otp", data={"otp": driver.code, "csrf_token": csrf_from(page)},
                      headers=HTML).text
    assert 'data-status="waiting_for_approval"' in page and FILE in page and 'id="approve-button"' in page
    view = agent.get(f"/api/workflows/{run_id}").json()
    page = agent.post(f"/workflows/{run_id}/decision", headers=HTML, data={
        "decision": "approve", "action_id": view["waiting"]["action_id"], "card_sha256": view["waiting"]["card_sha256"],
        "csrf_token": csrf}).text
    assert 'data-status="completed"' in page and 'data-canonical="QUERY_RAISED"' in page
    assert f'data-req-id="{REQ}" data-match="MATCHED" data-action-state="VERIFIED"' in page
    assert 'id="reply-next"' in page and 'id="reply-evidence"' in page
    assert f'data-run-id="{run_id}"' in agent.get("/workflows", headers=HTML).text

    again = agent.post(f"/api/workflows/{run_id}/resume", json={"decision": "approve"}, headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_waiting"
    sign_out(agent, csrf)
    other = sign_in(agent, "P-LAKSHMI")
    assert agent.get(f"/api/workflows/{run_id}").status_code == 404
    assert agent.post(f"/api/workflows/{run_id}/continue", headers={**JSON, "x-csrf-token": other}).status_code == 404


def test_web_blocked_run_is_visible(agent: TestClient) -> None:
    from tests.test_consent_web import sign_in

    csrf = sign_in(agent, "P-ARJUN")
    page = agent.post("/workflows", data={"claim_id": "CLM-2026-0150", "intent": "status", "csrf_token": csrf},
                      headers=HTML).text
    assert 'data-status="blocked"' in page and 'data-node="consent_gate" data-result="BLOCKED"' in page
    assert "The portal was not opened" in page
    bad = agent.post("/api/workflows", json={"claim_id": "nope"}, headers={**JSON, "x-csrf-token": csrf})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"  # rejected at the boundary


def test_cli_workflow_answers_interrupts(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                         driver: OtpStoreDriver, vault: Path, capsys: pytest.CaptureFixture) -> None:
    from agent import cli

    client = portal_client(db, driver, tmp_path, vault)
    monkeypatch.setenv("DATABASE_PATH", str(db.path))
    monkeypatch.setenv("VAULT_DIR", str(vault))
    monkeypatch.setattr("agent.portal_client.build_portal_client", lambda settings, database: client)
    answers = iter([lambda: driver.code, lambda: "approve"])
    args = cli.argparse.Namespace(requester="P-ARJUN", claim=CLAIM, intent="upload", resume=None)
    assert cli._workflow(args, ask=lambda prompt: next(answers)()) == 0
    out = capsys.readouterr().out
    assert f"Upload {FILE} for {REQ}" in out and "Next step:" in out and "verify_and_reply" in out
    assert len(driver.sent) == 1


def test_an_earlier_decision_on_the_same_file_is_reported_not_re_offered(flow: ClaimWorkflow, driver: OtpStoreDriver,
                                                                         db: Database) -> None:
    view = to_approval(flow, driver)
    approve(flow, view, "reject")
    for intent in ("upload", "status"):  # later checks of the same claim
        later = otp(flow, flow.start("P-ARJUN", CLAIM, intent=intent).run_id, driver) \
            if driver.state is not LoginState.AUTHENTICATED else flow.start("P-ARJUN", CLAIM, intent=intent)
        assert later.status == "completed" and "human_approval" not in [n for n, _ in nodes(later)]
        doc = next(d for d in later.final_response["documents"] if d["requirement_id"] == REQ)
        assert doc["action_state"] == "REJECTED" and doc["next_action"] == "Nothing: you chose not to upload it"
        assert not later.final_response["next_step"].startswith("ask me to upload")
    assert len(actions_in(db)) == 1 and driver.sent == []
