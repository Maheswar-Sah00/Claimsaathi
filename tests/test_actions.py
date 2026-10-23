"""Phase 8: approval, idempotent execution and read-back verification of uploads.

These tests drive the real `PortalClient` (consent, approval and byte checks included) over
`StoreDriver`, which talks to the portal's real `PortalStore`: the same requirement, type and
duplicate validation and the same fault modes, without a browser. A timeout is simulated the way
a client sees it: the call returns "no response" whether or not the store kept the file.
The same scenarios run against the live portal and Chromium in `test_actions_live.py`.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import actions as act
from agent import consent as cs
from agent import repository as repo
from agent.db import Database, to_utc_iso
from agent.extract import extract_claim
from agent.matcher import match_claim
from agent.portal_client import (
    ClaimNotFound,
    ClaimPage,
    ClaimSnapshot,
    LoginState,
    PageArtifacts,
    PortalClient,
    PortalUnavailable,
    PreAuth,
    Requirement,
    UploadedDocument,
    UploadPage,
)
from agent.seed import init_database
from portal.seed import MEMBERS_BY_ID
from portal.store import PortalStore
from shared.errors import AppError
from tests.conftest import HTML, JSON

CLAIM, REQ = "CLM-2026-0142", "REQ-0142-01"
BASE = "http://portal.test"


# ---------------- a browserless driver over the real portal store ----------------

class StoreDriver:
    """`PortalDriver` over an in-process `PortalStore`. Thread-safe; counts what reaches the portal."""

    base_url = BASE

    def __init__(self, store: PortalStore) -> None:
        self.store = store
        self.state = LoginState.AUTHENTICATED
        self.fetches = 0
        self.sent: list[tuple[str, str, str]] = []
        self.on_fetch: Callable[[int], None] | None = None   # called with the fetch number, before reading
        self.before_send: Callable[[], None] | None = None
        self.fail_reads_after_send = False
        self.accept_without_storing = False
        self._lock = threading.Lock()

    def login(self, username: str, password: str):  # noqa: ANN201 - unused: already signed in
        raise AssertionError("not used")

    submit_otp = login

    def close(self) -> None:
        pass

    def _page(self, claim_id: str, text: str) -> PageArtifacts:
        return PageArtifacts(url=f"{BASE}/claims/{claim_id}", html=f"<html><body>{text}</body></html>",
                             screenshot=None, text=text)

    def fetch_claim(self, claim_id: str) -> ClaimPage:
        with self._lock:
            self.fetches += 1
            number = self.fetches
            failing = self.fail_reads_after_send and self.sent
        if self.on_fetch:
            self.on_fetch(number)
        if failing:
            raise PortalUnavailable("portal did not respond (test)", url=f"{BASE}/claims/{claim_id}")
        try:
            claim = self.store.get_claim(claim_id)
        except AppError as exc:
            raise ClaimNotFound(exc.message) from exc
        with self.store.lock:
            member = MEMBERS_BY_ID[claim.patient_id]
            snapshot = ClaimSnapshot(
                claim_id=claim.claim_id, policy_no=claim.policy_no, patient_name=member.name,
                patient_member_id=member.member_id, route=claim.route, hospital=claim.hospital,
                admission_date=claim.admission_date, discharge_date=claim.discharge_date,
                amount_claimed_inr=claim.amount_claimed, raw_status=claim.raw_status,
                status_updated_at=claim.status_updated_at.isoformat(),
                status_updated_at_utc=to_utc_iso(claim.status_updated_at),
                preauth=PreAuth(claim.preauth.reference, claim.preauth.status, claim.preauth.amount,
                                claim.preauth.decided_at.isoformat()) if claim.preauth else None,
                requirements=tuple(Requirement(r.req_id, r.description, r.doc_type, r.due_date, r.state)
                                   for r in claim.requirements),
                uploaded=tuple(UploadedDocument(u.doc_id, u.filename, u.size, u.sha256, u.requirement_id,
                                                u.uploaded_at.isoformat()) for u in claim.uploads),
            )
        return ClaimPage(snapshot, self._page(claim_id, f"claim {claim_id} {claim.raw_status}"))

    def submit_upload(self, claim_id: str, requirement_id: str, filename: str, data: bytes, content_type: str,
                      timeout_ms: int) -> UploadPage:
        """Mirror of the portal's upload route (validation, fault, store), as seen from the client."""
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            self.sent.append((claim_id, requirement_id, sha))
        if self.before_send:
            self.before_send()
        store, page = self.store, self._page(claim_id, "upload result")
        try:
            sha, ctype, ext = store.validate_upload(claim_id, requirement_id, filename, data, 1024 * 1024)
        except AppError as exc:
            store.log_upload(claim_id, requirement_id, filename, "", f"rejected: {exc.code}", "none")
            return UploadPage("rejected", error_code=exc.code, message=exc.message, http_status=exc.status_code,
                              artifacts=page)
        fault = store.take_fault()
        if fault == "http_500":
            store.log_upload(claim_id, requirement_id, filename, sha, "not stored (simulated HTTP 500)", fault)
            return UploadPage("rejected", error_code="simulated_server_error", message="Simulated server error",
                              http_status=500, artifacts=page)
        if fault == "timeout_before_store":
            store.log_upload(claim_id, requirement_id, filename, sha, "not stored (simulated timeout)", fault)
            return UploadPage("timeout", message=f"no response within {timeout_ms} ms")
        if self.accept_without_storing:
            return UploadPage("accepted", doc_id="DOC-GHOST", sha256=sha, http_status=200, artifacts=page)
        doc = store.store_upload(claim_id, requirement_id, filename, data, sha, ctype, ext)
        store.log_upload(claim_id, requirement_id, filename, sha, "stored", fault)
        if fault == "timeout_after_store":
            return UploadPage("timeout", message=f"no response within {timeout_ms} ms")
        return UploadPage("accepted", doc_id=doc.doc_id, sha256=doc.sha256, http_status=200, artifacts=page)


