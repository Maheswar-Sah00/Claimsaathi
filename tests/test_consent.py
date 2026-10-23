"""Consent gate: every scope rule, recording, guarded actions and the TC2 lifecycle."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent import consent as cs
from agent import repository as repo
from agent.db import Database
from agent.models import ConsentResult
from agent.seed import POLICY_NO, init_database
from tests.conftest import matched

NOW = "2026-10-22T08:00:00Z"
SHA = hashlib.sha256(b"pharmacy invoice").hexdigest()
PORTAL_KINDS = ("portal_page", "document_list", "upload_response")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, tmp_path / "vault")
    return database


def grant(db: Database, **overrides: object) -> repo.Consent:
    """Create a consent from Lakshmi to Arjun with overridable fields."""
    fields = {"patient_id": "P-LAKSHMI", "grantee_id": "P-ARJUN", "policy_no": POLICY_NO,
              "claim_id": "CLM-2026-0150", "actions": ["read_status"],
              "granted_at": "2026-10-20T00:00:00Z", "expires_at": "2026-11-20T00:00:00Z"}
    fields.update(overrides)
    with db.transaction() as conn:
        return repo.create_consent(conn, **fields)


def portal_evidence_count(db: Database, claim_id: str) -> int:
    with db.read() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM evidence e JOIN runs r ON r.id = e.run_id WHERE r.claim_id = ? "
            f"AND e.kind IN {PORTAL_KINDS}", (claim_id,)
        ).fetchone()[0]


# ---------------- the five checks ----------------

def test_valid_consent_allows_read_and_upload(db: Database) -> None:
    for action in ("read_status", "upload_docs"):
        decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", action, at=NOW)
        assert decision.result is ConsentResult.ALLOW and decision.consent_id == "CON-SEED-01"
        assert decision.patient_id == "P-RAMESH"


def test_wrong_patient_mother_claim_needs_consent(db: Database) -> None:
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status", at=NOW)
    assert decision.result is ConsentResult.NEEDS_CONSENT
    assert decision.patient_id == "P-LAKSHMI"  # resolved from the registry, not the caller


def test_wrong_requester(db: Database) -> None:
    decision = cs.check_consent(db, "P-LAKSHMI", "CLM-2026-0142", "read_status", at=NOW)
    assert decision.result is ConsentResult.NEEDS_CONSENT  # Ramesh's consent is for Arjun only
    with pytest.raises(cs.UnknownRequester):
        cs.check_consent(db, "P-STRANGER", "CLM-2026-0142", "read_status", at=NOW)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE requester_id = 'P-STRANGER'").fetchone()[0] == 0


def test_wrong_policy_is_out_of_scope(db: Database) -> None:
    grant(db, policy_no="POL-OTHER-000001")
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status", at=NOW)
    assert decision.result is ConsentResult.OUT_OF_SCOPE and "policy" in decision.reason


def test_wrong_claim_is_out_of_scope(db: Database) -> None:
    grant(db, claim_id="CLM-2026-0999")
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status", at=NOW)
    assert decision.result is ConsentResult.OUT_OF_SCOPE and "CLM-2026-0999" in decision.reason


def test_unregistered_claim_is_out_of_scope(db: Database) -> None:
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0777", "read_status", at=NOW)
    assert decision.result is ConsentResult.OUT_OF_SCOPE and decision.patient_id is None


def test_wildcard_only_when_explicitly_granted(db: Database) -> None:
    # CON-SEED-01 names CLM-2026-0142 exactly; it must not spill onto Ramesh's other claim.
    upload_0163 = cs.check_consent(db, "P-ARJUN", "CLM-2026-0163", "upload_docs", at=NOW)
    assert upload_0163.result is ConsentResult.ACTION_NOT_ALLOWED and upload_0163.consent_id == "CON-SEED-02"
    grant(db, claim_id="*")
    assert cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status", at=NOW).allowed


def test_action_not_allowed(db: Database) -> None:
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0163", "upload_docs", at=NOW)
    assert decision.result is ConsentResult.ACTION_NOT_ALLOWED
    with pytest.raises(repo.ValidationError):
        cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", "delete_claim", at=NOW)


def test_expired_and_not_yet_valid(db: Database) -> None:
    late = cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", "read_status", at="2027-05-01T00:00:00Z")
    assert late.result is ConsentResult.EXPIRED_OR_REVOKED and "expired" in late.reason
    early = cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", "read_status", at="2026-10-01T00:00:00Z")
    assert early.result is ConsentResult.EXPIRED_OR_REVOKED and "not valid until" in early.reason


def test_revoked(db: Database) -> None:
    cs.revoke(db, "CON-SEED-01", by_person_id="P-RAMESH")
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", "read_status")
    assert decision.result is ConsentResult.EXPIRED_OR_REVOKED and "revoked" in decision.reason


def test_patient_can_read_own_claim(db: Database) -> None:
    decision = cs.check_consent(db, "P-LAKSHMI", "CLM-2026-0150", "read_status", at=NOW)
    assert decision.allowed and "is the patient" in decision.reason


# ---------------- recording ----------------

def test_every_check_is_recorded_with_evidence_snapshot(db: Database) -> None:
    decision = cs.check_consent(db, "P-ARJUN", "CLM-2026-0163", "upload_docs", at=NOW)
    with db.read() as conn:
        stored = repo.get_decision(conn, decision.decision_id)
        evidence = repo.get_evidence(conn, decision.evidence_id)
        run = repo.get_run(conn, decision.run_id)
    assert (stored.kind, stored.result, stored.decided_by) == ("consent", "ACTION_NOT_ALLOWED", "rule")
    assert stored.evidence_id == evidence.id and evidence.kind == "consent_record"
    assert evidence.extracted["claim"]["patient_id"] == "P-RAMESH"
    assert {c["id"]: c["outcome"] for c in evidence.extracted["consents"]} == {
        "CON-SEED-01": "OUT_OF_SCOPE", "CON-SEED-02": "ACTION_NOT_ALLOWED"}
    assert run.state == "blocked"


# ---------------- guarded actions ----------------

def fake_reader(db: Database, calls: list[str]):
    """Stands in for the portal adapter: records the call and stores portal evidence."""
    def read(run_id: str, claim_id: str) -> str:
        calls.append(claim_id)
        with db.transaction() as conn:
            repo.add_evidence(conn, run_id=run_id, kind="portal_page", url=f"http://portal/claims/{claim_id}",
                              html_sha256="a" * 64)
        return "page"
    return read


def test_missing_consent_blocks_portal_reads(db: Database) -> None:
    calls: list[str] = []
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0150")
    with pytest.raises(cs.ConsentDenied) as denied:
        cs.guarded_portal_read(db, "P-ARJUN", "CLM-2026-0150", run_id=run.id, reader=fake_reader(db, calls))
    assert denied.value.decision.result is ConsentResult.NEEDS_CONSENT
    assert calls == [] and portal_evidence_count(db, "CLM-2026-0150") == 0


def test_database_refuses_portal_evidence_without_allow(db: Database) -> None:
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0150")
    with pytest.raises(repo.ConstraintViolation, match="ALLOW consent decision"), db.transaction() as conn:
        repo.add_evidence(conn, run_id=run.id, kind="portal_page", url="http://portal/x", html_sha256="a" * 64)


def test_allowed_read_calls_reader_once(db: Database) -> None:
    calls: list[str] = []
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0142")
    assert cs.guarded_portal_read(db, "P-ARJUN", "CLM-2026-0142", run_id=run.id,
                                  reader=fake_reader(db, calls)) == "page"
    assert calls == ["CLM-2026-0142"] and portal_evidence_count(db, "CLM-2026-0142") == 1


def test_mid_session_revocation_stops_approval_and_execution(db: Database) -> None:
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0142")
    assert cs.check_consent(db, "P-ARJUN", "CLM-2026-0142", "upload_docs", run_id=run.id).allowed
    with db.transaction() as conn:
        first, _ = repo.propose_action(conn, run_id=run.id, claim_id="CLM-2026-0142",
                                       requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run.id, "REQ-0142-01", SHA))
        second, _ = repo.propose_action(conn, run_id=run.id, claim_id="CLM-2026-0142",
                                        requirement_id="REQ-0142-02", file_sha256=SHA,
            match_decision_id=matched(conn, run.id, "REQ-0142-02", SHA))
    approved = cs.guarded_approve_upload(db, first.id, approved_by="P-ARJUN")
    assert approved.state == "APPROVED"

    cs.revoke(db, "CON-SEED-01", by_person_id="P-RAMESH")

    with pytest.raises(cs.ConsentDenied):
        cs.guarded_start_upload(db, first.id, requester_id="P-ARJUN")
    with pytest.raises(cs.ConsentDenied):
        cs.guarded_approve_upload(db, second.id, approved_by="P-ARJUN")
    with db.read() as conn:
        assert repo.get_action(conn, first.id).state == "APPROVED"  # never reached EXECUTING
        assert repo.get_action(conn, second.id).state == "PROPOSED"
        results = [d.result for d in repo.decisions_for_run(conn, run.id)]
    assert results[-2:] == ["EXPIRED_OR_REVOKED", "EXPIRED_OR_REVOKED"]  # the denials were recorded


def test_approver_must_be_the_runs_requester(db: Database) -> None:
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0142")
    with db.transaction() as conn:
        action, _ = repo.propose_action(conn, run_id=run.id, claim_id="CLM-2026-0142",
                                        requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run.id, "REQ-0142-01", SHA))
    with pytest.raises(cs.ConsentError, match="different requester"):
        cs.guarded_approve_upload(db, action.id, approved_by="P-LAKSHMI")


# ---------------- TC2: request, approval, then access ----------------

def test_tc2_mother_claim_full_lifecycle(db: Database) -> None:
    calls: list[str] = []
    run = cs.start_run(db, "P-ARJUN", "CLM-2026-0150", test_case="TC2")

    # 1. Blocked, and nothing about the protected claim is read.
    with pytest.raises(cs.ConsentDenied) as denied:
        cs.guarded_portal_read(db, "P-ARJUN", "CLM-2026-0150", run_id=run.id, reader=fake_reader(db, calls))
    assert denied.value.decision.result is ConsentResult.NEEDS_CONSENT
    assert calls == [] and portal_evidence_count(db, "CLM-2026-0150") == 0

    # 2. A consent request is generated for the patient from the registry.
    request = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"], run_id=run.id)
    assert (request.patient_id, request.status, request.actions) == ("P-LAKSHMI", "pending", ("read_status",))
    assert cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"]).id == request.id  # reused

    # 3. Neither the requester nor another family member can approve it.
    for impostor in ("P-ARJUN", "P-RAMESH"):
        with pytest.raises(cs.ConsentPermissionError):
            cs.approve_request(db, request.id, patient_session_id=impostor)
    assert portal_evidence_count(db, "CLM-2026-0150") == 0

    # 4. The patient approves exactly this claim.
    consent = cs.approve_request(db, request.id, patient_session_id="P-LAKSHMI", duration_days=7)
    assert consent.claim_id == "CLM-2026-0150" and consent.actions == ("read_status",)
    with pytest.raises(cs.RequestNotPending):
        cs.approve_request(db, request.id, patient_session_id="P-LAKSHMI")

    # 5. Only now does the read happen.
    cs.guarded_portal_read(db, "P-ARJUN", "CLM-2026-0150", run_id=run.id, reader=fake_reader(db, calls))
    assert calls == ["CLM-2026-0150"] and portal_evidence_count(db, "CLM-2026-0150") == 1
    assert cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "upload_docs").result is ConsentResult.ACTION_NOT_ALLOWED


def test_request_widens_actions_and_patient_can_grant_less(db: Database) -> None:
    first = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"], at=NOW)
    wider = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["upload_docs"], at=NOW)
    assert wider.id == first.id and wider.actions == ("read_status", "upload_docs")
    with pytest.raises(repo.ValidationError):
        cs.approve_request(db, first.id, patient_session_id="P-LAKSHMI", actions=[], at=NOW)
    with pytest.raises(repo.ValidationError):
        cs.approve_request(db, first.id, patient_session_id="P-LAKSHMI", duration_days=365, at=NOW)
    consent = cs.approve_request(db, first.id, patient_session_id="P-LAKSHMI", actions=["read_status"],
                                 all_claims=True, at=NOW)
    assert consent.actions == ("read_status",) and consent.claim_id == "*"
    assert consent.expires_at == "2026-10-29T08:00:00Z"


def test_request_expires_after_24_hours(db: Database) -> None:
    request = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"], at=NOW)
    with pytest.raises(cs.RequestNotPending, match="expired"):
        cs.approve_request(db, request.id, patient_session_id="P-LAKSHMI", at="2026-10-23T08:00:00Z")
    fresh = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"], at="2026-10-23T09:00:00Z")
    assert fresh.id != request.id and fresh.status == "pending"


def test_deny_and_view_permissions(db: Database) -> None:
    request = cs.request_consent(db, "P-ARJUN", "CLM-2026-0150", ["read_status"])
    with pytest.raises(cs.ConsentPermissionError):
        cs.view_request(db, request.id, viewer_id="P-RAMESH")
    assert cs.view_request(db, request.id, viewer_id="P-ARJUN").status == "pending"
    denied = cs.deny_request(db, request.id, patient_session_id="P-LAKSHMI")
    assert denied.status == "denied" and denied.decided_by == "P-LAKSHMI"
    assert cs.check_consent(db, "P-ARJUN", "CLM-2026-0150", "read_status").result is ConsentResult.NEEDS_CONSENT


def test_request_rejects_self_and_unknown_claim(db: Database) -> None:
    with pytest.raises(cs.ConsentError, match="own claims"):
        cs.request_consent(db, "P-LAKSHMI", "CLM-2026-0150", ["read_status"])
    with pytest.raises(cs.UnknownClaim):
        cs.request_consent(db, "P-ARJUN", "CLM-2026-0777", ["read_status"])


def test_revoke_permissions(db: Database) -> None:
    with pytest.raises(cs.ConsentPermissionError):
        cs.revoke(db, "CON-SEED-01", by_person_id="P-LAKSHMI")
    assert cs.revoke(db, "CON-SEED-01", by_person_id="P-ARJUN").revoked  # grantee gives up access
