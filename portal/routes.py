"""Portal pages and form handlers.

Pages are server-rendered for humans and for the agent's browser; every value the
agent needs has a stable id or class (see templates). Clients that do not send
`Accept: text/html` get JSON responses in the shared error envelope instead.
"""

from __future__ import annotations

import asyncio
import hmac
import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from portal.config import PortalSettings
from portal.security import (
    SESSION_OTP,
    check_csrf,
    csrf_token as session_csrf,
    require_user,
    safe_next,
    start_session,
    wants_html,
)
from portal.seed import DEMO_USERS, MEMBERS_BY_ID, POLICY
from portal.store import FAULT_MODES, PortalStore, safe_display_name
from shared.errors import AppError

router = APIRouter()
CLAIM_ID = re.compile(r"^CLM-\d{4}-\d{4}$")
STATUS_SUGGESTIONS = (
    "Query Raised",
    "Under Review",
    "Under Process - Pending Final Authorisation",
    "Approved",
    "Partially Approved",
    "Settled",
    "Rejected",
    "Closed - Documents Not Received",
    "Sent to Medical Team",
)


def _store(request: Request) -> PortalStore:
    return request.app.state.store


def _settings(request: Request) -> PortalSettings:
    return request.app.state.settings


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    """Render a template with the common context (user, CSRF token, policy)."""
    ctx = {"user": request.session.get("user"), "csrf": session_csrf(request), "policy": POLICY}
    ctx.update(context or {})
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _claim_id(claim_id: str) -> str:
    if not CLAIM_ID.match(claim_id):
        raise AppError("claim_not_found", "Claim numbers look like CLM-2026-0142.", 404)
    return claim_id


# ---------------- public ----------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing(request: Request) -> HTMLResponse:
    """Public landing page."""
    return render(request, "landing.html")


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "app": "claimsaathi-portal"}


# ---------------- login + OTP ----------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None) -> HTMLResponse:
    """Username/password form."""
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/claims"),
) -> Response:
    """Check the password, then send the user to a separate OTP page."""
    expected = DEMO_USERS.get(username.strip().lower())
    if expected is None or not hmac.compare_digest(expected, password):
        return render(
            request, "login.html", {"next": safe_next(next), "error": "Incorrect username or password."}, 401
        )
    request.session.clear()
    request.session[SESSION_OTP] = _store(request).new_otp(username.strip().lower())
    request.session["next"] = safe_next(next)
    return RedirectResponse("/otp", status_code=303)


@router.get("/otp", response_model=None)
async def otp_page(request: Request) -> Response:
    """OTP form plus the simulated SMS panel a human reads the code from."""
    challenge = _store(request).get_otp(request.session.get(SESSION_OTP))
    if challenge is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "otp.html", {"challenge": challenge})


@router.post("/otp", response_model=None)
async def otp_submit(request: Request, otp: str = Form(...)) -> Response:
    """Verify the typed OTP and start the signed-in session."""
    store = _store(request)
    try:
        username = store.verify_otp(request.session.get(SESSION_OTP), otp)
    except AppError as exc:
        challenge = store.get_otp(request.session.get(SESSION_OTP))
        if challenge is None:
            request.session.pop(SESSION_OTP, None)
            return render(request, "login.html", {"next": "/claims", "error": exc.message}, 401)
        return render(request, "otp.html", {"challenge": challenge, "error": exc.message}, 401)
    next_path = safe_next(request.session.pop("next", None))
    start_session(request, username)
    return RedirectResponse(next_path, status_code=303)


@router.post("/otp/resend", response_model=None)
async def otp_resend(request: Request) -> Response:
    """Issue a fresh simulated OTP for the same pending login."""
    store = _store(request)
    challenge = store.get_otp(request.session.get(SESSION_OTP))
    if challenge is None:
        return RedirectResponse("/login", status_code=303)
    request.session[SESSION_OTP] = store.new_otp(challenge.username)
    return RedirectResponse("/otp", status_code=303)


@router.post("/logout", response_model=None)
async def logout(request: Request, csrf_token: str = Form("")) -> Response:
    """End the session."""
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------- claims ----------------


@router.get("/claims", response_class=HTMLResponse)
async def claims_list(request: Request, user: str = Depends(require_user)) -> HTMLResponse:
    """All claims on the policy."""
    claims = sorted(_store(request).claims.values(), key=lambda c: c.claim_id)
    return render(request, "claims.html", {"claims": claims, "members": MEMBERS_BY_ID})


@router.get("/claims/{claim_id}", response_class=HTMLResponse)
async def claim_detail(
    request: Request, claim_id: str, uploaded: str | None = None, user: str = Depends(require_user)
) -> HTMLResponse:
    """Claim page: status, pre-auth, requested and uploaded documents."""
    claim = _store(request).get_claim(_claim_id(claim_id))
    flash = next((u for u in claim.uploads if u.doc_id == uploaded), None) if uploaded else None
    return render(
        request,
        "claim.html",
        {
            "claim": claim,
            "patient": MEMBERS_BY_ID[claim.patient_id],
            "flash_upload": flash,
            "max_mb": _settings(request).max_upload_bytes // (1024 * 1024),
        },
    )