# ---------------- fixtures and helpers ----------------

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
def driver(store: PortalStore) -> StoreDriver:
    return StoreDriver(store)


@pytest.fixture
def client(db: Database, driver: StoreDriver, tmp_path: Path, vault: Path) -> PortalClient:
    return PortalClient(db, driver, evidence_dir=tmp_path / "evidence", vault_dir=vault, username="arjun",
                        password="demo123", upload_timeout_ms=1000)


@pytest.fixture
def policy(vault: Path) -> act.UploadPolicy:
    return act.UploadPolicy(vault_dir=vault, portal_base_url=BASE, upload_timeout_ms=1000)


@pytest.fixture
def executor(db: Database, client: PortalClient, policy: act.UploadPolicy) -> act.UploadExecutor:
    return act.UploadExecutor(db, client, policy)


def proposal(db: Database, client: PortalClient, vault: Path, requester: str = "P-ARJUN") -> repo.Action:
    """The real flow up to a PROPOSED upload: consented read, DOM extraction, match, propose."""
    run = cs.start_run(db, requester, CLAIM)
    assert cs.check_consent(db, requester, CLAIM, "read_status", run_id=run.id).allowed
    read = client.read_claim(CLAIM, run_id=run.id)
    assert extract_claim(db, run_id=run.id, evidence_id=read.evidence_id, claim_id=CLAIM).valid
    report = match_claim(db, run_id=run.id, requester_id=requester, vault_dir=vault)
    item = next(i for i in report.items if i.requirement_id == REQ)
    assert item.vault_file_id == "VF-SEED-01"
    action, created = act.propose_from_match(db, run_id=run.id, decision_id=item.decision_id, person_id=requester)
    assert created and action.state == "PROPOSED"
    return action


def approve(db: Database, action: repo.Action, policy: act.UploadPolicy, person: str = "P-ARJUN") -> repo.Action:
    card = act.approval_card(db, action.id, person_id=person, policy=policy)
    assert card.can_approve, [c for c in card.checks if not c.ok]
    approved, new = act.approve_upload(db, action.id, person_id=person, card_sha256=card.card_sha256, policy=policy)
    assert new and approved.state == "APPROVED"
    return approved


@pytest.fixture
def approved(db: Database, client: PortalClient, vault: Path, policy: act.UploadPolicy) -> repo.Action:
    return approve(db, proposal(db, client, vault), policy)


def stored(store: PortalStore, sha: str) -> list:
    return [u for u in store.get_claim(CLAIM).uploads if u.sha256 == sha]


def current(db: Database, action_id: str) -> repo.Action:
    with db.read() as conn:
        return repo.get_action(conn, action_id)


def attempts(db: Database, action_id: str) -> list[repo.ActionAttempt]:
    with db.read() as conn:
        return repo.attempts_for(conn, action_id)


def verifications(db: Database, action_id: str) -> list[repo.Decision]:
    with db.read() as conn:
        return repo.verifications_for(conn, action_id)


# ---------------- idempotency key ----------------

def test_idempotency_key_is_canonical_and_unambiguous() -> None:
    sha = hashlib.sha256(b"x").hexdigest()
    key = repo.idempotency_key(CLAIM, REQ, sha)
    parts = (b"claimsaathi/upload-idempotency/v1", CLAIM.encode(), REQ.encode(), sha.encode())
    assert key == hashlib.sha256(b"".join(len(p).to_bytes(4, "big") + p for p in parts)).hexdigest()
    assert key == repo.idempotency_key(CLAIM, REQ, sha)
    assert len({key, repo.idempotency_key(CLAIM, "REQ-0142-02", sha), repo.idempotency_key("CLM-2026-0150", REQ, sha),
                repo.idempotency_key(CLAIM, REQ, hashlib.sha256(b"y").hexdigest())}) == 4
    for bad in ("REQ|0142", "", "REQ 01", "x" * 65):
        with pytest.raises(repo.ValidationError, match="requirement id"):
            repo.idempotency_key(CLAIM, bad, sha)


