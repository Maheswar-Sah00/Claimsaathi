"""Official test cases (TC1-TC4) and edge cases (E1-E6), end to end through the real workflow.

    pytest -m scenario

What it does
- Resets the demo data in the configured agent database (DATABASE_PATH) and clears evidence/RUN-*.
- Starts the synthetic portal in-process on a free local port and drives it with headless Chromium
  (the same PortalClient, consent gate, matcher, executor and graph the app uses).
- Between cases only the portal, the seed consents, the seed vault and upload actions are restored
  (`reset_scenario_state`), so every case's run, steps and evidence stay for the test-runs view.
- Plays the family member: types the OTP (read from the portal's simulated SMS state, as a person
  would read their phone) and approves or rejects the card it is shown.
- Records input, expected, actual, every check, PASS/FAIL and evidence in `scenario_results` and in
  docs/test-evidence.md. A case that did not run is listed as NOT RUN, never as passed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from agent import actions as act
from agent import consent as cs
from agent import repository as repo
from agent.config import PROJECT_ROOT, Settings, load_settings
from agent.db import Database, utc_now
from agent.graph import ClaimWorkflow, WorkflowDeps, WorkflowView
from agent.portal_client import PlaywrightTpaDriver, PortalClient
from agent.seed import clear_evidence, reset_demo_data, reset_scenario_state
from portal.app import create_app as create_portal
from portal.config import PortalSettings
from tests.conftest import serve

pytestmark = [pytest.mark.live, pytest.mark.scenario]

POLICY = "POL-HDF-778812"
C142, C150, C163 = "CLM-2026-0142", "CLM-2026-0150", "CLM-2026-0163"
INVOICE = "pharmacy_invoice_ramesh.pdf"
REPORT = PROJECT_ROOT / "docs" / "test-evidence.md"
AGENT_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    input: dict[str, Any]
    expected: str


CASES: dict[str, Case] = {c.id: c for c in (
    Case("TC1", "Arjun checks his father's claim with consent; the correct invoice is uploaded after approval",
         {"requester": "P-ARJUN", "claim_id": C142, "policy_no": POLICY, "intent": "upload", "human": "types OTP, approves"},
         "Stops for the OTP; finds pharmacy_invoice_ramesh.pdf for REQ-0142-01; asks for approval and uploads nothing "
         "before it; uploads it once to REQ-0142-01; re-reads the portal and marks it VERIFIED."),
    Case("TC2", "Arjun asks about his mother's claim without her consent",
         {"requester": "P-ARJUN", "claim_id": C150, "policy_no": POLICY, "intent": "status"},
         "Blocked by the consent gate (NEEDS_CONSENT) before any portal contact; a consent request goes to Lakshmi."),
    Case("TC3", "Pre-authorisation APPROVED is not the claim decision",
         {"requester": "P-ARJUN", "claim_id": C163, "policy_no": POLICY, "intent": "status", "human": "types OTP"},
         "Canonical status UNDER_REVIEW from 'Under Process - Pending Final Authorisation'; pre-authorisation "
         "APPROVED Rs 50,000 reported separately; the reply never says the claim is approved."),
    Case("TC4", "TC1 with the portal storing the file but timing out (timeout_after_store)",
         {"requester": "P-ARJUN", "claim_id": C142, "policy_no": POLICY, "intent": "upload",
          "portal_fault": "timeout_after_store (once)", "human": "types OTP, approves"},
         "The attempt times out and is recorded UNKNOWN; the re-read finds the file; marked VERIFIED with exactly one "
         "upload and no second attempt."),
    Case("E1", "Wrong-patient invoice",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "upload",
          "vault": "Ramesh's correct invoice removed; Lakshmi's invoice still present"},
         "REQ-0142-01 is MISMATCH because the invoice is for Lakshmi; nothing is proposed or uploaded."),
    Case("E2", "Consent revoked by the patient",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "status", "before": "Ramesh revokes CON-SEED-01"},
         "Blocked (EXPIRED_OR_REVOKED) before any portal contact; a new consent request goes to Ramesh."),
    Case("E3", "Unknown raw status text",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "status", "portal_status": "Escalated to medical panel"},
         "Canonical status UNCERTAIN; the reply says the status could not be confirmed and assumes nothing."),
    Case("E4", "Manual OTP: a wrong code, then the right one",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "status", "human": "types a wrong OTP, then the right one"},
         "The wrong code is refused and the run asks again; the right code continues; nothing is read before it."),
    Case("E5", "Portal outage",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "status", "portal": "unreachable"},
         "The run fails with FETCH_FAILED; no claim status is reported and the reply does not say 'no change'."),
    Case("E6", "Hospital-held documents on a cashless claim",
         {"requester": "P-ARJUN", "claim_id": C142, "intent": "upload", "human": "types OTP, rejects the invoice upload"},
         "REQ-0142-02 (indoor case papers) is HOSPITAL_SHOULD_PROVIDE with a drafted note citing the IRDAI circular; "
         "it is never proposed for upload."),
)}


# ---------------- the suite ----------------


@dataclass
class Recorder:
    case: Case
    run_id: str | None = None
    actual: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
        return bool(ok)


class Suite:
    def __init__(self, settings: Settings, db: Database, portal_app: FastAPI, portal_url: str) -> None:
        self.settings, self.db, self.portal_app, self.portal_url = settings, db, portal_app, portal_url
        self.results: dict[str, dict[str, Any]] = {}
        self._open: list[Any] = []

    @property
    def store(self):  # noqa: ANN201 - portal.store.PortalStore
        return self.portal_app.state.store

    def prepare(self) -> None:
        reset_scenario_state(self.db, self.settings.vault_dir)
        self.store.reset()
        self.store.otp_challenges.clear()

    def flow(self, url: str | None = None) -> ClaimWorkflow:
        client = PortalClient(self.db, PlaywrightTpaDriver(url or self.portal_url, timeout_ms=8000),
                              evidence_dir=self.settings.evidence_dir, vault_dir=self.settings.vault_dir,
                              username="arjun", password="demo123", upload_timeout_ms=1000)
        policy = act.UploadPolicy(vault_dir=self.settings.vault_dir, portal_base_url=url or self.portal_url,
                                  upload_timeout_ms=1000)
        wf = ClaimWorkflow(WorkflowDeps(db=self.db, portal_for=lambda _p: client, policy=policy))
        self._open += [wf, client]
        return wf

    def otp_code(self) -> str:
        """What the person reads on the simulated SMS panel for the latest login."""
        return list(self.store.otp_challenges.values())[-1].code

    def type_otp(self, flow: ClaimWorkflow, view: WorkflowView, code: str | None = None) -> WorkflowView:
        return flow.resume(view.run_id, person_id="P-ARJUN", answer={"otp": code or self.otp_code()})

    def decide(self, flow: ClaimWorkflow, view: WorkflowView, decision: str) -> WorkflowView:
        w = view.waiting or {}
        return flow.resume(view.run_id, person_id="P-ARJUN", answer={
            "decision": decision, "card_sha256": w.get("card_sha256"), "action_id": w.get("action_id")})

    def actions(self, run_id: str) -> list[repo.Action]:
        with self.db.read() as conn:
            return [repo.Action(**dict(r)) for r in conn.execute("SELECT * FROM actions WHERE run_id = ?", (run_id,))]

    def stored(self, claim_id: str, sha: str) -> list:
        return [u for u in self.store.get_claim(claim_id).uploads if u.sha256 == sha]

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = repo.evidence_for_run(conn, run_id)
        root = self.settings.evidence_dir
        return [{"id": e.id, "kind": e.kind, "fetched_at": e.fetched_at, "html_sha256": e.html_sha256,
                 "screenshot": str(root / e.screenshot_path) if e.screenshot_path else None,
                 "url": f"{AGENT_URL}/evidence/{e.id}"} for e in rows]

    @contextmanager
    def case(self, case_id: str) -> Iterator[Recorder]:
        rec = Recorder(CASES[case_id])
        self.prepare()
        error: BaseException | None = None
        try:
            yield rec
        except Exception as exc:  # noqa: BLE001 - recorded as a failed check, then re-raised below
            error = exc
            rec.check("ran without an error", False, f"{type(exc).__name__}: {exc}")
        finally:
            for item in reversed(self._open):
                item.close()
            self._open.clear()
        passed = bool(rec.checks) and all(c["ok"] for c in rec.checks)
        evidence = self.evidence(rec.run_id) if rec.run_id else []
        with self.db.transaction() as conn:
            repo.record_scenario_result(conn, test_case=case_id, title=rec.case.title, run_id=rec.run_id,
                                        input=rec.case.input, expected=rec.case.expected, actual=rec.actual,
                                        passed=passed, checks=rec.checks, evidence=evidence)
        self.results[case_id] = {"passed": passed, "run_id": rec.run_id, "actual": rec.actual,
                                 "checks": rec.checks, "evidence": evidence}
        if error is not None:
            raise error
        failed = [c["check"] for c in rec.checks if not c["ok"]]
        assert not failed, f"{case_id} failed: {failed}"


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Suite]:
    settings = load_settings()
    db = Database(settings.database_path)
    reset_demo_data(db, settings.vault_dir)
    clear_evidence(settings.evidence_dir)
    portal_settings = PortalSettings(session_secret="scenario-secret", data_dir=tmp_path_factory.mktemp("portal"),
                                     max_upload_bytes=5 * 1024 * 1024, fault_delay_seconds=2.5,
                                     https_only_cookies=False, log_level="WARNING")
    app = create_portal(portal_settings)
    with serve(app) as url:
        s = Suite(settings, db, app, url)
        try:
            yield s
        finally:
            write_report(s)
            reset_scenario_state(db, settings.vault_dir)  # leave the demo ready for a live TC1 (runs are kept)


# ---------------- official cases ----------------


def test_tc1_upload_after_approval_is_verified(suite: Suite) -> None:
    with suite.case("TC1") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="upload", policy_no=POLICY, test_case="TC1")
        rec.run_id = view.run_id
        rec.check("stops for the portal OTP, nothing read yet", view.status == "waiting_for_otp", view.status)
        view = suite.type_otp(flow, view)
        rec.check("asks a person to approve an upload", view.status == "waiting_for_approval", view.status)
        card = (view.waiting or {}).get("card") or {}
        rec.check("card: the correct synthetic pharmacy invoice", (card.get("file") or {}).get("filename") == INVOICE,
                  (card.get("file") or {}).get("filename"))
        rec.check("card: requirement REQ-0142-01 for patient Ramesh",
                  card.get("requirement", {}).get("requirement_id") == "REQ-0142-01"
                  and card.get("patient", {}).get("name") == "Ramesh")
        rec.check("nothing uploaded before approval", suite.store.upload_log == [], len(suite.store.upload_log))
        view = suite.decide(flow, view, "approve")
        (action,) = suite.actions(view.run_id)
        stored = suite.stored(C142, action.file_sha256)
        with suite.db.read() as conn:
            verification = repo.verifications_for(conn, action.id)[-1]
            reread = repo.get_evidence(conn, verification.evidence_id)
        rec.check("exactly one upload, to REQ-0142-01",
                  len(stored) == 1 and stored[0].requirement_id == "REQ-0142-01", [s.requirement_id for s in stored])
        rec.check("VERIFIED by a fresh read of the document list",
                  action.state == "VERIFIED" and verification.result == "VERIFIED" and reread.kind == "document_list",
                  f"{action.state} / {verification.result} / {reread.kind}")
        f = view.final_response or {}
        doc = next((d for d in f.get("documents", []) if d["requirement_id"] == "REQ-0142-01"), {})
        rec.check("reply: QUERY_RAISED, REQ-0142-01 now Received and verified",
                  f.get("canonical_status") == "QUERY_RAISED" and doc.get("portal_state") == "Received"
                  and doc.get("action_state") == "VERIFIED", (f.get("canonical_status"), doc.get("portal_state")))
        rec.actual = (f"{view.status}; card {card.get('file', {}).get('filename')} for "
                      f"{card.get('requirement', {}).get('requirement_id')}; action {action.state} after "
                      f"{action.attempts} attempt(s); {len(stored)} stored file(s) under {stored[0].requirement_id}; "
                      f"verification {verification.result} ({reread.id}); status {f.get('canonical_status')}")


def test_tc2_no_consent_blocks_before_portal(suite: Suite) -> None:
    with suite.case("TC2") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C150, intent="status", policy_no=POLICY, test_case="TC2")
        rec.run_id = view.run_id
        f = view.final_response or {}
        with suite.db.read() as conn:
            kinds = {e.kind for e in repo.evidence_for_run(conn, view.run_id)}
            request = repo.get_consent_request(conn, f["consent_request_id"]) if f.get("consent_request_id") else None
        rec.check("blocked by the consent gate with NEEDS_CONSENT",
                  view.status == "blocked" and f.get("consent", {}).get("read") == "NEEDS_CONSENT",
                  (view.status, f.get("consent")))
        rec.check("no protected portal read: only consent evidence", kinds == {"consent_record"}, sorted(kinds))
        rec.check("the portal was never contacted (no login, no OTP)", not suite.store.otp_challenges)
        rec.check("a consent request is pending with Lakshmi",
                  request is not None and request.patient_id == "P-LAKSHMI" and request.status == "pending",
                  request and (request.patient_id, request.status))
        rec.actual = (f"{view.status}: {f.get('consent', {}).get('read')}; evidence kinds {sorted(kinds)}; consent "
                      f"request {request.id if request else None} to {request.patient_id if request else None} "
                      f"({request.status if request else '-'})")


def test_tc3_preauth_is_not_the_claim_decision(suite: Suite) -> None:
    with suite.case("TC3") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C163, intent="status", policy_no=POLICY, test_case="TC3")
        rec.run_id = view.run_id
        view = suite.type_otp(flow, view)
        f = view.final_response or {}
        preauth = f.get("preauth") or {}
        rec.check("canonical status UNDER_REVIEW", f.get("canonical_status") == "UNDER_REVIEW", f.get("canonical_status"))
        rec.check("raw portal status kept exactly",
                  f.get("raw_status") == "Under Process - Pending Final Authorisation", f.get("raw_status"))
        rec.check("pre-authorisation APPROVED Rs 50,000 reported separately",
                  preauth.get("status") == "APPROVED" and preauth.get("amount_inr") == 50000, preauth)
        rec.check("reply guarded and never says the claim is approved",
                  f.get("reply_guard") == "PASSED"
                  and not re.search(r"claim [^.]*\b(is|has been) approved", f.get("text", ""), re.I), f.get("text"))
        rec.check("nothing proposed or uploaded", suite.actions(view.run_id) == [] and suite.store.upload_log == [])
        rec.actual = (f"{f.get('canonical_status')} from '{f.get('raw_status')}' (updated {f.get('status_stamp')}); "
                      f"pre-auth {preauth.get('status')} Rs {preauth.get('amount_inr')} shown separately; "
                      f"reply guard {f.get('reply_guard')}")


def test_tc4_timeout_after_store_is_verified_once(suite: Suite) -> None:
    with suite.case("TC4") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="upload", policy_no=POLICY, test_case="TC4")
        rec.run_id = view.run_id
        view = suite.type_otp(flow, view)
        rec.check("asks a person to approve the upload", view.status == "waiting_for_approval", view.status)
        suite.store.set_fault("timeout_after_store", once=True)
        view = suite.decide(flow, view, "approve")
        (action,) = suite.actions(view.run_id)
        with suite.db.read() as conn:
            attempts = repo.attempts_for(conn, action.id)
            verifications = repo.verifications_for(conn, action.id)
        step = next((s for s in view.steps if s["node"] == "execute_upload"), {})
        stored = suite.stored(C142, action.file_sha256)
        rec.check("the attempt timed out and was recorded UNKNOWN",
                  [a.outcome for a in attempts] == ["timeout"] and "UNKNOWN" in step.get("decision", ""),
                  step.get("decision"))
        rec.check("the re-read found the file: VERIFIED",
                  action.state == "VERIFIED" and verifications[-1].input_refs["state_before"] == "UNKNOWN"
                  and verifications[-1].result == "VERIFIED", (action.state, verifications[-1].input_refs["state_before"]))
        rec.check("exactly one upload, no second attempt",
                  action.attempts == 1 and len(suite.store.upload_log) == 1 and len(stored) == 1,
                  (action.attempts, len(suite.store.upload_log), len(stored)))
        rec.actual = (f"attempt {attempts[0].outcome if attempts else '-'}; path {step.get('decision', '')[:90]}; "
                      f"action {action.state}, {action.attempts} attempt(s); portal log "
                      f"{[e.outcome for e in suite.store.upload_log]}; {len(stored)} stored file(s)")


# ---------------- edge cases ----------------


def test_e1_wrong_patient_invoice(suite: Suite) -> None:
    with suite.case("E1") as rec:
        (suite.settings.vault_dir / "seed" / "arjun" / INVOICE).unlink()
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="upload", test_case="E1")
        rec.run_id = view.run_id
        view = suite.type_otp(flow, view)
        f = view.final_response or {}
        doc = next((d for d in f.get("documents", []) if d["requirement_id"] == "REQ-0142-01"), {})
        rec.check("completes without asking for approval", view.status == "completed", view.status)
        rec.check("REQ-0142-01 is MISMATCH: the invoice is for Lakshmi",
                  doc.get("match") == "MISMATCH" and "is for Lakshmi, but this claim is for Ramesh" in doc.get("reason", ""),
                  doc.get("reason"))
        rec.check("nothing proposed or uploaded", suite.actions(view.run_id) == [] and suite.store.upload_log == [])
        rec.actual = f"{view.status}; REQ-0142-01 {doc.get('match')}: {doc.get('reason', '')[:160]}"


def test_e2_revoked_consent(suite: Suite) -> None:
    with suite.case("E2") as rec:
        cs.revoke(suite.db, "CON-SEED-01", by_person_id="P-RAMESH")
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="status", test_case="E2")
        rec.run_id = view.run_id
        f = view.final_response or {}
        with suite.db.read() as conn:
            kinds = {e.kind for e in repo.evidence_for_run(conn, view.run_id)}
            request = repo.get_consent_request(conn, f["consent_request_id"]) if f.get("consent_request_id") else None
        rec.check("blocked with EXPIRED_OR_REVOKED",
                  view.status == "blocked" and f.get("consent", {}).get("read") == "EXPIRED_OR_REVOKED", f.get("consent"))
        rec.check("no portal contact", kinds == {"consent_record"} and not suite.store.otp_challenges, sorted(kinds))
        rec.check("a new consent request is pending with Ramesh",
                  request is not None and request.patient_id == "P-RAMESH" and request.status == "pending")
        rec.actual = f"{view.status}: {f.get('consent', {}).get('read')}; request {request.id if request else None}"


def test_e3_unknown_raw_status(suite: Suite) -> None:
    with suite.case("E3") as rec:
        suite.store.set_status(C142, "Escalated to medical panel")
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="status", test_case="E3")
        rec.run_id = view.run_id
        f = (suite.type_otp(flow, view)).final_response or {}
        rec.check("canonical status UNCERTAIN", f.get("canonical_status") == "UNCERTAIN", f.get("canonical_status"))
        rec.check("raw status shown as read", f.get("raw_status") == "Escalated to medical panel", f.get("raw_status"))
        rec.check("reply says it could not confirm and assumes nothing",
                  "could not confirm the status" in f.get("text", "") and "approved" not in f.get("text", "").lower(),
                  f.get("text"))
        rec.actual = f"{f.get('canonical_status')} from '{f.get('raw_status')}': {f.get('text', '')[:140]}"


def test_e4_manual_otp(suite: Suite) -> None:
    with suite.case("E4") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="status", test_case="E4")
        rec.run_id = view.run_id
        right = suite.otp_code()
        wrong = "000000" if right != "000000" else "111111"
        view = suite.type_otp(flow, view, wrong)
        with suite.db.read() as conn:
            portal_evidence = [e for e in repo.evidence_for_run(conn, view.run_id) if e.kind != "consent_record"]
        rec.check("wrong code refused; the run asks again",
                  view.status == "waiting_for_otp" and bool((view.waiting or {}).get("last_error")),
                  (view.waiting or {}).get("last_error"))
        rec.check("nothing read before the right code", portal_evidence == [])
        view = suite.type_otp(flow, view, right)
        rec.check("the right code continues to a completed answer",
                  view.status == "completed" and (view.final_response or {}).get("canonical_status") == "QUERY_RAISED")
        rec.check("one login only", len(suite.store.otp_challenges) <= 1, len(suite.store.otp_challenges))
        source = (PROJECT_ROOT / "agent" / "portal_client.py").read_text(encoding="utf-8")
        rec.check("the adapter never reads the OTP from the page", "demo-sms-otp" not in source)
        rec.actual = f"wrong code -> {(view.steps[4] if len(view.steps) > 4 else {}).get('decision', '')[:80]}; " \
                     f"right code -> {view.status}"


def test_e5_portal_outage(suite: Suite) -> None:
    with suite.case("E5") as rec:
        flow = suite.flow(url="http://127.0.0.1:9")  # nothing listens here
        view = flow.start("P-ARJUN", C142, intent="status", test_case="E5")
        rec.run_id = view.run_id
        f = view.final_response or {}
        rec.check("run failed with FETCH_FAILED", view.status == "failed" and f.get("fetch") == "FETCH_FAILED",
                  (view.status, f.get("fetch")))
        rec.check("no claim status reported", f.get("canonical_status") is None, f.get("canonical_status"))
        rec.check("reply is honest: cannot say whether anything changed, never 'no change'",
                  "cannot say whether anything changed" in f.get("text", "") and "no change" not in f.get("text", "").lower(),
                  f.get("text"))
        rec.actual = f"{view.status} / {f.get('fetch')}: {f.get('text', '')[:140]}"


def test_e6_hospital_held_documents(suite: Suite) -> None:
    with suite.case("E6") as rec:
        flow = suite.flow()
        view = flow.start("P-ARJUN", C142, intent="upload", test_case="E6")
        rec.run_id = view.run_id
        view = suite.type_otp(flow, view)
        asked_for = (view.waiting or {}).get("card", {}).get("requirement", {}).get("requirement_id")
        rec.check("the only approval asked is for REQ-0142-01", asked_for == "REQ-0142-01", asked_for)
        view = suite.decide(flow, view, "reject")
        f = view.final_response or {}
        doc = next((d for d in f.get("documents", []) if d["requirement_id"] == "REQ-0142-02"), {})
        rec.check("REQ-0142-02 is HOSPITAL_SHOULD_PROVIDE", doc.get("match") == "HOSPITAL_SHOULD_PROVIDE", doc.get("match"))
        rec.check("a note to the hospital TPA desk is drafted, citing IRDAI 17(c)",
                  "17(c)" in (doc.get("hospital_request") or "") and "Sunrise" in (doc.get("hospital_request") or ""))
        rec.check("never proposed or uploaded",
                  all(a.requirement_id != "REQ-0142-02" for a in suite.actions(view.run_id))
                  and suite.store.upload_log == [])
        rec.actual = f"REQ-0142-02 {doc.get('match')}; next action '{doc.get('next_action')}'; uploads {len(suite.store.upload_log)}"


# ---------------- report ----------------


def _link(path: str | None) -> str:
    if not path:
        return "-"
    rel = os.path.relpath(path, REPORT.parent).replace("\\", "/")
    return f"[screenshot]({rel})"


def write_report(suite: Suite) -> None:
    lines = [
        "# ClaimSaathi test evidence", "",
        f"Generated by `pytest -m scenario` at {utc_now()} (UTC). All people, claims and documents are synthetic; the",
        "TPA portal is the local mock, started in-process for this run. Nothing here involves a real insurer.", "",
        f"- Agent database: `{suite.settings.database_path}` (demo data reset before the run; each case's run,",
        "  steps and evidence are kept and listed on the agent's **Test runs** page).",
        f"- Evidence folder: `{suite.settings.evidence_dir}` (screenshots and HTML per run; links below are relative).",
        f"- Portal: `{suite.portal_url}` driven by headless Chromium through the agent's PortalClient.",
        "- The test plays the family member: it reads the OTP from the portal's simulated SMS panel state (as a person",
        "  reads their phone) and types it through the workflow's resume API, and approves or rejects the card shown.",
        "- A case is PASS only if it ran and every listed check held. Cases that did not run are NOT RUN.", "",
        "## Summary", "", "| Case | Title | Verdict | Run |", "| --- | --- | --- | --- |",
    ]
    for case in CASES.values():
        r = suite.results.get(case.id)
        verdict = "NOT RUN" if r is None else ("PASS" if r["passed"] else "**FAIL**")
        run = f"`{r['run_id']}`" if r and r["run_id"] else "-"
        lines.append(f"| {case.id} | {case.title} | {verdict} | {run} |")
    for case in CASES.values():
        r = suite.results.get(case.id)
        lines += ["", f"## {case.id}: {case.title}", "", f"- **Input:** `{json.dumps(case.input)}`",
                  f"- **Expected:** {case.expected}"]
        if r is None:
            lines.append("- **Verdict:** NOT RUN (no result was recorded for this case)")
            continue
        lines += [f"- **Actual:** {r['actual'] or '(not reached)'}",
                  f"- **Verdict:** {'PASS' if r['passed'] else 'FAIL'}",
                  f"- **Run:** `{r['run_id']}` ({AGENT_URL}/workflows/{r['run_id']} when the agent runs on this database)"
                  if r["run_id"] else "- **Run:** none", "", "Checks:", ""]
        lines += [f"- [{'x' if c['ok'] else ' '}] {c['check']}" + (f" ({c['detail']})" if c["detail"] and not c["ok"]
                                                                   else "") for c in r["checks"]]
        if r["evidence"]:
            lines += ["", "| Evidence | Kind | Captured (UTC) | HTML SHA-256 | Screenshot |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| [{e['id']}]({e['url']}) | {e['kind']} | {e['fetched_at']} | "
                      f"{('`' + e['html_sha256'][:16] + '…`') if e['html_sha256'] else '-'} | {_link(e['screenshot'])} |"
                      for e in r["evidence"]]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
