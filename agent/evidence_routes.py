"""Evidence viewer and test results.

Evidence (screenshots, saved HTML, extracted fields) is shown only to the person whose run produced
it. Files are resolved from the evidence id through the database, never from a path in the URL, and
saved HTML is served as plain text (never rendered in this origin) with its SHA-256 re-checked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from agent import ids
from agent import repository as repo
from agent.auth import render, require_person
from shared.errors import AppError

router = APIRouter()


def _owned(request: Request, evidence_id: str, person_id: str) -> repo.Evidence:
    with request.app.state.db.read() as conn:
        try:
            evidence = repo.get_evidence(conn, evidence_id)
            run = repo.get_run(conn, evidence.run_id)
        except repo.NotFound:
            evidence, run = None, None
    if evidence is None or run is None or run.requester_id != person_id:
        raise AppError("not_found", "No such evidence for you.", 404)
    return evidence


def _file(request: Request, rel: str | None) -> Path | None:
    if not rel:
        return None
    base = Path(request.app.state.settings.evidence_dir).resolve()
    try:
        target = (base / repo.safe_relative_path(rel)).resolve()
    except repo.ValidationError:
        return None
    return target if base in target.parents and target.is_file() else None


def evidence_json(request: Request, evidence: repo.Evidence) -> dict[str, Any]:
    extracted = evidence.extracted if isinstance(evidence.extracted, dict) else {}
    html_rel = extracted.get("html_file")
    html_path = _file(request, html_rel)
    html_ok = None
    if html_path is not None and evidence.html_sha256:
        html_ok = hashlib.sha256(html_path.read_bytes()).hexdigest() == evidence.html_sha256
    shown = {k: v for k, v in extracted.items() if k not in ("visible_text", "html_file")}
    return {
        "id": evidence.id, "run_id": evidence.run_id, "kind": evidence.kind, "url": evidence.url,
        "fetched_at": evidence.fetched_at, "html_sha256": evidence.html_sha256, "html_hash_matches": html_ok,
        "screenshot_url": f"/evidence/{evidence.id}/screenshot" if _file(request, evidence.screenshot_path) else None,
        "html_url": f"/evidence/{evidence.id}/html" if html_path else None,
        "extraction_source": evidence.extraction_source, "extracted": shown,
        "visible_text_chars": len(extracted.get("visible_text") or ""),
    }


@router.get("/api/evidence/{evidence_id}")
def evidence_api(request: Request, evidence_id: ids.EvidenceId, person_id: str = Depends(require_person)) -> dict:
    """One evidence record: what was captured, when, its HTML SHA-256 (re-checked) and file links."""
    return evidence_json(request, _owned(request, evidence_id, person_id))


@router.get("/evidence/{evidence_id}", response_class=HTMLResponse)
def evidence_page(request: Request, evidence_id: ids.EvidenceId, person_id: str = Depends(require_person)) -> HTMLResponse:
    data = evidence_json(request, _owned(request, evidence_id, person_id))
    return render(request, "evidence.html", {"ev": data})


@router.get("/evidence/{evidence_id}/screenshot")
def evidence_screenshot(request: Request, evidence_id: ids.EvidenceId, person_id: str = Depends(require_person)) -> Response:
    path = _file(request, _owned(request, evidence_id, person_id).screenshot_path)
    if path is None:
        raise AppError("not_found", "This evidence has no screenshot.", 404)
    return Response(path.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.get("/evidence/{evidence_id}/html")
def evidence_html(request: Request, evidence_id: ids.EvidenceId, person_id: str = Depends(require_person)) -> Response:
    """The saved page source, as plain text, with the stored SHA-256 alongside."""
    evidence = _owned(request, evidence_id, person_id)
    extracted = evidence.extracted if isinstance(evidence.extracted, dict) else {}
    path = _file(request, extracted.get("html_file"))
    if path is None:
        raise AppError("not_found", "This evidence has no saved HTML.", 404)
    data = path.read_bytes()
    return Response(data, media_type="text/plain; charset=utf-8", headers={
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'",
        "X-Evidence-SHA256": evidence.html_sha256 or "", "X-Computed-SHA256": hashlib.sha256(data).hexdigest(),
    })


# ---------------- test results ----------------


OFFICIAL = ("TC1", "TC2", "TC3", "TC4")


def _test_runs(request: Request) -> dict[str, Any]:
    with request.app.state.db.read() as conn:
        rows = repo.test_runs(conn)
    recorded = {r["test_case"] for r in rows}
    return {"runs": rows, "official_without_runs": [c for c in OFFICIAL if c not in recorded],
            "passed": sum(1 for r in rows if r["verdict"] == "PASS"),
            "failed": sum(1 for r in rows if r["verdict"] == "FAIL"),
            "unverified": sum(1 for r in rows if r["verdict"] is None)}


@router.get("/api/test-runs")
def test_runs_api(request: Request, _person: str = Depends(require_person)) -> dict:
    """Runs tagged with a test case (from the runs table) and their recorded verdicts. A run with no
    recorded verdict is reported as such, never as passed."""
    return _test_runs(request)


@router.get("/test-runs", response_class=HTMLResponse)
def test_runs_page(request: Request, person_id: str = Depends(require_person)) -> HTMLResponse:
    return render(request, "test_runs.html", {**_test_runs(request), "me": person_id})