def test_same_claim_requirement_file_is_one_action_across_runs(db: Database, client: PortalClient,
                                                               vault: Path) -> None:
    first = proposal(db, client, vault)
    run = cs.start_run(db, "P-ARJUN", CLAIM)
    assert cs.check_consent(db, "P-ARJUN", CLAIM, "read_status", run_id=run.id).allowed
    read = client.read_claim(CLAIM, run_id=run.id)
    assert extract_claim(db, run_id=run.id, evidence_id=read.evidence_id, claim_id=CLAIM).valid
    item = next(i for i in match_claim(db, run_id=run.id, requester_id="P-ARJUN", vault_dir=vault).items
                if i.requirement_id == REQ)
    again, created = act.propose_from_match(db, run_id=run.id, decision_id=item.decision_id, person_id="P-ARJUN")
    assert not created and again.id == first.id and again.idem_key == first.idem_key


# ---------------- approval card and the human decision ----------------

def test_card_shows_everything_needed_to_decide(db: Database, client: PortalClient, vault: Path,
                                                policy: act.UploadPolicy) -> None:
    action = proposal(db, client, vault)
    card = act.approval_card(db, action.id, person_id="P-ARJUN", policy=policy)
    c = card.card
    assert c["patient"] == {"id": "P-RAMESH", "name": "Ramesh"}
    assert c["claim"]["claim_id"] == CLAIM and c["claim"]["policy_no"] == "POL-HDF-778812"
    assert c["requirement"]["requirement_id"] == REQ and "Pharmacy invoice" in c["requirement"]["description"]
    assert c["file"]["filename"] == "pharmacy_invoice_ramesh.pdf" and c["file"]["preview_url"] == "/vault/files/VF-SEED-01"
    assert c["file"]["sha256"] == action.file_sha256
    assert c["matching_evidence"]["result"] == "MATCHED" and c["matching_evidence"]["portal_evidence_id"]
    assert c["destination"]["upload_url"] == f"{BASE}/claims/{CLAIM}/documents" and c["destination"]["requirement_id"] == REQ
    assert card.can_approve and all(ch.ok for ch in card.checks)
    assert card.card_sha256 == repo.card_digest(c)


def test_rejection_causes_zero_uploads(db: Database, client: PortalClient, vault: Path, policy: act.UploadPolicy,
                                       executor: act.UploadExecutor, driver: StoreDriver, store: PortalStore) -> None:
    action = proposal(db, client, vault)
    rejected, new = act.reject_upload(db, action.id, person_id="P-ARJUN", policy=policy)
    assert new and rejected.state == "REJECTED"
    report = executor.execute(action.id, requester_id="P-ARJUN")
    assert report.action.state == "REJECTED" and not report.uploaded
    assert driver.sent == [] and store.upload_log == []
    with pytest.raises(repo.InvalidTransition, match="already rejected"):
        act.approve_upload(db, action.id, person_id="P-ARJUN", card_sha256="0" * 64, policy=policy)
    with db.read() as conn:
        assert repo.get_action_approval(conn, action.id).decision == "rejected"


def test_nothing_runs_without_approval(db: Database, client: PortalClient, vault: Path,
                                       executor: act.UploadExecutor, driver: StoreDriver) -> None:
    action = proposal(db, client, vault)
    with pytest.raises(act.ApprovalRequired):
        executor.execute(action.id, requester_id="P-ARJUN")
    with pytest.raises(repo.InvalidTransition), db.transaction() as conn:
        repo.transition_action(conn, action.id, "EXECUTING")
    with pytest.raises(sqlite3.IntegrityError, match="recorded human approval"), db.transaction() as conn:
        conn.execute("UPDATE actions SET state = 'EXECUTING', attempts = 1, approved_by = 'P-ARJUN', "
                     "approved_at = updated_at WHERE id = ?", (action.id,))
    assert driver.sent == []


def test_approval_needs_the_shown_card_and_the_requester(db: Database, client: PortalClient, vault: Path,
                                                        policy: act.UploadPolicy) -> None:
    action = proposal(db, client, vault)
    card = act.approval_card(db, action.id, person_id="P-ARJUN", policy=policy)
    with pytest.raises(act.NotYourAction):
        act.approval_card(db, action.id, person_id="P-LAKSHMI", policy=policy)
    with pytest.raises(act.NotYourAction):
        act.approve_upload(db, action.id, person_id="P-RAMESH", card_sha256=card.card_sha256, policy=policy)

    with db.transaction() as conn:  # details change after the card was shown
        repo.update_vault_fields(conn, "VF-SEED-01", source="human", invoice_no="INV-PH-2291-B")
    with pytest.raises(act.CardChanged):
        act.approve_upload(db, action.id, person_id="P-ARJUN", card_sha256=card.card_sha256, policy=policy)
    assert current(db, action.id).state == "PROPOSED"
    approve(db, action, policy)
    with db.read() as conn:
        approval = repo.get_action_approval(conn, action.id)
    assert approval.decided_by == "P-ARJUN" and approval.decided_at and approval.card["file"]["invoice_no"] == "INV-PH-2291-B"
    assert approval.card_sha256 == repo.card_digest(approval.card)
    assert (approval.claim_id, approval.requirement_id, approval.file_sha256) == (CLAIM, REQ, action.file_sha256)


