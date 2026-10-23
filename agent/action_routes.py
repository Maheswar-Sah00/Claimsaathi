"""Upload approval card, the person's Approve / Reject decision, and execution with read-back.

Identity always comes from the signed-in session; a person only ever sees and decides their own
uploads. Approve posts the digest of the card that was shown, so an approval can only ever be for
that exact claim, requirement and file. Uploads run on the portal session's own thread.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from agent import actions as act
from agent import consent as cs
from agent import ids
from agent import repository as repo
from agent.auth import render, require_person
from agent.matcher import MatcherError
from agent.portal_client import PortalError, SessionPortal
from shared.errors import AppError
from shared.websec import check_csrf

router = APIRouter()

STATE_LABELS = {
    "PROPOSED": "Waiting for your approval", "REJECTED": "Rejected by you", "APPROVED": "Approved, not uploaded yet",
    "EXECUTING": "Uploading", "PENDING_VERIFY": "Uploaded, checking the portal", "UNKNOWN": "Outcome unknown",
    "RETRY_ALLOWED": "Not on the portal; retry allowed", "VERIFIED": "Verified on the portal", "FAILED": "Failed",
}


def _policy(request: Request) -> act.UploadPolicy:
    return act.UploadPolicy.from_settings(request.app.state.settings)


def _executor(request: Request) -> act.UploadExecutor:
    policy = _policy(request)
    return act.UploadExecutor(request.app.state.db, SessionPortal(request.app.state.portal_session,
                                                                  policy.upload_timeout_ms), policy)


def _card_json(card: act.ApprovalCard) -> dict[str, Any]:
    return {"action": asdict(card.action), "card": card.card, "card_sha256": card.card_sha256,
            "can_approve": card.can_approve, "card_url": f"/actions/{card.action.id}"}


# ---------------- JSON API ----------------


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(pattern=r"^DEC-[0-9a-f]{12}$")


class DecisionBody(BaseModel):
    """Approve or reject only. The browser can never set an action's state (e.g. VERIFIED)."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|reject)$")
    card_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


@router.post("/api/runs/{run_id}/actions")
def propose(request: Request, run_id: ids.RunId, body: ProposeBody, x_csrf_token: str | None = Header(default=None),
            person_id: str = Depends(require_person)) -> dict:
    """Propose an upload from a MATCHED decision in your run. Nothing is uploaded until you approve."""
    check_csrf(request, x_csrf_token)
    try:
        action, created = act.propose_from_match(request.app.state.db, run_id=run_id, decision_id=body.decision_id,
                                                 person_id=person_id)
    except MatcherError as exc:
        raise AppError("not_uploadable", str(exc), 409) from exc
    card = act.approval_card(request.app.state.db, action.id, person_id=person_id, policy=_policy(request))
    return {**_card_json(card), "created": created}


@router.get("/api/actions/{action_id}")
def card_api(request: Request, action_id: ids.ActionId, person_id: str = Depends(require_person)) -> dict:
    """The approval card, the current state and the full history of one of your uploads."""
    card = act.approval_card(request.app.state.db, action_id, person_id=person_id, policy=_policy(request))
    history = act.action_history(request.app.state.db, action_id, person_id=person_id)
    return {**_card_json(card), "state_label": STATE_LABELS[card.action.state],
            "approval": asdict(history["approval"]) if history["approval"] else None,
            "attempts": [asdict(a) for a in history["attempts"]],
            "verifications": [asdict(v) for v in history["verifications"]],
            "checks": [asdict(c) for c in history["checks"]]}


@router.post("/api/actions/{action_id}/decision")
def decide_api(request: Request, action_id: ids.ActionId, body: DecisionBody, x_csrf_token: str | None = Header(default=None),
               person_id: str = Depends(require_person)) -> dict:
    """Approve (with the digest of the card you were shown) or reject one upload."""
    check_csrf(request, x_csrf_token)
    db, policy = request.app.state.db, _policy(request)
    if body.decision == "approve":
        if body.card_sha256 is None:
            raise AppError("card_required", "Send the card_sha256 of the card you approved.", 400)
        action, new = act.approve_upload(db, action_id, person_id=person_id, card_sha256=body.card_sha256,
                                         policy=policy)
    else:
        action, new = act.reject_upload(db, action_id, person_id=person_id, policy=policy)
    return {"action": asdict(action), "recorded": new}


