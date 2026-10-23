"""Sign-in for synthetic people (family members and patients) with a demo OTP.

The OTP is "sent" to the person's registered synthetic contact and shown in a
clearly labelled simulated SMS panel. The signed-in person id lives only in the
signed session; consent approval uses it and nothing the browser submits.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agent import repository as repo
from agent.db import Database
from shared.errors import AppError
from shared.websec import NotAuthenticated, OtpStore, check_csrf, rotate_csrf, safe_next
from shared.websec import csrf_token as session_csrf

SESSION_PERSON = "person_id"
SESSION_OTP = "otp_challenge"
SESSION_NEXT = "next"

router = APIRouter(prefix="/auth")


def require_person(request: Request) -> str:
    """FastAPI dependency: the signed-in person's id, or NotAuthenticated."""
    person = request.session.get(SESSION_PERSON)
    if not person:
        raise NotAuthenticated(request.url.path)
    return str(person)


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    """Render an agent template with the signed-in person and CSRF token."""
    db: Database = request.app.state.db
    person = None
    person_id = request.session.get(SESSION_PERSON)
    if person_id:
        with db.read() as conn:
            try:
                person = repo.get_person(conn, person_id)
            except repo.NotFound:
                request.session.clear()
    ctx = {"person": person, "csrf": session_csrf(request),
           "portal_url": getattr(request.app.state.settings, "portal_base_url", None)}
    ctx.update(context or {})
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _otp(request: Request) -> OtpStore:
    return request.app.state.otp_store


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None) -> HTMLResponse:
    """Choose a synthetic identity to sign in as."""
    with request.app.state.db.read() as conn:
        people = repo.list_people(conn)
    return render(request, "auth_login.html", {"people": people, "next": safe_next(next, "/consent")})


@router.post("/login", response_model=None)
async def login_submit(request: Request, person_id: str = Form(...), next: str = Form("/consent")) -> Response:
    """Send a demo OTP to the chosen person's synthetic contact."""
    with request.app.state.db.read() as conn:
        try:
            person = repo.get_person(conn, person_id)
        except repo.NotFound as exc:
            raise AppError("unknown_person", "No such synthetic person.", 400) from exc
    request.session.clear()
    request.session[SESSION_OTP] = _otp(request).new(person.id, person.contact_ref)
    request.session[SESSION_NEXT] = safe_next(next, "/consent")
    return RedirectResponse("/auth/otp", status_code=303)


@router.get("/otp", response_model=None)
async def otp_page(request: Request) -> Response:
    """OTP form plus the simulated SMS panel."""
    challenge = _otp(request).get(request.session.get(SESSION_OTP))
    if challenge is None:
        return RedirectResponse("/auth/login", status_code=303)
    with request.app.state.db.read() as conn:
        person = repo.get_person(conn, challenge.subject)
    return render(request, "auth_otp.html", {"challenge": challenge, "who": person})


@router.post("/otp", response_model=None)
async def otp_submit(request: Request, otp: str = Form(...)) -> Response:
    """Verify the OTP typed by a human and start the session."""
    store = _otp(request)
    try:
        person_id = store.verify(request.session.get(SESSION_OTP), otp)
    except AppError as exc:
        challenge = store.get(request.session.get(SESSION_OTP))
        if challenge is None:
            return RedirectResponse("/auth/login", status_code=303)
        with request.app.state.db.read() as conn:
            person = repo.get_person(conn, challenge.subject)
        return render(request, "auth_otp.html", {"challenge": challenge, "who": person, "error": exc.message}, 401)
    next_path = safe_next(request.session.pop(SESSION_NEXT, None), "/consent")
    request.session.pop(SESSION_OTP, None)
    request.session[SESSION_PERSON] = person_id
    rotate_csrf(request)
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout", response_model=None)
async def logout(request: Request, csrf_token: str = Form("")) -> Response:
    """End the session."""
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