def test_duplicate_approval_is_idempotent(db: Database, approved: repo.Action, policy: act.UploadPolicy,
                                          executor: act.UploadExecutor, driver: StoreDriver,
                                          store: PortalStore) -> None:
    card = act.approval_card(db, approved.id, person_id="P-ARJUN", policy=policy)
    again, new = act.approve_upload(db, approved.id, person_id="P-ARJUN", card_sha256=card.card_sha256, policy=policy)
    assert not new and again.state == "APPROVED" and again.approved_at == approved.approved_at
    with pytest.raises(repo.InvalidTransition, match="already approved"):
        act.reject_upload(db, approved.id, person_id="P-ARJUN", policy=policy)

    assert executor.execute(approved.id, requester_id="P-ARJUN").action.state == "VERIFIED"
    later, new = act.approve_upload(db, approved.id, person_id="P-ARJUN", card_sha256="0" * 64, policy=policy)
    assert not new and later.state == "VERIFIED"
    second = executor.execute(approved.id, requester_id="P-ARJUN")
    assert not second.uploaded and second.action.state == "VERIFIED"
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_approvals WHERE action_id = ?",
                            (approved.id,)).fetchone()[0] == 1


def test_approval_is_bound_to_one_claim_requirement_and_file(db: Database, approved: repo.Action) -> None:
    other = hashlib.sha256(b"another file").hexdigest()
    for column, value in (("file_sha256", other), ("requirement_id", "REQ-0142-02"), ("claim_id", "CLM-2026-0163"),
                          ("vault_file_id", "VF-SEED-02"), ("approved_by", "P-RAMESH")):
        with pytest.raises(sqlite3.IntegrityError, match="cannot be re-pointed"), db.transaction() as conn:
            conn.execute(f"UPDATE actions SET {column} = ? WHERE id = ?", (value, approved.id))  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.transaction() as conn:
        conn.execute("UPDATE action_approvals SET file_sha256 = ? WHERE action_id = ?", (other, approved.id))
    with pytest.raises(sqlite3.IntegrityError, match="never reopened"), db.transaction() as conn:
        conn.execute("UPDATE actions SET state = 'PROPOSED' WHERE id = ?", (approved.id,))
    with pytest.raises(repo.ConstraintViolation, match="bound to one proposed action"), db.transaction() as conn:
        repo._execute(conn, "INSERT INTO action_approvals (id, action_id, decision, decided_by, decided_at, idem_key, "
                      "claim_id, requirement_id, file_sha256, vault_file_id) SELECT 'APR-x', id, 'approved', "
                      "'P-ARJUN', updated_at, idem_key, claim_id, requirement_id, ?, vault_file_id FROM actions "
                      "WHERE id = ?", (other, approved.id), "approve another file")


# ---------------- TC1: approval uploads once, verified by re-reading ----------------

def test_tc1_approval_uploads_once_and_is_verified_by_reread(db: Database, approved: repo.Action,
                                                             executor: act.UploadExecutor, driver: StoreDriver,
                                                             store: PortalStore) -> None:
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.uploaded and report.verification == "VERIFIED"
    assert report.path == ("APPROVED", "EXECUTING", "PENDING_VERIFY", "VERIFIED")
    assert report.action.state == "VERIFIED" and report.action.attempts == 1 and report.action.last_error is None
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1

    (attempt,) = attempts(db, approved.id)
    assert (attempt.outcome, attempt.http_status, attempt.finished_at is not None) == ("accepted", 200, True)
    (verification,) = verifications(db, approved.id)
    refs = verification.input_refs
    assert verification.result == "VERIFIED" and verification.evidence_state == "linked"
    assert refs["intended"] == {"claim_id": CLAIM, "requirement_id": REQ, "file_sha256": approved.file_sha256,
                                "vault_file_id": "VF-SEED-01"}
    observed = refs["observed"]
    assert [d["sha256"] for d in observed["same_file_under_requirement"]] == [approved.file_sha256]
    assert observed["raw_status"] == "Query Raised" and observed["status_updated_at"].startswith("2026-10-21T14:05")
    assert observed["requirement_state"] == "Received" and (refs["state_before"], refs["state_after"]) == (
        "PENDING_VERIFY", "VERIFIED")
    with db.read() as conn:  # the verdict rests on a fresh document-list read made after the upload answer
        upload_ev = repo.get_evidence(conn, attempt.evidence_id)
        reread_ev = repo.get_evidence(conn, verification.evidence_id)
    assert upload_ev.kind == "upload_response" and reread_ev.kind == "document_list"
    assert (reread_ev.fetched_at, reread_ev.id) != (upload_ev.fetched_at, upload_ev.id)


