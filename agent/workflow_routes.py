"""Start, watch and resume workflow runs (agent.graph) over HTTP.

The signed-in person is the requester; only they can see or resume their runs. A run that waits
for the portal OTP or an upload decision shows what it needs; the answer resumes the same thread,
so nothing is redone and no upload can be started twice.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from agent import ids
from agent import repository as repo
from agent.auth import render, require_person
from agent.db import utc_now
from agent.graph import ClaimWorkflow, WorkflowError, WorkflowView, workflow_from_settings
from agent.llm import llm_from_settings
from agent.portal_client import PortalError, SessionPortal
from agent.reply import STATUS_SENTENCES
from shared.websec import check_csrf

router = APIRouter()
_build_lock = threading.Lock()

STATUS_LABELS = {
    "waiting_for_otp": "Waiting for the portal OTP", "waiting_for_approval": "Waiting for your upload decision",
    "waiting_for_retry": "Waiting: retry the upload?", "running": "Stopped part-way (can be continued)",
    "completed": "Done", "blocked": "Stopped by a safety rule", "failed": "Could not reach the portal",
}


def workflow(request: Request) -> ClaimWorkflow:
    """The app's workflow runner, built on first use (tests may set app.state.workflow)."""
    app = request.app
    with _build_lock:
        if getattr(app.state, "workflow", None) is None:
            settings, session = app.state.settings, app.state.portal_session
            app.state.workflow = workflow_from_settings(
                settings, app.state.db,
                lambda person: SessionPortal(session, settings.portal_upload_timeout_ms, owner=person),
                llm=llm_from_settings(settings),
            )
        return app.state.workflow


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=ids.CLAIM_ID)
    intent: str = Field(default="status", pattern="^(status|upload)$")
    policy_no: str | None = Field(default=None, pattern=ids.POLICY_NO)


class ResumeBody(BaseModel):
    """Only what a person can supply. There is no field for a state: the server decides every state."""

    model_config = ConfigDict(extra="forbid")

    otp: str | None = Field(default=None, max_length=12)
    decision: str | None = Field(default=None, pattern="^(approve|reject)$")
    card_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    action_id: str | None = Field(default=None, pattern=ids.ACTION_ID)


# ---------------- JSON API ----------------


@router.post("/api/workflows")
def start_api(request: Request, body: StartBody, x_csrf_token: str | None = Header(default=None),
              person_id: str = Depends(require_person)) -> dict:
    """Start a run for one claim: `status` only reads; `upload` may propose uploads for your approval."""
    check_csrf(request, x_csrf_token)
    return workflow(request).start(person_id, body.claim_id, intent=body.intent, policy_no=body.policy_no).to_json()


@router.get("/api/workflows/{run_id}")
def view_api(request: Request, run_id: ids.RunId, person_id: str = Depends(require_person)) -> dict:
    """Where the run is, what it is waiting for, every step taken, and the reply."""
    return workflow(request).view(run_id, person_id=person_id).to_json()


@router.post("/api/workflows/{run_id}/resume")
def resume_api(request: Request, run_id: ids.RunId, body: ResumeBody, x_csrf_token: str | None = Header(default=None),
               person_id: str = Depends(require_person)) -> dict:
    """Answer what the run is waiting for: {"otp"} or {"decision", "card_sha256", "action_id"}."""
    check_csrf(request, x_csrf_token)
    answer = body.model_dump(exclude_none=True)
    return workflow(request).resume(run_id, person_id=person_id, answer=answer).to_json()


@router.post("/api/workflows/{run_id}/continue")
def continue_api(request: Request, run_id: ids.RunId, x_csrf_token: str | None = Header(default=None),
                 person_id: str = Depends(require_person)) -> dict:
    """Continue a run that stopped part-way (crash or restart) from its last saved step."""
    check_csrf(request, x_csrf_token)
    return workflow(request).continue_run(run_id, person_id=person_id).to_json()


# ---------------- pages ----------------


STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Request checked", ("intake_validate",)), ("Consent", ("consent_gate",)),
    ("Portal sign-in", ("portal_login", "otp_entry")), ("Claim read", ("read_claim",)),
    ("Status worked out", ("extract_and_normalise",)), ("Documents matched", ("match_documents",)),
    ("Your approval", ("human_approval",)), ("Upload", ("execute_upload",)),
    ("Checked and answered", ("verify_and_reply", "finish")),
)
STAGE_WORDS = {"done": "Done", "waiting": "Waiting for you", "blocked": "Stopped", "failed": "Failed",
               "skipped": "Not needed", "pending": "Upcoming"}
_STEP_STATE = {"OK": "done", "SKIPPED": "skipped", "INTERRUPTED": "waiting", "BLOCKED": "blocked",
               "FETCH_FAILED": "failed", "FAILED": "failed"}
PLAIN_STATUS = {status.value: sentence for status, sentence in STATUS_SENTENCES.items()} | {
    "UNCERTAIN": "the status could not be confirmed, so nothing is assumed"}


def progress(view: WorkflowView) -> list[dict[str, str]]:
    """Each stage of the graph and where it stands, from the recorded steps (never guessed)."""
    finished = view.status in ("completed", "blocked", "failed")
    out = []
    for label, nodes in STAGES:
        rows = [s for s in view.steps if s["node"] in nodes]
        state = _STEP_STATE[rows[-1]["result"]] if rows else ("skipped" if finished else "pending")
        out.append({"label": label, "state": state, "word": STAGE_WORDS[state]})
    return out