@router.post("/claims/{claim_id}/documents", response_model=None)
async def upload_document(
    request: Request,
    claim_id: str,
    requirement_id: str = Form(...),
    csrf_token: str = Form(""),
    file: UploadFile = File(...),
    user: str = Depends(require_user),
) -> Response:
    """Multipart upload of one synthetic file against one document request.

    Order: CSRF, validation (requirement, size, type, duplicate), then the demo fault, then store.
    """
    check_csrf(request, csrf_token)
    store, settings = _store(request), _settings(request)
    claim_id = _claim_id(claim_id)
    filename = safe_display_name(file.filename)
    data = await file.read(settings.max_upload_bytes + 1)
    try:
        sha, content_type, ext = store.validate_upload(
            claim_id, requirement_id, filename, data, settings.max_upload_bytes
        )
    except AppError as exc:
        store.log_upload(claim_id, requirement_id, filename, "", f"rejected: {exc.code}", "none")
        raise

    fault = store.take_fault()
    if fault == "http_500":
        store.log_upload(claim_id, requirement_id, filename, sha, "not stored (simulated HTTP 500)", fault)
        raise AppError("simulated_server_error", "Simulated server error (demo fault). File not stored.", 500)
    if fault == "timeout_before_store":
        await asyncio.sleep(settings.fault_delay_seconds)
        store.log_upload(claim_id, requirement_id, filename, sha, "not stored (simulated timeout)", fault)
        raise AppError("simulated_timeout", "Simulated gateway timeout (demo fault). File not stored.", 504)

    doc = store.store_upload(claim_id, requirement_id, filename, data, sha, content_type, ext)
    if fault == "timeout_after_store":
        store.log_upload(claim_id, requirement_id, filename, sha, "stored; response delayed (simulated timeout)", fault)
        await asyncio.sleep(settings.fault_delay_seconds)
    else:
        store.log_upload(claim_id, requirement_id, filename, sha, "stored", fault)

    if wants_html(request):
        return RedirectResponse(f"/claims/{claim_id}?uploaded={doc.doc_id}", status_code=303)
    return JSONResponse(
        {
            "uploaded": {
                "doc_id": doc.doc_id,
                "claim_id": claim_id,
                "requirement_id": requirement_id,
                "filename": doc.filename,
                "size": doc.size,
                "sha256": doc.sha256,
                "uploaded_at": doc.uploaded_at.isoformat(),
            }
        },
        status_code=201,
    )


# ---------------- demo admin ----------------


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, done: str | None = None, user: str = Depends(require_user)) -> HTMLResponse:
    """Demo control panel: reset, status changes, upload faults, upload log."""
    store = _store(request)
    return render(
        request,
        "admin.html",
        {
            "claims": sorted(store.claims.values(), key=lambda c: c.claim_id),
            "fault_mode": store.fault_mode,
            "fault_once": store.fault_once,
            "fault_modes": FAULT_MODES,
            "fault_delay": _settings(request).fault_delay_seconds,
            "upload_log": list(reversed(store.upload_log)),
            "suggestions": STATUS_SUGGESTIONS,
            "done": done,
        },
    )


def _admin_done(request: Request, message: str) -> Response:
    if wants_html(request):
        return RedirectResponse(f"/admin?done={message}", status_code=303)
    return JSONResponse({"ok": True, "done": message})


@router.post("/admin/reset", response_model=None)
async def admin_reset(request: Request, csrf_token: str = Form(""), user: str = Depends(require_user)) -> Response:
    """Restore the seed data and clear uploads, faults and the log."""
    check_csrf(request, csrf_token)
    _store(request).reset()
    return _admin_done(request, "reset")


@router.post("/admin/status", response_model=None)
async def admin_status(
    request: Request,
    claim_id: str = Form(...),
    raw_status: str = Form(...),
    csrf_token: str = Form(""),
    user: str = Depends(require_user),
) -> Response:
    """Change a claim's raw status text (any text, to test unknown statuses)."""
    check_csrf(request, csrf_token)
    _store(request).set_status(_claim_id(claim_id), raw_status)
    return _admin_done(request, "status")


@router.post("/admin/fault", response_model=None)
async def admin_fault(
    request: Request,
    mode: str = Form(...),
    once: str | None = Form(None),
    csrf_token: str = Form(""),
    user: str = Depends(require_user),
) -> Response:
    """Select the upload fault mode, optionally for the next upload only."""
    check_csrf(request, csrf_token)
    _store(request).set_fault(mode, once is not None)
    return _admin_done(request, "fault")