@router.post("/api/actions/{action_id}/execute")
def execute_api(request: Request, action_id: ids.ActionId, x_csrf_token: str | None = Header(default=None),
                person_id: str = Depends(require_person)) -> dict:
    """Upload an approved file once and verify it by re-reading the portal (also: guarded retry)."""
    check_csrf(request, x_csrf_token)
    return _executor(request).execute(action_id, requester_id=person_id).to_json()


@router.post("/api/actions/{action_id}/reconcile")
def reconcile_api(request: Request, action_id: ids.ActionId, x_csrf_token: str | None = Header(default=None),
                  person_id: str = Depends(require_person)) -> dict:
    """Re-read the portal for an upload whose outcome is unknown. Never uploads."""
    check_csrf(request, x_csrf_token)
    return _executor(request).reconcile(action_id, requester_id=person_id).to_json()


# ---------------- pages ----------------


@router.get("/actions", response_class=HTMLResponse)
def actions_page(request: Request, person_id: str = Depends(require_person)) -> HTMLResponse:
    """Your proposed and finished uploads."""
    with request.app.state.db.read() as conn:
        items = repo.actions_for_requester(conn, person_id)
    return render(request, "actions.html", {"items": items, "labels": STATE_LABELS})


@router.get("/actions/{action_id}", response_class=HTMLResponse)
def card_page(request: Request, action_id: ids.ActionId, error: str | None = None, done: str | None = None,
              person_id: str = Depends(require_person)) -> HTMLResponse:
    card = act.approval_card(request.app.state.db, action_id, person_id=person_id, policy=_policy(request))
    history = act.action_history(request.app.state.db, action_id, person_id=person_id)
    blocked = next((c for c in reversed(history["checks"]) if not c.passed), None)
    return render(request, "action_card.html", {
        "card": card.card, "card_sha256": card.card_sha256, "can_approve": card.can_approve,
        "checks": card.checks, "action": card.action, "state_label": STATE_LABELS[card.action.state],
        "history": history, "error": error, "done": done, "last_blocked": blocked,
        "labels": act.CHECK_LABELS,
    })


def _back(action_id: ids.ActionId, *, error: str | None = None, done: str | None = None) -> RedirectResponse:
    query = f"?error={quote(error)}" if error else f"?done={quote(done)}" if done else ""
    return RedirectResponse(f"/actions/{quote(action_id)}{query}", status_code=303)


def _run_form(action_id: ids.ActionId, step: Any) -> Response:
    """Run one step for a form post and come back to the card, with what happened."""
    try:
        done = step()
    except (act.ActionError, PortalError) as exc:
        return _back(action_id, error=exc.code)
    except cs.ConsentDenied:
        return _back(action_id, error="consent_denied")
    except repo.InvalidTransition:
        return _back(action_id, error="already_decided")
    return _back(action_id, done=done)


@router.post("/actions/{action_id}/approve", response_model=None)
def approve_form(request: Request, action_id: ids.ActionId, card_sha256: str = Form(...), csrf_token: str = Form(""),
                 person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _run_form(action_id, lambda: act.approve_upload(
        request.app.state.db, action_id, person_id=person_id, card_sha256=card_sha256, policy=_policy(request),
    ) and "approved")


@router.post("/actions/{action_id}/reject", response_model=None)
def reject_form(request: Request, action_id: ids.ActionId, csrf_token: str = Form(""),
                person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _run_form(action_id, lambda: act.reject_upload(
        request.app.state.db, action_id, person_id=person_id, policy=_policy(request)) and "rejected")


@router.post("/actions/{action_id}/execute", response_model=None)
def execute_form(request: Request, action_id: ids.ActionId, csrf_token: str = Form(""),
                 person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _run_form(action_id, lambda: _executor(request).execute(action_id, requester_id=person_id).action.state)


@router.post("/actions/{action_id}/reconcile", response_model=None)
def reconcile_form(request: Request, action_id: ids.ActionId, csrf_token: str = Form(""),
                   person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    return _run_form(action_id, lambda: _executor(request).reconcile(action_id, requester_id=person_id).action.state)