def _evidence_index(request: Request, view: WorkflowView) -> dict[str, dict[str, Any]]:
    wanted = {e for s in view.steps for e in s["evidence_ids"]} | set((view.final_response or {}).get("evidence") or [])
    with request.app.state.db.read() as conn:
        rows = [e for e in repo.evidence_for_run(conn, view.run_id) if e.id in wanted]
    return {e.id: {"kind": e.kind, "html_sha256": e.html_sha256, "screenshot": bool(e.screenshot_path),
                   "fetched_at": e.fetched_at} for e in rows}


def _dashboard(request: Request, person_id: str, error: str | None) -> HTMLResponse:
    with request.app.state.db.read() as conn:
        runs = repo.runs_for_requester(conn, person_id, limit=12)
        claims = [dict(r) for r in conn.execute("SELECT claim_id, policy_no FROM claims ORDER BY claim_id")]
        received = repo.consents_for(conn, grantee_id=person_id)
        sent = repo.consent_requests_for(conn, requester_id=person_id)
        incoming = [r for r in repo.consent_requests_for(conn, patient_id=person_id) if r.status == "pending"]
        people = {p.id: p for p in repo.list_people(conn)}
        me = people[person_id]
    return render(request, "dashboard.html", {
        "runs": runs, "claims": claims, "received": received, "sent": sent[:6], "incoming": incoming,
        "people": people, "me": me, "error": error, "now": utc_now(),
        "policies": sorted({c["policy_no"] for c in claims}),
    })


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, error: str | None = None, person_id: str = Depends(require_person)) -> HTMLResponse:
    """Start a claim check, see consent, and pick up earlier checks."""
    return _dashboard(request, person_id, error)


@router.get("/workflows", response_class=HTMLResponse)
def runs_page(request: Request, error: str | None = None, person_id: str = Depends(require_person)) -> HTMLResponse:
    return _dashboard(request, person_id, error)


@router.get("/workflows/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: ids.RunId, error: str | None = None, tab: str = "claim",
             person_id: str = Depends(require_person)) -> HTMLResponse:
    view = workflow(request).view(run_id, person_id=person_id)
    final = view.final_response or {}
    consent_request, patient = None, None
    with request.app.state.db.read() as conn:
        request_id = (final.get("consent") or {}).get("request_id")
        if request_id:
            consent_request = repo.get_consent_request(conn, request_id)
            patient = repo.get_person(conn, consent_request.patient_id)
    return render(request, "workflow.html", {
        "view": view, "label": STATUS_LABELS.get(view.status, view.status), "error": error,
        "tab": "trace" if tab == "trace" else "claim", "progress": progress(view),
        "evidence": _evidence_index(request, view), "plain": PLAIN_STATUS, "consent_request": consent_request,
        "patient": patient, "unknown_uploads": [d for d in final.get("documents") or []
                                                if d.get("action_state") in ("UNKNOWN", "PENDING_VERIFY", "EXECUTING")],
    })


def _go(path: str, error: str | None = None) -> RedirectResponse:
    return RedirectResponse(path + (f"?error={quote(error)}" if error else ""), status_code=303)


def _form(run_id: ids.RunId, step: Any) -> Response:
    try:
        step()
    except (WorkflowError, PortalError) as exc:
        return _go(f"/workflows/{quote(run_id)}", exc.code)
    return _go(f"/workflows/{quote(run_id)}")


@router.post("/workflows", response_model=None)
def start_form(request: Request, claim_id: str = Form(...), intent: str = Form("status"), policy_no: str = Form(""),
               csrf_token: str = Form(""), person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    claim_id, policy_no = claim_id.strip().upper(), policy_no.strip().upper()
    if intent not in ("status", "upload"):
        return _go("/dashboard", "invalid_request")
    try:
        view = workflow(request).start(person_id, claim_id, intent=intent, policy_no=policy_no or None)
    except WorkflowError as exc:
        return _go("/dashboard", exc.code)
    return _go(f"/workflows/{quote(view.run_id)}")


@router.post("/workflows/{run_id}/otp", response_model=None)
def otp_form(request: Request, run_id: ids.RunId, otp: str = Form(""), csrf_token: str = Form(""),
             person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _form(run_id, lambda: workflow(request).resume(run_id, person_id=person_id, answer={"otp": otp}))


@router.post("/workflows/{run_id}/decision", response_model=None)
def decision_form(request: Request, run_id: ids.RunId, decision: str = Form(...), action_id: str = Form(...),
                  card_sha256: str = Form(""), csrf_token: str = Form(""),
                  person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    answer = {"decision": decision, "action_id": action_id, "card_sha256": card_sha256 or None}
    return _form(run_id, lambda: workflow(request).resume(run_id, person_id=person_id, answer=answer))


@router.post("/workflows/{run_id}/continue", response_model=None)
def continue_form(request: Request, run_id: ids.RunId, csrf_token: str = Form(""),
                  person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _form(run_id, lambda: workflow(request).continue_run(run_id, person_id=person_id))
