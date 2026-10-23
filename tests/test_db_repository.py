"""Repository helpers: validation, audit convention, idempotent actions, append-only records."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest

from agent import repository as repo
from agent.db import Database, to_utc_iso, utc_now
from agent.repository import ConstraintViolation, InvalidTransition, NotFound, ValidationError
from agent.seed import init_database
from tests.conftest import allow_portal
from tests.conftest import matched

SHA = hashlib.sha256(b"synthetic invoice").hexdigest()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, tmp_path / "vault")
    return database


@pytest.fixture
def run_id(db: Database) -> str:
    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142", test_case="TC1")
        allow_portal(conn, run.id)
        return run.id


# ---------- time ----------

def test_timestamps_are_utc_with_z() -> None:
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    assert to_utc_iso(datetime(2026, 10, 22, 14, 5, tzinfo=ist)) == "2026-10-22T08:35:00Z"
    assert utc_now().endswith("Z")
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 10, 22, 14, 5))


# ---------- JSON ----------

@pytest.mark.parametrize("bad", ["{not json", float("nan"), {"x": float("inf")}, {1: "int key"}, {"s": {1, 2}}, 42])
def test_invalid_json_rejected(bad: object) -> None:
    with pytest.raises(ValidationError):
        repo.canonical_json(bad)


def test_json_is_canonical() -> None:
    assert repo.canonical_json('{"b": 1, "a": [2, 1]}') == '{"a":[2,1],"b":1}'


# ---------- people and consents ----------

def test_person_requires_synthetic_contact(db: Database) -> None:
    with pytest.raises(ValidationError), db.transaction() as conn:
        repo.create_person(conn, id="P-NEW", name="New", role="family", contact_ref="9876543210")


def test_consent_lookup_and_coverage(db: Database) -> None:
    with db.read() as conn:
        consents = {c.id: c for c in repo.consents_between(conn, "P-ARJUN", "P-RAMESH")}
        assert repo.consents_between(conn, "P-ARJUN", "P-LAKSHMI") == []  # TC2 precondition
    claim_0142, claim_0163 = consents["CON-SEED-01"], consents["CON-SEED-02"]
    assert claim_0142.covers("CLM-2026-0142", "upload_docs", "2026-10-22T08:00:00Z")
    assert not claim_0142.covers("CLM-2026-0163", "read_status", "2026-10-22T08:00:00Z")  # exact claim only
    assert not claim_0142.covers("CLM-2026-0142", "upload_docs", "2027-05-01T00:00:00Z")  # expired
    assert claim_0163.covers("CLM-2026-0163", "read_status", "2026-10-22T08:00:00Z")
    assert not claim_0163.covers("CLM-2026-0163", "upload_docs", "2026-10-22T08:00:00Z")


def test_consent_rejects_unknown_action_and_bad_claim(db: Database) -> None:
    with pytest.raises(ValidationError, match="consent action"), db.transaction() as conn:
        repo.create_consent(conn, patient_id="P-LAKSHMI", grantee_id="P-ARJUN", policy_no="POL-HDF-778812",
                            claim_id="CLM-2026-0150", actions=["delete_claim"],
                            granted_at="2026-10-22T08:00:00Z", expires_at="2026-11-22T08:00:00Z")
    with pytest.raises(ValidationError, match="claim id"), db.transaction() as conn:
        repo.create_consent(conn, patient_id="P-LAKSHMI", grantee_id="P-ARJUN", policy_no="POL-HDF-778812",
                            claim_id="0150", actions=["read_status"],
                            granted_at="2026-10-22T08:00:00Z", expires_at="2026-11-22T08:00:00Z")


def test_revoke_consent_once(db: Database) -> None:
    with db.transaction() as conn:
        revoked = repo.revoke_consent(conn, "CON-SEED-01")
        assert revoked.revoked and revoked.revoked_at
        assert not revoked.covers("CLM-2026-0142", "read_status", utc_now())
        with pytest.raises(InvalidTransition):
            repo.revoke_consent(conn, "CON-SEED-01")


# ---------- runs ----------

def test_run_lifecycle(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        assert repo.update_run(conn, run_id, state="running").finished_at is None
        done = repo.update_run(conn, run_id, state="completed", final_status="QUERY_RAISED")
        assert done.finished_at and done.final_status == "QUERY_RAISED"
        with pytest.raises(InvalidTransition):
            repo.update_run(conn, run_id, state="running")
        with pytest.raises(NotFound):
            repo.get_run(conn, "RUN-missing")


# ---------- evidence ----------

def test_evidence_validates_hash_path_and_source(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        with pytest.raises(ValidationError, match="html_sha256"):
            repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="u", html_sha256="ABC")
        with pytest.raises(ValidationError, match="unsafe"):
            repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="u", screenshot_path="C:/x.png")
        with pytest.raises(ValidationError, match="together"):
            repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="u", extracted={"raw_status": "x"})
        ev = repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="http://portal/claims/CLM-2026-0142",
                               html_sha256="e" * 64, screenshot_path="runs/r1/claim.png",
                               extracted={"raw_status": "Query Raised"}, extraction_source="model")
    assert ev.extracted == {"raw_status": "Query Raised"}


def test_evidence_and_decisions_are_append_only(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        ev = repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="u", html_sha256="e" * 64)
        dec = repo.record_decision(conn, run_id=run_id, kind="status", result="QUERY_RAISED",
                                   reason="raw text 'Query Raised' matched table row", evidence_id=ev.id)
    for sql in (f"UPDATE evidence SET url = 'x' WHERE id = '{ev.id}'",
                f"UPDATE decisions SET result = 'APPROVED' WHERE id = '{dec.id}'"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), db.transaction() as conn:
            conn.execute(sql)


def test_extracted_content_is_never_logged(db: Database, run_id: str, caplog: pytest.LogCaptureFixture) -> None:
    secret_text = "Diagnosis text that must not reach logs"
    with caplog.at_level(logging.DEBUG), db.transaction() as conn:
        repo.add_evidence(conn, run_id=run_id, kind="vault_extraction", url="vault://VF-SEED-01",
                          extracted={"notes": secret_text}, extraction_source="model")
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "evidence recorded" in logged and secret_text not in logged


# ---------- decisions: audit convention ----------

def test_decision_links_evidence_from_same_run(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        ev = repo.add_evidence(conn, run_id=run_id, kind="portal_page", url="u", html_sha256="e" * 64)
        dec = repo.record_decision(conn, run_id=run_id, kind="status", result="UNDER_REVIEW",
                                   reason="raw text matched", evidence_id=ev.id,
                                   input_refs={"raw_status": "Under Review"})
    assert dec.evidence_state == "linked" and dec.decided_by == "rule"

    with db.transaction() as conn:
        other = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0163").id
        allow_portal(conn, other)
    with pytest.raises(ConstraintViolation, match="same run"), db.transaction() as conn:
        repo.record_decision(conn, run_id=other, kind="status", result="UNDER_REVIEW", reason="borrowed",
                             evidence_id=ev.id)


def test_missing_evidence_must_be_declared_and_status_becomes_uncertain(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        with pytest.raises(ValidationError, match="say why there is no evidence"):
            repo.record_decision(conn, run_id=run_id, kind="match", result="MISSING", reason="no file")
        with pytest.raises(ValidationError, match="only be UNCERTAIN"):
            repo.record_decision(conn, run_id=run_id, kind="status", result="APPROVED",
                                 reason="portal page did not load", evidence_state="missing")
        dec = repo.record_decision(conn, run_id=run_id, kind="status", result="UNCERTAIN",
                                   reason="portal page did not load", evidence_state="missing")
        consent = repo.record_decision(conn, run_id=run_id, kind="consent", result="ALLOW",
                                       reason="CON-SEED-01 covers read_status", evidence_state="not_applicable",
                                       input_refs={"consent_id": "CON-SEED-01"})
    assert dec.evidence_id is None and dec.evidence_state == "missing"
    assert consent.input_refs == {"consent_id": "CON-SEED-01"}


def test_model_cannot_be_the_decider_and_results_are_whitelisted(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        with pytest.raises(ValidationError, match="decided_by"):
            repo.record_decision(conn, run_id=run_id, kind="consent", result="ALLOW", reason="model said so",
                                 evidence_state="not_applicable", decided_by="model")
        with pytest.raises(ValidationError, match="not allowed for status"):
            repo.record_decision(conn, run_id=run_id, kind="status", result="Looks approved",
                                 reason="free text", evidence_state="missing")


# ---------- vault ----------

def test_vault_rejects_unsafe_paths_and_bad_fields(db: Database) -> None:
    with db.transaction() as conn:
        for path in ("../secrets.pdf", "/etc/passwd", "C:\\Windows\\x.pdf", "a//b.pdf"):
            with pytest.raises(ValidationError, match="unsafe"):
                repo.add_vault_file(conn, owner_id="P-ARJUN", storage_path=path, original_name="x.pdf",
                                    sha256=SHA, size_bytes=10, content_type="application/pdf")
        with pytest.raises(ValidationError, match="amount_inr"):
            repo.add_vault_file(conn, owner_id="P-ARJUN", storage_path="uploads/x.pdf", original_name="x.pdf",
                                sha256=SHA, size_bytes=10, content_type="application/pdf",
                                amount_inr=-5, extraction_source="human")


def test_vault_duplicate_hash_per_owner_rejected(db: Database) -> None:
    with db.read() as conn:
        seeded = repo.get_vault_file(conn, "VF-SEED-01")
    with pytest.raises(ConstraintViolation), db.transaction() as conn:
        repo.add_vault_file(conn, owner_id="P-ARJUN", storage_path="uploads/copy.pdf", original_name="copy.pdf",
                            sha256=seeded.sha256, size_bytes=seeded.size_bytes, content_type="application/pdf")


# ---------- actions: idempotency and lifecycle ----------

def test_idempotency_key_is_deterministic() -> None:
    a = repo.idempotency_key("CLM-2026-0142", "REQ-0142-01", SHA)
    assert a == repo.idempotency_key("CLM-2026-0142", "REQ-0142-01", SHA)
    assert a != repo.idempotency_key("CLM-2026-0142", "REQ-0142-02", SHA)


def test_proposing_twice_returns_the_same_action(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        first, created = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                             requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", SHA))
        again, created_again = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                                   requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", SHA))
    assert created and not created_again and first.id == again.id


def test_duplicate_idempotency_key_rejected_by_database(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        action, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                        requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", SHA))
    with pytest.raises(ConstraintViolation, match="idempotency key already exists"), db.transaction() as conn:
        repo._execute(
            conn,
            "INSERT INTO actions (id, idem_key, run_id, claim_id, requirement_id, file_sha256, state, created_at, "
            "updated_at, match_decision_id) VALUES ('ACT-dup', ?, ?, 'CLM-2026-0142', 'REQ-0142-99', ?, 'PROPOSED', ?, ?, ?)",
            (action.idem_key, run_id, SHA, utc_now(), utc_now(), matched(conn, run_id, "REQ-0142-99", SHA)),
            "duplicate insert",
        )


def test_action_lifecycle_tc4_path(db: Database, run_id: str) -> None:
    with db.transaction() as conn:
        action, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                        requirement_id="REQ-0142-01", file_sha256=SHA, vault_file_id="VF-SEED-01",
            match_decision_id=matched(conn, run_id, "REQ-0142-01", SHA))
        with pytest.raises(InvalidTransition):
            repo.transition_action(conn, action.id, "EXECUTING")  # not approved yet
        approved = repo.approve_action(conn, action.id, approved_by="P-ARJUN")
        assert approved.approved_by == "P-ARJUN" and approved.approved_at
        running = repo.transition_action(conn, action.id, "EXECUTING")
        assert running.attempts == 1
        unknown = repo.transition_action(conn, action.id, "UNKNOWN", error="client timeout after 10 s")
        assert unknown.last_error == "client timeout after 10 s"
        verified = repo.transition_action(conn, action.id, "VERIFIED")
        assert verified.attempts == 1  # verified by re-reading, no second upload
        with pytest.raises(InvalidTransition):
            repo.transition_action(conn, action.id, "EXECUTING")


def test_retry_counts_attempts_and_rejection_is_terminal(db: Database, run_id: str) -> None:
    other = hashlib.sha256(b"other file").hexdigest()
    with db.transaction() as conn:
        a, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                   requirement_id="REQ-0142-01", file_sha256=SHA,
            match_decision_id=matched(conn, run_id, "REQ-0142-01", SHA))
        repo.approve_action(conn, a.id, approved_by="P-ARJUN")
        repo.transition_action(conn, a.id, "EXECUTING")
        repo.transition_action(conn, a.id, "UNKNOWN")
        repo.transition_action(conn, a.id, "RETRY_ALLOWED")
        assert repo.transition_action(conn, a.id, "EXECUTING").attempts == 2

        b, _ = repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142",
                                   requirement_id="REQ-0142-02", file_sha256=other,
            match_decision_id=matched(conn, run_id, "REQ-0142-02", other))
        rejected = repo.reject_action(conn, b.id, rejected_by="P-ARJUN")
        assert rejected.state == "REJECTED" and rejected.approved_by is None
        with pytest.raises(InvalidTransition):
            repo.approve_action(conn, b.id, approved_by="P-ARJUN")
        with pytest.raises(NotFound):
            repo.approve_action(conn, b.id, approved_by="P-NOBODY")
