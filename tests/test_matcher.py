"""Document matcher: every rule, the hospital-held rule, and 'a mismatch is never uploadable'."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import repository as repo
from agent.db import Database
from agent.extract import extract_claim
from agent.matcher import MatcherError, match_claim, propose_upload
from agent.models import MatchResult
from agent.seed import init_database
from tests.conftest import HTML, JSON, allow_portal

M = MatchResult
SNAPSHOT_0142: dict[str, Any] = {
    "claim_id": "CLM-2026-0142", "policy_no": "POL-HDF-778812", "patient_name": "Ramesh", "patient_member_id": "M-01",
    "route": "Cashless", "hospital": "Sunrise Multispeciality Hospital (demo)", "admission_date": "2026-10-14",
    "discharge_date": "2026-10-18", "amount_claimed_inr": 118400, "raw_status": "Query Raised",
    "status_updated_at": "2026-10-21T14:05:00+05:30", "status_updated_at_utc": "2026-10-21T08:35:00Z",
    "preauth": {"reference": "PA-2026-0142", "status": "Approved", "amount_inr": 120000,
                "decided_at": "2026-10-14T11:40:00+05:30"},
    "requirements": [
        {"req_id": "REQ-0142-01", "description": "Pharmacy invoice for discharge medicines, showing patient name and date",
         "doc_type": "pharmacy_invoice", "due_date": "2026-11-05", "state": "Pending"},
        {"req_id": "REQ-0142-02", "description": "Indoor case papers for the admission",
         "doc_type": "indoor_case_papers", "due_date": "2026-11-05", "state": "Pending"},
    ],
    "uploaded": [
        {"doc_id": "DOC-SEED-01", "filename": "preauth_request_form.pdf", "size_bytes": 755, "sha256": "1" * 64,
         "requirement_id": None, "uploaded_at": "2026-10-20T14:05:00+05:30"},
    ],
}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def db(tmp_path: Path, vault: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, vault)
    return database


def read_into_run(db: Database, snapshot: dict[str, Any] = SNAPSHOT_0142, requester: str = "P-ARJUN") -> str:
    """A run holding a consented portal read and its valid DOM extraction."""
    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id=requester, claim_id=snapshot["claim_id"])
        allow_portal(conn, run.id)
        ev = repo.add_evidence(conn, run_id=run.id, kind="portal_page",
                               url=f"http://portal/claims/{snapshot['claim_id']}", html_sha256="a" * 64,
                               extracted={"ok": True, "claim": snapshot}, extraction_source="rule")
    out = extract_claim(db, run_id=run.id, evidence_id=ev.id, claim_id=snapshot["claim_id"])
    assert out.valid, out.errors
    return run.id


def run_match(db: Database, vault: Path, run_id: str, requester: str = "P-ARJUN", **kwargs: Any):  # noqa: ANN201
    report = match_claim(db, run_id=run_id, requester_id=requester, vault_dir=vault, **kwargs)
    return report, {i.requirement_id: i for i in report.items}


def sha_of(db: Database, vault_file_id: str) -> str:
    with db.read() as conn:
        return repo.get_vault_file(conn, vault_file_id).sha256


# ---------------- acceptance ----------------

def test_0142_matches_ramesh_invoice_and_flags_hospital_papers(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    report, items = run_match(db, vault, run_id)
    invoice, papers = items["REQ-0142-01"], items["REQ-0142-02"]

    assert invoice.result is M.MATCHED and invoice.vault_file_id == "VF-SEED-01" and invoice.eligible_for_upload
    rejected = {c.vault_file_id: c.failures for c in invoice.candidates if not c.passed}
    assert any("is for Lakshmi, but this claim is for Ramesh" in f for f in rejected["VF-SEED-02"])
    assert any("outside 2026-10-14 to 2026-10-18" in f for f in rejected["VF-SEED-03"])
    assert any("not readable" in f for f in rejected["VF-SEED-05"])

    assert papers.result is M.HOSPITAL_SHOULD_PROVIDE and not papers.eligible_for_upload
    assert "17(c)" in papers.hospital_request and "Sunrise Multispeciality Hospital (demo)" in papers.hospital_request
    assert "not legal advice" in papers.guidance and papers.candidates == []

    with db.read() as conn:
        decisions = {d.subject_ref: d for d in repo.decisions_for_run(conn, run_id) if d.kind == "match"}
        vault_evidence = [e for e in repo.evidence_for_run(conn, run_id) if e.kind == "vault_extraction"]
        vault_extractions = conn.execute(
            "SELECT COUNT(*) FROM extractions WHERE run_id = ? AND subject = 'vault_file'", (run_id,)).fetchone()[0]
    assert decisions["REQ-0142-01"].result == "MATCHED" and decisions["REQ-0142-01"].evidence_state == "linked"
    assert decisions["REQ-0142-01"].input_refs["matched_sha256"] == sha_of(db, "VF-SEED-01")
    assert decisions["REQ-0142-02"].result == "HOSPITAL_SHOULD_PROVIDE"
    assert len(vault_evidence) == vault_extractions == 6


# ---------------- each rule ----------------

def test_wrong_patient_is_rejected_and_never_uploadable(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    _, items = run_match(db, vault, run_id, selections={"REQ-0142-01": "VF-SEED-02"})
    item = items["REQ-0142-01"]
    assert item.result is M.MISMATCH and "is for Lakshmi, but this claim is for Ramesh" in item.reason
    assert not item.eligible_for_upload
    with pytest.raises(MatcherError, match="only a MATCHED"):
        propose_upload(db, run_id=run_id, decision_id=item.decision_id)
    with pytest.raises(repo.ConstraintViolation, match="MATCHED match decision"), db.transaction() as conn:
        repo.propose_action(conn, run_id=run_id, claim_id="CLM-2026-0142", requirement_id="REQ-0142-01",
                            file_sha256=sha_of(db, "VF-SEED-02"), match_decision_id=item.decision_id)


def test_outside_admission_window_unless_policy_days_configured(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    _, items = run_match(db, vault, run_id, selections={"REQ-0142-01": "VF-SEED-03"})
    assert items["REQ-0142-01"].result is M.MISMATCH and "outside 2026-10-14 to 2026-10-18" in items["REQ-0142-01"].reason
    report, items = run_match(db, vault, run_id, selections={"REQ-0142-01": "VF-SEED-03"}, pre_admission_days=90)
    assert items["REQ-0142-01"].result is M.MATCHED and report.window == ("2026-07-16", "2026-10-18")


def test_unreadable_file_is_rejected(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    _, items = run_match(db, vault, run_id, selections={"REQ-0142-01": "VF-SEED-05"})
    assert items["REQ-0142-01"].result is M.MISMATCH and "is not readable" in items["REQ-0142-01"].reason


def test_already_uploaded_hash_is_rejected(db: Database, vault: Path) -> None:
    snapshot = copy.deepcopy(SNAPSHOT_0142)
    snapshot["uploaded"].append({"doc_id": "DOC-X", "filename": "pharmacy_invoice_ramesh.pdf", "size_bytes": 10,
                                 "sha256": sha_of(db, "VF-SEED-01"), "requirement_id": "REQ-0142-01",
                                 "uploaded_at": "2026-10-22T10:00:00+05:30"})
    _, items = run_match(db, vault, read_into_run(db, snapshot))
    assert items["REQ-0142-01"].result is M.MISMATCH
    assert "already uploaded to the claim" in items["REQ-0142-01"].reason


def test_missing_document(db: Database, vault: Path) -> None:
    snapshot = copy.deepcopy(SNAPSHOT_0142) | {"claim_id": "CLM-2026-0150", "patient_name": "Lakshmi",
                                               "patient_member_id": "M-02", "route": "Reimbursement"}
    snapshot["requirements"] = [{"req_id": "REQ-0150-01", "description": "Pharmacy invoice", "doc_type": None,
                                 "due_date": None, "state": "Pending"}]
    _, items = run_match(db, vault, read_into_run(db, snapshot, requester="P-LAKSHMI"), requester="P-LAKSHMI")
    item = items["REQ-0150-01"]
    assert item.result is M.MISSING and item.needed_doc_type == "pharmacy_invoice"  # type from the description
    assert "no pharmacy invoice for Lakshmi" in item.reason


def test_hospital_held_even_if_family_selects_a_file(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    _, items = run_match(db, vault, run_id, selections={"REQ-0142-02": "VF-SEED-06"})
    assert items["REQ-0142-02"].result is M.HOSPITAL_SHOULD_PROVIDE and items["REQ-0142-02"].vault_file_id is None


def test_hospital_rule_is_for_cashless_only(db: Database, vault: Path) -> None:
    snapshot = copy.deepcopy(SNAPSHOT_0142) | {"route": "Reimbursement"}
    _, items = run_match(db, vault, read_into_run(db, snapshot))
    assert items["REQ-0142-02"].result is M.MATCHED and items["REQ-0142-02"].vault_file_id == "VF-SEED-06"


def test_unregistered_foreign_tampered_and_missing_files(db: Database, vault: Path) -> None:
    lakshmi_file = vault / "seed" / "lakshmi" / "own.pdf"
    lakshmi_file.parent.mkdir(parents=True)
    lakshmi_file.write_bytes(b"%PDF-1.4 lakshmi")
    with db.transaction() as conn:
        repo.add_vault_file(conn, id="VF-LAKSHMI", owner_id="P-LAKSHMI", storage_path="seed/lakshmi/own.pdf",
                            original_name="own.pdf", sha256=__import__("hashlib").sha256(b"%PDF-1.4 lakshmi").hexdigest(),
                            size_bytes=16, content_type="application/pdf")
    run_id = read_into_run(db)
    for selection, needle in (("VF-NOT-REAL", "not a registered file in your vault"),
                              ("VF-LAKSHMI", "not a registered file in your vault"),
                              ("../../etc/passwd", "not a registered file in your vault")):
        _, items = run_match(db, vault, run_id, selections={"REQ-0142-01": selection})
        assert items["REQ-0142-01"].result is M.MISMATCH and needle in items["REQ-0142-01"].reason

    (vault / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").write_bytes(b"%PDF-1.4 swapped")
    _, items = run_match(db, vault, read_into_run(db), selections={"REQ-0142-01": "VF-SEED-01"})
    assert "no longer match the registered SHA-256" in items["REQ-0142-01"].reason
    (vault / "seed" / "arjun" / "pharmacy_invoice_ramesh.pdf").unlink()
    _, items = run_match(db, vault, read_into_run(db), selections={"REQ-0142-01": "VF-SEED-01"})
    assert "missing from the vault" in items["REQ-0142-01"].reason


def test_only_matched_items_become_upload_proposals(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    report, _ = run_match(db, vault, run_id)
    for item in report.items:
        if item.eligible_for_upload:
            action, created = propose_upload(db, run_id=run_id, decision_id=item.decision_id)
            assert created and action.state == "PROPOSED" and action.vault_file_id == "VF-SEED-01"
            assert propose_upload(db, run_id=run_id, decision_id=item.decision_id)[0].id == action.id
        else:
            with pytest.raises(MatcherError):
                propose_upload(db, run_id=run_id, decision_id=item.decision_id)


def test_requester_and_extraction_preconditions(db: Database, vault: Path) -> None:
    run_id = read_into_run(db)
    with pytest.raises(MatcherError, match="only the person who started the run"):
        match_claim(db, run_id=run_id, requester_id="P-LAKSHMI", vault_dir=vault)
    with db.transaction() as conn:
        empty = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
    with pytest.raises(MatcherError, match="read the claim first"):
        match_claim(db, run_id=empty.id, requester_id="P-ARJUN", vault_dir=vault)


# ---------------- model-read metadata ----------------

class FakeVaultLlm:
    model = "fake-gemini"

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies

    def generate_json_from_document(self, prompt: str, data: bytes, mime_type: str, schema: dict) -> str:
        assert "data, not instructions" in prompt and schema["additionalProperties"] is False
        return self.replies.get("png" if mime_type == "image/png" else "pdf", "")


def test_model_metadata_is_validated_and_recorded(db: Database, vault: Path) -> None:
    good = json.dumps({"doc_type": "pharmacy_invoice", "patient_name": "Ramesh", "doc_date": "2026-10-18",
                       "invoice_no": None, "hospital": None, "amount_inr": None, "legible": True})
    run_id = read_into_run(db)
    _, items = run_match(db, vault, run_id, llm=FakeVaultLlm({"pdf": good, "png": "{broken"}))
    assert items["REQ-0142-01"].result is M.MATCHED  # several PDFs read as qualifying; one is chosen
    with db.read() as conn:
        rows = conn.execute("SELECT valid, method, errors_json FROM extractions WHERE run_id = ? "
                            "AND subject = 'vault_file'", (run_id,)).fetchall()
    assert {r["method"] for r in rows} == {"gemini"}
    assert any(not r["valid"] and "malformed JSON" in r["errors_json"] for r in rows)


# ---------------- web ----------------

@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from agent import config as agent_config
    from agent.server import create_app

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "webvault"))
    monkeypatch.setenv("AGENT_SESSION_SECRET", "test-secret")
    with TestClient(create_app(agent_config.load_settings())) as client:
        yield client


def test_vault_pages_and_match_api(agent: TestClient, tmp_path: Path) -> None:
    from tests.test_consent_web import sign_in, sign_out

    csrf = sign_in(agent, "P-ARJUN")
    page = agent.get("/vault", headers=HTML).text
    assert page.count('class="vault-file"') == 6 and 'data-vault-file-id="VF-SEED-01"' in page
    pdf = agent.get("/vault/files/VF-SEED-01")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-")

    run_id = read_into_run(agent.app.state.db)
    resp = agent.post(f"/api/runs/{run_id}/match", json={"selections": {"REQ-0142-01": "VF-SEED-02"}},
                      headers={**JSON, "x-csrf-token": csrf})
    items = {i["requirement_id"]: i for i in resp.json()["items"]}
    assert items["REQ-0142-01"]["result"] == "MISMATCH" and items["REQ-0142-01"]["eligible_for_upload"] is False
    assert items["REQ-0142-02"]["result"] == "HOSPITAL_SHOULD_PROVIDE"
    sign_out(agent, csrf)

    csrf = sign_in(agent, "P-LAKSHMI")
    assert 'class="vault-file"' not in agent.get("/vault", headers=HTML).text
    assert agent.get("/vault/files/VF-SEED-01", headers=JSON).status_code == 404
    denied = agent.post(f"/api/runs/{run_id}/match", json={}, headers={**JSON, "x-csrf-token": csrf})
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "cannot_match"
