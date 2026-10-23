"""Controlled interrupt for the portal login: the agent stops at the OTP screen, shows a person
a screenshot of it, and continues only with the code that person types.

The screenshot is served from memory to the person who started the session; no filesystem
path is ever returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from agent.auth import render, require_person
from agent.portal_client import LoginState, OtpRejected, PortalError, PortalSession
from shared.errors import AppError
from shared.websec import check_csrf

router = APIRouter()
OTP_SCREEN_URL = "/api/portal/session/otp-screen"


def _session(request: Request) -> PortalSession:
    return request.app.state.portal_session


def _require_owner(session: PortalSession, person_id: str) -> None:
    if session.owner is not None and session.owner != person_id:
        raise AppError("forbidden", "Another person is handling this portal sign-in.", 403)


def _start(session: PortalSession, person_id: str) -> dict:
    _require_owner(session, person_id)
    status = session.call("login")
    session.owner = person_id
    session.otp_screen = status.screenshot
    return {
        "state": status.state.value,
        "message": status.message,
        "otp_screen_url": OTP_SCREEN_URL if status.state is LoginState.OTP_REQUIRED else None,
    }


def _submit(session: PortalSession, person_id: str, otp: str) -> dict:
    if session.owner != person_id:
        raise AppError("forbidden", "Only the person who started the portal sign-in can enter the OTP.", 403)
    try:
        status = session.call("submit_otp", otp)
    except OtpRejected as exc:
        if exc.artifacts and exc.artifacts.screenshot:
            session.otp_screen = exc.artifacts.screenshot
        raise
    session.otp_screen = None
    return {"state": status.state.value, "message": status.message}


class OtpBody(BaseModel):
    otp: str


@router.post("/api/portal/session")
def start_session(request: Request, x_csrf_token: str | None = Header(default=None),
                  person_id: str = Depends(require_person)) -> dict:
    """Open the portal and sign in up to the OTP screen."""
    check_csrf(request, x_csrf_token)
    return _start(_session(request), person_id)


@router.get("/api/portal/session")
def session_status(request: Request, person_id: str = Depends(require_person)) -> dict:
    """Current portal sign-in state."""
    session = _session(request)
    return {"state": session.state.value, "is_owner": session.owner == person_id,
            "otp_screen_url": OTP_SCREEN_URL if session.otp_screen and session.owner == person_id else None}


@router.get(OTP_SCREEN_URL)
def otp_screen(request: Request, person_id: str = Depends(require_person)) -> Response:
    """Screenshot of the portal's OTP screen, for the person entering the code."""
    session = _session(request)
    if session.owner != person_id or not session.otp_screen:
        raise AppError("not_found", "No OTP screen is waiting for you.", 404)
    return Response(session.otp_screen, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/api/portal/session/otp")
def submit_otp(request: Request, body: OtpBody, x_csrf_token: str | None = Header(default=None),
               person_id: str = Depends(require_person)) -> dict:
    """Continue the paused session with the OTP a person typed."""
    check_csrf(request, x_csrf_token)
    return _submit(_session(request), person_id, body.otp)


# ---- the same interrupt as a page ----


@router.get("/portal-session", response_class=HTMLResponse)
def portal_session_page(request: Request, error: str | None = None,
                        person_id: str = Depends(require_person)) -> HTMLResponse:
    session = _session(request)
    return render(request, "portal_session.html", {
        "state": session.state.value, "is_owner": session.owner in (None, person_id),
        "has_screen": bool(session.otp_screen) and session.owner == person_id, "error": error,
    })


@router.post("/portal-session/start", response_model=None)
def portal_session_start(request: Request, csrf_token: str = Form(""),
                         person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    try:
        _start(_session(request), person_id)
    except PortalError as exc:
        return RedirectResponse(f"/portal-session?error={exc.code}", status_code=303)
    return RedirectResponse("/portal-session", status_code=303)


@router.post("/portal-session/otp", response_model=None)
def portal_session_otp(request: Request, otp: str = Form(...), csrf_token: str = Form(""),
                       person_id: str = Depends(require_person)) -> Response:
    check_csrf(request, csrf_token)
    try:
        _submit(_session(request), person_id, otp)
    except PortalError as exc:
        return RedirectResponse(f"/portal-session?error={exc.code}", status_code=303)
    return RedirectResponse("/portal-session", status_code=303)
