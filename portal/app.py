"""Synthetic TPA portal app factory.

Run:  uvicorn portal.app:app --port 8001 --reload
(Single worker only: portal state is held in memory.)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from portal.config import PortalSettings, load_portal_settings
from portal.routes import render, router
from portal.security import NotAuthenticated, wants_html
from portal.store import PortalStore
from shared.assets import asset_version
from shared.errors import AppError, error_body, install_error_handlers
from shared.log import RequestLogMiddleware, configure_logging

HERE = Path(__file__).resolve().parent
logger = logging.getLogger("portal")


def inr(amount: int | None) -> str:
    """Format rupees with Indian digit grouping: 120000 -> 'Rs 1,20,000'."""
    if amount is None:
        return "-"
    digits = str(int(amount))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "Rs " + ",".join(groups + [tail]) if groups else f"Rs {tail}"


def ist(value: datetime | None) -> str:
    """Display format for portal timestamps, e.g. '21-Oct-2026 14:05 IST'."""
    return value.strftime("%d-%b-%Y %H:%M IST") if value else "-"


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    """Build the portal app with fresh seed data."""
    settings = settings or load_portal_settings()
    configure_logging("portal", settings.log_level)
    store = PortalStore(upload_dir=settings.upload_dir)
    store.reset()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("portal started", extra={"data_dir": str(settings.data_dir), "claims": len(store.claims)})
        yield

    app = FastAPI(title="ClaimSaathi Synthetic TPA Portal", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["inr"] = inr
    templates.env.filters["ist"] = ist
    templates.env.globals["asset_v"] = asset_version(HERE / "static")
    app.state.templates = templates

    # Order matters: the last-added middleware runs first, so request ids exist before sessions.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="portal_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.https_only_cookies,
    )
    app.add_middleware(RequestLogMiddleware)
    install_error_handlers(app)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        """Browsers get a readable error page; other clients get the JSON envelope."""
        if wants_html(request):
            return render(request, "error.html", {"code": exc.code, "message": exc.message}, exc.status_code)
        return JSONResponse(error_body(request, exc.code, exc.message, exc.details), exc.status_code)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if wants_html(request):
            return RedirectResponse(f"/login?next={quote(exc.next_path)}", status_code=303)
        return JSONResponse(error_body(request, "unauthorized", "Sign in required."), 401)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
    return app


app = create_app()