def test_http_200_alone_is_not_verification(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                            driver: StoreDriver) -> None:
    driver.accept_without_storing = True  # confirmation page with our hash, but nothing stored
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.verification == "NOT_FOUND" and report.action.state == "FAILED"
    assert "does not list this file" in report.action.last_error
    assert attempts(db, approved.id)[0].outcome == "accepted"


def test_definite_refusal_is_settled_by_reread_not_by_status(db: Database, approved: repo.Action,
                                                             executor: act.UploadExecutor, driver: StoreDriver,
                                                             store: PortalStore, vault: Path) -> None:
    data = (vault / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").read_bytes()
    # Someone else uploads the same file between our checks and our send: the portal answers 409.
    driver.before_send = lambda: store.store_upload(CLAIM, REQ, "same.pdf", data, approved.file_sha256,
                                                    "application/pdf", "pdf")
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    (attempt,) = attempts(db, approved.id)
    assert (attempt.outcome, attempt.http_status, attempt.error_code) == ("rejected", 409, "duplicate_upload")
    assert report.path == ("APPROVED", "EXECUTING", "PENDING_VERIFY", "VERIFIED")
    assert len(stored(store, approved.file_sha256)) == 1


# ---------------- TC4 and the other fault modes ----------------

def test_tc4_timeout_after_store_is_unknown_then_verified_with_one_file(db: Database, approved: repo.Action,
                                                                        executor: act.UploadExecutor,
                                                                        driver: StoreDriver,
                                                                        store: PortalStore) -> None:
    store.set_fault("timeout_after_store", once=True)
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.uploaded and report.path == ("APPROVED", "EXECUTING", "UNKNOWN", "VERIFIED")
    assert report.action.state == "VERIFIED" and report.action.attempts == 1
    assert "no second upload" in report.message
    (attempt,) = attempts(db, approved.id)
    assert attempt.outcome == "timeout" and attempt.http_status is None
    (verification,) = verifications(db, approved.id)
    assert (verification.input_refs["state_before"], verification.input_refs["state_after"]) == ("UNKNOWN", "VERIFIED")

    # Retries, reconnects and re-proposals never send it again.
    assert not executor.execute(approved.id, requester_id="P-ARJUN").uploaded
    assert not executor.reconcile(approved.id, requester_id="P-ARJUN").uploaded
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1
    assert [e.outcome for e in store.upload_log] == ["stored"]


def test_timeout_before_store_permits_a_guarded_retry(db: Database, approved: repo.Action,
                                                      executor: act.UploadExecutor, driver: StoreDriver,
                                                      store: PortalStore) -> None:
    store.set_fault("timeout_before_store", once=True)
    first = executor.execute(approved.id, requester_id="P-ARJUN")
    assert first.path == ("APPROVED", "EXECUTING", "UNKNOWN", "RETRY_ALLOWED")
    assert first.verification == "NOT_FOUND" and stored(store, approved.file_sha256) == []
    assert first.checks and all(c.ok for c in first.checks)  # consent and every rule re-checked for the retry
    assert {c.name for c in first.checks} >= {"consent", "approval", "file_registered", "patient", "legible",
                                              "requirement", "document_type", "document_date", "not_on_claim",
                                              "attempts"}

    retry = executor.execute(approved.id, requester_id="P-ARJUN")
    assert retry.uploaded and retry.path == ("RETRY_ALLOWED", "EXECUTING", "PENDING_VERIFY", "VERIFIED")
    assert retry.action.attempts == 2 and len(driver.sent) == 2
    assert len(stored(store, approved.file_sha256)) == 1
    assert [a.outcome for a in attempts(db, approved.id)] == ["timeout", "accepted"]
    with db.read() as conn:
        assert [c.passed for c in repo.checks_for(conn, approved.id) if c.phase == "execute"] == [True, True]


def test_retry_rechecks_consent_first(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                      driver: StoreDriver, store: PortalStore) -> None:
    store.set_fault("timeout_before_store", once=True)
    assert executor.execute(approved.id, requester_id="P-ARJUN").action.state == "RETRY_ALLOWED"
    cs.revoke(db, "CON-SEED-01", by_person_id="P-RAMESH")
    with pytest.raises(cs.ConsentDenied):
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert len(driver.sent) == 1 and current(db, approved.id).state == "RETRY_ALLOWED"


def test_http_500_is_unknown_until_the_reread_then_retry(db: Database, approved: repo.Action,
                                                         executor: act.UploadExecutor, driver: StoreDriver,
                                                         store: PortalStore) -> None:
    store.set_fault("http_500", once=True)
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.path == ("APPROVED", "EXECUTING", "UNKNOWN", "RETRY_ALLOWED")
    (attempt,) = attempts(db, approved.id)
    assert (attempt.outcome, attempt.http_status, attempt.error_code) == ("rejected", 500, "simulated_server_error")
    assert "HTTP 500" in report.action.last_error and "not on" in report.message
    assert executor.execute(approved.id, requester_id="P-ARJUN").action.state == "VERIFIED"
    assert len(stored(store, approved.file_sha256)) == 1


def test_attempts_are_bounded(db: Database, approved: repo.Action, client: PortalClient, vault: Path,
                              store: PortalStore) -> None:
    executor = act.UploadExecutor(db, client, act.UploadPolicy(vault_dir=vault, portal_base_url=BASE,
                                                               max_attempts=1))
    store.set_fault("timeout_before_store", once=True)
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.action.state == "FAILED" and "1 of 1 attempts used" in report.action.last_error


# ---------------- UNKNOWN is never papered over ----------------

def test_unknown_survives_a_failed_reread_and_a_reconnect_never_resets_it(
        db: Database, approved: repo.Action, executor: act.UploadExecutor, driver: StoreDriver,
        store: PortalStore, client: PortalClient, vault: Path, policy: act.UploadPolicy) -> None:
    store.set_fault("timeout_after_store", once=True)
    driver.fail_reads_after_send = True
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.action.state == "UNKNOWN" and report.verification == "FETCH_FAILED"
    assert "re-read failed" in report.action.last_error
    assert verifications(db, approved.id)[-1].evidence_state == "linked"  # the failed read is evidence too

    # A reconnecting client proposes, approves and executes again: still the same UNKNOWN action, no upload.
    with db.read() as conn:
        match_id = approved.match_decision_id
        run_id = approved.run_id
    again, created = act.propose_from_match(db, run_id=run_id, decision_id=match_id, person_id="P-ARJUN")
    assert not created and again.id == approved.id and again.state == "UNKNOWN"
    assert act.approve_upload(db, approved.id, person_id="P-ARJUN", card_sha256="0" * 64, policy=policy)[0].state == "UNKNOWN"
    assert executor.execute(approved.id, requester_id="P-ARJUN").action.state == "UNKNOWN"
    with pytest.raises(sqlite3.IntegrityError, match="never reopened"), db.transaction() as conn:
        conn.execute("UPDATE actions SET state = 'PROPOSED' WHERE id = ?", (approved.id,))
    assert len(driver.sent) == 1

    driver.fail_reads_after_send = False
    settled = executor.reconcile(approved.id, requester_id="P-ARJUN")
    assert settled.action.state == "VERIFIED" and not settled.uploaded
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1


def test_an_attempt_that_never_reported_back_is_reconciled(db: Database, approved: repo.Action, client: PortalClient,
                                                           vault: Path, driver: StoreDriver) -> None:
    with db.transaction() as conn:  # the process stopped after claiming the attempt, before sending
        check = repo.record_action_check(conn, action_id=approved.id, phase="execute", passed=True,
                                         checks=[{"name": "test", "ok": True, "detail": "simulated"}])
        repo.transition_action(conn, approved.id, "EXECUTING")
        repo.start_attempt(conn, action_id=approved.id, check_id=check.id, deadline_ms=1000)
    fresh = act.UploadExecutor(db, client, act.UploadPolicy(vault_dir=vault, portal_base_url=BASE))
    busy = fresh.execute(approved.id, requester_id="P-ARJUN")
    assert busy.action.state == "EXECUTING" and "in progress" in busy.message
    with pytest.raises(repo.ConstraintViolation, match="needs an EXECUTING action"), db.transaction() as conn:
        repo._execute(conn, "INSERT INTO action_attempts (id, action_id, attempt_no, check_id, started_at, deadline_ms) "
                      "VALUES ('ATT-x', ?, 2, ?, ?, 1000)", (approved.id, check.id, check.checked_at), "second open")

    stale = act.UploadExecutor(db, client, act.UploadPolicy(vault_dir=vault, portal_base_url=BASE,
                                                            stale_after_seconds=0))
    report = stale.reconcile(approved.id, requester_id="P-ARJUN")
    assert report.path == ("EXECUTING", "UNKNOWN", "RETRY_ALLOWED") and not report.uploaded
    assert attempts(db, approved.id)[0].outcome == "abandoned" and driver.sent == []


# ---------------- checks immediately before execution ----------------

def test_revoked_consent_blocks_execution(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                          driver: StoreDriver) -> None:
    cs.revoke(db, "CON-SEED-01", by_person_id="P-RAMESH")
    fetches = driver.fetches
    with pytest.raises(cs.ConsentDenied) as denied:
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert denied.value.decision.result.value == "EXPIRED_OR_REVOKED"
    assert driver.fetches == fetches and driver.sent == []  # the portal was not even read
    assert current(db, approved.id).state == "APPROVED"
    with db.read() as conn:
        (check,) = repo.checks_for(conn, approved.id)[-1:]
    assert check.phase == "execute" and not check.passed and check.checks[0]["name"] == "consent"


def test_consent_revoked_during_the_final_checks_still_blocks(db: Database, approved: repo.Action,
                                                              executor: act.UploadExecutor,
                                                              driver: StoreDriver) -> None:
    driver.on_fetch = lambda n: cs.revoke(db, "CON-SEED-01", by_person_id="P-RAMESH")  # revoked mid-preflight
    with pytest.raises(cs.ConsentDenied):
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert driver.sent == [] and current(db, approved.id).state == "APPROVED"


def test_wrong_patient_file_is_blocked(db: Database, client: PortalClient, vault: Path, policy: act.UploadPolicy,
                                       executor: act.UploadExecutor, driver: StoreDriver) -> None:
    from tests.conftest import matched

    # A buggy component records MATCHED for Lakshmi's invoice: the card shows why it cannot be approved.
    run = cs.start_run(db, "P-ARJUN", CLAIM)
    with db.read() as conn:
        lakshmi = repo.get_vault_file(conn, "VF-SEED-02")
    with db.transaction() as conn:
        forged, _ = repo.propose_action(conn, run_id=run.id, claim_id=CLAIM, requirement_id=REQ,
                                        file_sha256=lakshmi.sha256, vault_file_id=lakshmi.id,
                                        match_decision_id=matched(conn, run.id, REQ, lakshmi.sha256, lakshmi.id))
    card = act.approval_card(db, forged.id, person_id="P-ARJUN", policy=policy)
    patient = next(c for c in card.checks if c.name == "patient")
    assert not card.can_approve and not patient.ok and "is for Lakshmi" in patient.detail
    with pytest.raises(act.NotEligible, match="is for Lakshmi"):
        act.approve_upload(db, forged.id, person_id="P-ARJUN", card_sha256=card.card_sha256, policy=policy)

    # An approved file whose details turn out to name another patient is stopped before sending.
    approved = approve(db, proposal(db, client, vault), policy)
    with db.transaction() as conn:
        repo.update_vault_fields(conn, "VF-SEED-01", source="human", patient_name="Lakshmi")
    with pytest.raises(act.ExecutionBlocked, match="is for Lakshmi, but CLM-2026-0142 is for Ramesh"):
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert driver.sent == [] and current(db, approved.id).state == "APPROVED"


@pytest.mark.parametrize("change, needle", [
    (lambda vault, db: (vault / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").write_bytes(b"%PDF-1.4 swapped"),
     "no longer match the registered SHA-256"),
    (lambda vault, db: _set_fields(db, doc_date="2026-08-02"), "outside 2026-10-14 to 2026-10-18"),
    (lambda vault, db: _set_fields(db, legible=False), "not readable"),
    (lambda vault, db: _set_fields(db, doc_type="discharge_summary"), "not a pharmacy invoice"),
])
def test_every_rule_is_checked_again_before_sending(change, needle: str, db: Database, approved: repo.Action,
                                                    executor: act.UploadExecutor, driver: StoreDriver,
                                                    vault: Path) -> None:
    change(vault, db)
    with pytest.raises(act.ExecutionBlocked, match=needle):
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert driver.sent == [] and current(db, approved.id).state == "APPROVED"


def _set_fields(db: Database, **fields) -> None:  # noqa: ANN003
    with db.transaction() as conn:
        repo.update_vault_fields(conn, "VF-SEED-01", source="human", **fields)


def test_requirement_no_longer_pending_blocks(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                              driver: StoreDriver, store: PortalStore) -> None:
    store.get_claim(CLAIM).requirements[0].state = "Waived"
    with pytest.raises(act.ExecutionBlocked, match="Waived, no longer pending"):
        executor.execute(approved.id, requester_id="P-ARJUN")
    assert driver.sent == []


def test_file_already_on_the_claim_is_verified_without_sending(db: Database, approved: repo.Action,
                                                               executor: act.UploadExecutor, driver: StoreDriver,
                                                               store: PortalStore, vault: Path) -> None:
    data = (vault / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").read_bytes()
    store.store_upload(CLAIM, REQ, "by_hand.pdf", data, approved.file_sha256, "application/pdf", "pdf")
    report = executor.execute(approved.id, requester_id="P-ARJUN")
    assert report.action.state == "VERIFIED" and report.action.attempts == 0 and not report.uploaded
    assert driver.sent == [] and "Nothing was sent" in report.message


# ---------------- concurrency ----------------

def _race(executor: act.UploadExecutor, action_id: str, n: int) -> list[act.ExecutionReport | BaseException]:
    results: list[act.ExecutionReport | BaseException] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            out: act.ExecutionReport | BaseException = executor.execute(action_id, requester_id="P-ARJUN")
        except BaseException as exc:  # noqa: BLE001 - collected and asserted on below
            out = exc
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def test_concurrent_duplicate_requests_cannot_upload_twice(db: Database, approved: repo.Action,
                                                           executor: act.UploadExecutor, driver: StoreDriver,
                                                           store: PortalStore) -> None:
    gate = threading.Barrier(2, timeout=10)
    first_reads = driver.fetches
    # Both requests finish their checks at the same moment, then race for the attempt.
    driver.on_fetch = lambda n: gate.wait() if n in (first_reads + 1, first_reads + 2) else None
    results = _race(executor, approved.id, 2)
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert sum(r.uploaded for r in results) == 1
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1
    final = current(db, approved.id)
    assert final.state == "VERIFIED" and final.attempts == 1 and len(attempts(db, approved.id)) == 1


def test_many_concurrent_requests_upload_once(db: Database, approved: repo.Action, executor: act.UploadExecutor,
                                              driver: StoreDriver, store: PortalStore) -> None:
    store.set_fault("timeout_after_store", once=True)
    results = _race(executor, approved.id, 8)
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert len(driver.sent) == 1 and len(stored(store, approved.file_sha256)) == 1
    assert current(db, approved.id).state == "VERIFIED"


# ---------------- the approval page ----------------

@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from agent import config as agent_config
    from agent.server import create_app

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("PORTAL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as client:
        yield client


def web_proposal(agent: TestClient, csrf: str) -> dict:
    from tests.test_matcher import read_into_run

    app_db = agent.app.state.db
    run_id = read_into_run(app_db)
    report = match_claim(app_db, run_id=run_id, requester_id="P-ARJUN", vault_dir=agent.app.state.settings.vault_dir)
    decision = next(i for i in report.items if i.requirement_id == REQ).decision_id
    resp = agent.post(f"/api/runs/{run_id}/actions", json={"decision_id": decision},
                      headers={**JSON, "x-csrf-token": csrf})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_approval_page_shows_the_card_and_records_the_person(agent: TestClient) -> None:
    from tests.conftest import csrf_from
    from tests.test_consent_web import sign_in, sign_out

    csrf = sign_in(agent, "P-ARJUN")
    proposed = web_proposal(agent, csrf)
    action_id = proposed["action"]["id"]
    assert proposed["created"] and proposed["can_approve"] and proposed["card_url"] == f"/actions/{action_id}"

    page = agent.get(f"/actions/{action_id}", headers=HTML).text
    for needle in ('id="card-patient" data-person-id="P-RAMESH">Ramesh', 'id="card-claim">CLM-2026-0142',
                   f'data-req-id="{REQ}"', "pharmacy_invoice_ramesh.pdf", 'src="/vault/files/VF-SEED-01"',
                   proposed["card"]["file"]["sha256"], 'id="card-match-reason"', "/claims/CLM-2026-0142/documents",
                   f'value="{proposed["card_sha256"]}"', 'id="approve-button"', 'id="reject-button"'):
        assert needle in page, needle
    assert f'data-action-id="{action_id}"' in agent.get("/actions", headers=HTML).text

    wrong = agent.post(f"/api/actions/{action_id}/decision", json={"decision": "approve", "card_sha256": "0" * 64},
                       headers={**JSON, "x-csrf-token": csrf})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "card_changed"
    no_csrf = agent.post(f"/actions/{action_id}/approve", data={"card_sha256": proposed["card_sha256"]}, headers=HTML)
    assert no_csrf.status_code == 403

    done = agent.post(f"/actions/{action_id}/approve", headers=HTML,
                      data={"card_sha256": proposed["card_sha256"], "csrf_token": csrf_from(page)})
    assert done.status_code == 200 and 'data-state="APPROVED"' in done.text
    assert 'data-decision="approved" data-by="P-ARJUN"' in done.text and 'id="execute-button"' in done.text

    # Executing without a signed-in portal session fails safely: nothing is sent, nothing changes.
    run = agent.post(f"/api/actions/{action_id}/execute", headers={**JSON, "x-csrf-token": csrf})
    assert run.status_code == 409 and run.json()["error"]["code"] == "session_expired"
    assert agent.get(f"/api/actions/{action_id}").json()["action"]["state"] == "APPROVED"
    sign_out(agent, csrf)

    other = sign_in(agent, "P-LAKSHMI")
    assert agent.get(f"/actions/{action_id}", headers=JSON).status_code == 404
    denied = agent.post(f"/api/actions/{action_id}/decision", json={"decision": "reject"},
                        headers={**JSON, "x-csrf-token": other})
    assert denied.status_code == 404


def test_reject_from_the_page(agent: TestClient) -> None:
    from tests.test_consent_web import sign_in

    csrf = sign_in(agent, "P-ARJUN")
    action_id = web_proposal(agent, csrf)["action"]["id"]
    done = agent.post(f"/actions/{action_id}/reject", data={"csrf_token": csrf}, headers=HTML)
    assert 'data-state="REJECTED"' in done.text and 'id="approve-button"' not in done.text
    again = agent.post(f"/actions/{action_id}/execute", data={"csrf_token": csrf}, headers=HTML)
    assert 'data-state="REJECTED"' in again.text
    api = agent.get(f"/api/actions/{action_id}").json()
    assert api["approval"]["decision"] == "rejected" and api["attempts"] == []
