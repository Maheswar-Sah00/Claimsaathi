"""Consent pages (patient review, approve, deny, revoke) and the requester's access-check API.

Identity always comes from the signed session (`require_person`). A request body may name
a claim and an action, never a patient or a requester.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from agent import consent as consent_service
from agent import ids
from agent import repository as repo
from agent.auth import render, require_person
from agent.consent import ALLOWED_DURATIONS_DAYS
from agent.models import ConsentAction, ConsentResult
from shared.websec import check_csrf

router = APIRouter()

ACTION_LABELS = {
    ConsentAction.READ_STATUS.value: "See the claim's status, the documents the insurer asked for, and documents already uploaded",
    ConsentAction.UPLOAD_DOCS.value: "Upload documents to this claim, each one only after you or they approve it",
}


def _db(request: Request):  # noqa: ANN202
    return request.app.state.db


def _request_view(request: Request, request_id: ids.ConsentRequestId, person_id: str) -> dict:
    db = _db(request)
    req = consent_service.view_request(db, request_id, viewer_id=person_id)
    with db.read() as conn:
        people = {p.id: p for p in repo.list_people(conn)}
    return {"req": req, "people": people, "is_patient": person_id == req.patient_id}


# ---------------- pages ----------------


@router.get("/consent", response_class=HTMLResponse)
async def consent_home(request: Request, done: str | None = None, person_id: str = Depends(require_person)) -> HTMLResponse:
    """Everything the signed-in person can review and revoke."""
    data = consent_service.overview(_db(request), person_id)
    return render(request, "consent_dashboard.html", {**data, "done": done, "labels": ACTION_LABELS})


@router.get("/consent/requests/{request_id}", response_class=HTMLResponse)
async def consent_request_page(request: Request, request_id: ids.ConsentRequestId, person_id: str = Depends(require_person)) -> HTMLResponse:
    """Review page. Only the patient sees the approve/deny form; the server re-checks on submit."""
    context = _request_view(request, request_id, person_id)
    return render(request, "consent_request.html",
                  {**context, "labels": ACTION_LABELS, "durations": ALLOWED_DURATIONS_DAYS})


@router.post("/consent/requests/{request_id}/approve", response_model=None)
async def approve(
    request: Request,
    request_id: ids.ConsentRequestId,
    csrf_token: str = Form(""),
    actions: list[str] = Form(default=[]),
    duration_days: int = Form(7),
    scope: Literal["claim", "all_claims"] = Form("claim"),
    person_id: str = Depends(require_person),
) -> Response:
    """Grant consent as the signed-in patient."""
    check_csrf(request, csrf_token)
    consent_service.approve_request(
        _db(request), request_id, patient_session_id=person_id, actions=actions,
        duration_days=duration_days, all_claims=scope == "all_claims",
    )
    return RedirectResponse("/consent?done=approved", status_code=303)


@router.post("/consent/requests/{request_id}/deny", response_model=None)
async def deny(request: Request, request_id: ids.ConsentRequestId, csrf_token: str = Form(""),
               person_id: str = Depends(require_person)) -> Response:
    """Refuse a request as the signed-in patient."""
    check_csrf(request, csrf_token)
    consent_service.deny_request(_db(request), request_id, patient_session_id=person_id)
    return RedirectResponse("/consent?done=denied", status_code=303)


@router.post("/consent/consents/{consent_id}/revoke", response_model=None)
async def revoke(request: Request, consent_id: ids.ConsentId, csrf_token: str = Form(""),
                 person_id: str = Depends(require_person)) -> Response:
    """Revoke a consent (patient) or give up access (grantee)."""
    check_csrf(request, csrf_token)
    consent_service.revoke(_db(request), consent_id, by_person_id=person_id)
    return RedirectResponse("/consent?done=revoked", status_code=303)


# ---------------- API ----------------


class AccessCheckBody(BaseModel):
    """What the requester wants to do. Deliberately has no patient or requester field."""

    claim_id: str
    action: ConsentAction


@router.post("/api/access-check")
async def access_check(
    request: Request,
    body: AccessCheckBody,
    x_csrf_token: str | None = Header(default=None),
    person_id: str = Depends(require_person),
) -> JSONResponse:
    """Check consent for the signed-in requester. If it does not pass, a consent request
    is created (or reused) for the claim's patient and its review link is returned."""
    check_csrf(request, x_csrf_token)
    db = _db(request)
    decision = consent_service.check_consent(db, person_id, body.claim_id, body.action.value)
    payload = {"decision": decision.public(), "consent_request": None}
    if not decision.allowed and decision.patient_id is not None:
        req = consent_service.request_consent(db, person_id, body.claim_id, [body.action.value],
                                              run_id=decision.run_id)
        payload["consent_request"] = {
            "id": req.id,
            "status": req.status,
            "actions": list(req.actions),
            "expires_at": req.expires_at,
            "review_url": f"/consent/requests/{req.id}",
        }
    status = 200 if decision.result is ConsentResult.ALLOW else 403
    return JSONResponse(payload, status_code=status)


@router.get("/api/consent-requests/{request_id}")
async def get_consent_request(request: Request, request_id: ids.ConsentRequestId,
                              person_id: str = Depends(require_person)) -> dict:
    """Status of a request, for its patient or requester."""
    req = consent_service.view_request(_db(request), request_id, viewer_id=person_id)
    return {"id": req.id, "claim_id": req.claim_id, "actions": list(req.actions), "status": req.status,
            "created_at": req.created_at, "expires_at": req.expires_at, "decided_at": req.decided_at}


class ConsentDecisionBody(BaseModel):
    """The patient's choice. Who decides comes from the signed-in session, never from this body."""

    model_config = ConfigDict(extra="forbid")

    actions: list[ConsentAction] | None = None
    duration_days: int = 7
    all_claims: bool = False


@router.post("/api/consent-requests/{request_id}/approve")
async def approve_api(request: Request, request_id: ids.ConsentRequestId, body: ConsentDecisionBody,
                      x_csrf_token: str | None = Header(default=None),
                      person_id: str = Depends(require_person)) -> dict:
    """Grant consent from a pending request. Only the patient it names, signed in with their own OTP."""
    check_csrf(request, x_csrf_token)
    consent = consent_service.approve_request(
        _db(request), request_id, patient_session_id=person_id,
        actions=[a.value for a in body.actions] if body.actions is not None else None,
        duration_days=body.duration_days, all_claims=body.all_claims,
    )
    return {"consent_id": consent.id, "claim_id": consent.claim_id, "actions": list(consent.actions),
            "expires_at": consent.expires_at}


@router.post("/api/consent-requests/{request_id}/deny")
async def deny_api(request: Request, request_id: ids.ConsentRequestId, x_csrf_token: str | None = Header(default=None),
                   person_id: str = Depends(require_person)) -> dict:
    """Refuse a pending request as its patient."""
    check_csrf(request, x_csrf_token)
    req = consent_service.deny_request(_db(request), request_id, patient_session_id=person_id)
    return {"id": req.id, "status": req.status}
