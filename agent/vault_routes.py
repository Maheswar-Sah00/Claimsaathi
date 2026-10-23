"""Family document vault pages and the document-matching API.

Files are always addressed by their registered vault id; no route accepts a filesystem path.
A person only ever sees and selects their own files.
"""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from agent import ids
from agent import repository as repo
from agent.auth import render, require_person
from agent.matcher import MatcherError, match_claim, vault_bytes
from shared.errors import AppError
from shared.websec import check_csrf

router = APIRouter()


@router.get("/vault", response_class=HTMLResponse)
def vault_page(request: Request, person_id: str = Depends(require_person)) -> HTMLResponse:
    """The signed-in person's registered documents and what is known about each."""
    with request.app.state.db.read() as conn:
        files = repo.vault_files_for(conn, person_id)
    return render(request, "vault.html", {"files": files})


@router.get("/vault/files/{vault_file_id}")
def vault_file(request: Request, vault_file_id: ids.VaultFileId, person_id: str = Depends(require_person)) -> Response:
    """View one of your own registered files (integrity-checked)."""
    with request.app.state.db.read() as conn:
        try:
            vf = repo.get_vault_file(conn, vault_file_id)
        except repo.NotFound:
            vf = None
    if vf is None or vf.owner_id != person_id:
        raise AppError("not_found", "No such document in your vault.", 404)
    data, problems = vault_bytes(request.app.state.settings.vault_dir, vf)
    if data is None:
        raise AppError("vault_file_unavailable", "; ".join(problems), 409)
    return Response(data, media_type=vf.content_type, headers={
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(vf.original_name)}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


class MatchBody(BaseModel):
    """Optional manual choice of file per request, by vault id only."""

    selections: dict[str, str] = Field(default_factory=dict)


@router.post("/api/runs/{run_id}/match")
def run_match(request: Request, run_id: ids.RunId, body: MatchBody, x_csrf_token: str | None = Header(default=None),
              person_id: str = Depends(require_person)) -> dict:
    """Match the claim read in this run against the requester's vault and record each decision."""
    check_csrf(request, x_csrf_token)
    settings = request.app.state.settings
    try:
        report = match_claim(
            request.app.state.db, run_id=run_id, requester_id=person_id, vault_dir=settings.vault_dir,
            selections=body.selections, pre_admission_days=settings.policy_pre_admission_days,
            post_admission_days=settings.policy_post_admission_days,
        )
    except MatcherError as exc:
        raise AppError("cannot_match", str(exc), 409) from exc
    items = []
    for item in report.items:
        data = asdict(item)
        data["result"] = item.result.value
        data["eligible_for_upload"] = item.eligible_for_upload
        data["candidates"] = [{k: v for k, v in c.items() if k != "evidence_id"} for c in data["candidates"]]
        items.append(data)
    return {"run_id": run_id, "claim_id": report.claim_id, "window": report.window, "items": items,
            "not_pending": report.not_pending}
