"""Agent web app: landing page, health checks, person sign-in and consent pages.

Run:  uvicorn agent.server:app --port 8000 --reload
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from agent import action_routes, auth, consent_routes, evidence_routes, portal_routes, vault_routes, workflow_routes
from agent import consent as consent_service
from agent.actions import ActionError
from agent.graph import WorkflowError
from agent import repository as repo
from agent.config import Settings, get_settings
from agent.db import Database
from agent.db import health as db_health
from agent.portal_client import PortalError, PortalSession, build_portal_client, probe_portal
from agent.schemas import DependencyCheck, HealthResponse, ReadinessResponse
from agent.seed import init_database
from shared.assets import asset_version
from shared.errors import AppError, error_body, install_error_handlers
from shared.log import RequestLogMiddleware, configure_logging
from shared.websec import NotAuthenticated, OtpStore, wants_html

VERSION = "0.1.0"
IST = timezone(timedelta(hours=5, minutes=30))


def format_ist(value: str | None) -> str:
    """'2026-10-22T08:30:00Z' or an ISO time with an offset -> '22 Oct 2026, 14:00 IST'."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(IST).strftime("%d %b %Y, %H:%M IST")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger("agent")

# Domain errors -> (HTTP status, stable code). Messages are the exception text.
_ERROR_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (consent_service.ConsentPermissionError, 403, "forbidden"),
    (consent_service.UnknownRequester, 403, "unknown_requester"),
    (consent_service.UnknownClaim, 404, "unknown_claim"),
    (consent_service.RequestNotPending, 409, "request_not_pending"),
    (consent_service.ConsentDenied, 403, "consent_denied"),
    (repo.NotFound, 404, "not_found"),
    (repo.ValidationError, 400, "validation_error"),
    (repo.InvalidTransition, 409, "invalid_transition"),
    (repo.ConstraintViolation, 409, "constraint_violation"),
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the agent app. Startup migrates the database and adds missing seed rows; no model calls."""
    settings = settings or get_settings()
    configure_logging("agent", settings.log_level)
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_database(database, settings.vault_dir)
        logger.info(
            "agent started",
            extra={
                "database": str(settings.database_path),
                "portal_base_url": settings.portal_base_url,
                "browser": settings.browser,
                "gemini_configured": settings.gemini_configured,
            },
        )
        yield
        if app.state.workflow is not None:
            app.state.workflow.close()
        app.state.portal_session.close()

    app = FastAPI(title="ClaimSaathi Agent", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.otp_store = OtpStore()
    app.state.workflow = None  # built on first use (workflow_routes.workflow)
    app.state.portal_session = PortalSession(lambda: build_portal_client(settings, database))
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters["ist"] = format_ist
    templates.env.globals["asset_v"] = asset_version(WEB_DIR / "static")
    app.state.templates = templates

    # The last-added middleware runs first, so request ids exist before sessions.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="claimsaathi_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.https_only_cookies,
    )
    app.add_middleware(RequestLogMiddleware)
    install_error_handlers(app)

    def error_response(request: Request, code: str, message: str, status: int) -> Response:
        if wants_html(request):
            return auth.render(request, "error.html", {"code": code, "message": message}, status)
        return JSONResponse(error_body(request, code, message), status)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        return error_response(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if wants_html(request):
            return RedirectResponse(f"/auth/login?next={quote(exc.next_path)}", status_code=303)
        return JSONResponse(error_body(request, "unauthorized", "Sign in required."), 401)

    @app.exception_handler(ActionError)
    async def _action_error(request: Request, exc: ActionError) -> Response:
        return error_response(request, exc.code, str(exc), exc.http_status)

    @app.exception_handler(WorkflowError)
    async def _workflow_error(request: Request, exc: WorkflowError) -> Response:
        return error_response(request, exc.code, str(exc), exc.http_status)

    @app.exception_handler(PortalError)
    async def _portal_error(request: Request, exc: PortalError) -> Response:
        if wants_html(request):
            return auth.render(request, "error.html", {"code": exc.code, "message": exc.message}, exc.http_status)
        body = error_body(request, exc.code, exc.message, {"blocked": exc.blocked})
        return JSONResponse(body, exc.http_status)

    for exc_type, status, code in _ERROR_MAP:
        async def _domain_error(request: Request, exc: Exception, _status: int = status, _code: str = code) -> Response:
            return error_response(request, _code, str(exc), _status)
        app.add_exception_handler(exc_type, _domain_error)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(consent_routes.router)
    app.include_router(portal_routes.router)
    app.include_router(vault_routes.router)
    app.include_router(action_routes.router)
    app.include_router(workflow_routes.router)
    app.include_router(evidence_routes.router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def landing(request: Request) -> HTMLResponse:
        return auth.render(request, "index.html", {"portal_url": settings.portal_base_url, "version": VERSION})

    @app.get("/walkthrough", response_class=HTMLResponse, include_in_schema=False)
    async def walkthrough(request: Request) -> HTMLResponse:
        return auth.render(request, "walkthrough.html")

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness probe; cheap and dependency-free."""
        return HealthResponse(status="ok", app="claimsaathi-agent", version=VERSION)

    @app.get("/health/ready", response_model=ReadinessResponse)
    def ready() -> ReadinessResponse:
        """Readiness probe: database schema/integrity healthy and portal reachable over HTTP."""
        result = db_health(database)
        db_check = DependencyCheck(ok=result["ok"], detail=result["detail"])
        portal_ok, portal_detail = probe_portal(settings.portal_base_url)
        return ReadinessResponse(
            status="ready" if db_check.ok and portal_ok else "degraded",
            database=db_check,
            portal=DependencyCheck(ok=portal_ok, detail=portal_detail),
            gemini_configured=settings.gemini_configured,
            browser=settings.browser,
        )

    return app


app = create_app()
