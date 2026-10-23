"""Deterministic consent gate (TC2) and the patient-facing consent request lifecycle.

Rules
- Only stored consent artefacts grant access. No model output, prompt text or request
  field can grant, infer or widen consent.
- The patient behind a claim comes from the trusted claim registry, never from the caller.
- Every check is recorded as a `consent` decision, linked to a `consent_record` evidence
  snapshot of exactly what was evaluated.
- Protected work (portal reads, upload approval, upload execution) goes through the
  `guarded_*` / `require_consent` helpers, which re-check consent immediately before acting.
  The database trigger `trg_evidence_requires_consent` is a backstop for portal evidence.
- Only the patient, identified by their signed-in session, can approve or deny a request.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from agent import repository as repo
from agent.db import Database, parse_utc, to_utc_iso, utc_now
from agent.models import ConsentAction, ConsentRequestStatus, ConsentResult, RunState

logger = logging.getLogger("agent.consent")

T = TypeVar("T")
REQUEST_TTL = timedelta(hours=24)
ALLOWED_DURATIONS_DAYS = (1, 7, 30)

# When several consents exist but none allows the action, report the most specific reason.
_RANK = {
    ConsentResult.ALLOW: 0,
    ConsentResult.ACTION_NOT_ALLOWED: 1,
    ConsentResult.EXPIRED_OR_REVOKED: 2,
    ConsentResult.OUT_OF_SCOPE: 3,
}


# ---------------- errors ----------------


class ConsentError(Exception):
    """Base class for consent errors."""


class UnknownRequester(ConsentError):
    """The requester is not a known person; nothing is checked or recorded for them."""


class UnknownClaim(ConsentError):
    """The claim is not in the trusted registry, so its patient cannot be identified."""


class ConsentPermissionError(ConsentError):
    """The signed-in person may not decide or revoke this consent."""


class RequestNotPending(ConsentError):
    """The consent request was already decided or has expired."""


class ConsentDenied(ConsentError):
    """A protected action was attempted without an ALLOW decision."""

    def __init__(self, decision: ConsentDecision) -> None:
        super().__init__(f"{decision.result.value}: {decision.reason}")
        self.decision = decision


# ---------------- decisions ----------------


@dataclass(frozen=True)
class ConsentDecision:
    """The recorded outcome of one consent check."""

    result: ConsentResult
    reason: str
    run_id: str
    decision_id: str
    evidence_id: str
    requester_id: str
    claim_id: str
    action: str
    patient_id: str | None
    consent_id: str | None

    @property
    def allowed(self) -> bool:
        return self.result is ConsentResult.ALLOW

    def public(self) -> dict[str, Any]:
        """Fields safe to return to the requester (no other people's consent details)."""
        return {
            "result": self.result.value,
            "reason": self.reason,
            "claim_id": self.claim_id,
            "action": self.action,
            "run_id": self.run_id,
            "decision_id": self.decision_id,
        }


def _evaluate(consent: repo.Consent, claim: repo.ClaimRecord, action: str, at: str) -> tuple[ConsentResult, str]:
    """Judge one consent artefact against one claim and action at time `at`."""
    if consent.policy_no != claim.policy_no:
        return ConsentResult.OUT_OF_SCOPE, f"{consent.id} is for policy {consent.policy_no}, not {claim.policy_no}"
    if consent.claim_id not in ("*", claim.claim_id):
        return ConsentResult.OUT_OF_SCOPE, f"{consent.id} covers {consent.claim_id} only, not {claim.claim_id}"
    if consent.revoked:
        return ConsentResult.EXPIRED_OR_REVOKED, f"{consent.id} was revoked at {consent.revoked_at}"
    if at < consent.granted_at:
        return ConsentResult.EXPIRED_OR_REVOKED, f"{consent.id} is not valid until {consent.granted_at}"
    if at >= consent.expires_at:
        return ConsentResult.EXPIRED_OR_REVOKED, f"{consent.id} expired at {consent.expires_at}"
    if action not in consent.actions:
        allowed = ", ".join(consent.actions)
        return ConsentResult.ACTION_NOT_ALLOWED, f"{consent.id} allows {allowed}, not {action}"
    scope = "all of the patient's claims on this policy" if consent.claim_id == "*" else consent.claim_id
    return ConsentResult.ALLOW, f"{consent.id} allows {action} on {scope} until {consent.expires_at}"


def _check(conn: sqlite3.Connection, requester_id: str, claim_id: str, action: str, *,
           run_id: str | None, at: str) -> ConsentDecision:
    """Evaluate and record one consent check inside the caller's transaction."""
    if action not in {a.value for a in ConsentAction}:
        raise repo.ValidationError(f"unknown action {action!r}")
    try:
        repo.get_person(conn, requester_id)
    except repo.NotFound as exc:
        raise UnknownRequester(f"unknown requester {requester_id!r}") from exc

    created_run = run_id is None
    if created_run:
        run = repo.create_run(conn, requester_id=requester_id, claim_id=claim_id)
        repo.update_run(conn, run.id, state=RunState.RUNNING.value)
    else:
        run = repo.get_run(conn, run_id)
        if run.requester_id != requester_id or run.claim_id != claim_id:
            raise ConsentError(f"run {run_id} belongs to a different requester or claim")

    try:
        claim: repo.ClaimRecord | None = repo.get_claim_record(conn, claim_id)
    except repo.NotFound:
        claim = None

    considered: list[dict[str, Any]] = []
    chosen: repo.Consent | None = None
    if claim is None:
        result, reason = ConsentResult.OUT_OF_SCOPE, f"{claim_id} is not in the trusted claim registry"
    elif claim.patient_id == requester_id:
        result, reason = ConsentResult.ALLOW, "the requester is the patient on this claim"
    else:
        consents = repo.consents_between(conn, requester_id, claim.patient_id)
        if not consents:
            result, reason = ConsentResult.NEEDS_CONSENT, f"no consent from {claim.patient_id} to {requester_id}"
        else:
            best: tuple[ConsentResult, str, repo.Consent] | None = None
            for consent in consents:
                outcome, why = _evaluate(consent, claim, action, at)
                considered.append({
                    "id": consent.id, "claim_id": consent.claim_id, "policy_no": consent.policy_no,
                    "actions": list(consent.actions), "granted_at": consent.granted_at,
                    "expires_at": consent.expires_at, "revoked": consent.revoked, "outcome": outcome.value,
                })
                if best is None or _RANK[outcome] < _RANK[best[0]]:
                    best = (outcome, why, consent)
            assert best is not None
            result, reason, chosen = best[0], best[1], best[2]

    snapshot = {
        "requester_id": requester_id,
        "claim": None if claim is None else {
            "claim_id": claim.claim_id, "policy_no": claim.policy_no, "patient_id": claim.patient_id,
            "source": claim.source,
        },
        "action": action,
        "evaluated_at": at,
        "consents": considered,
    }
    evidence = repo.add_evidence(
        conn, run_id=run.id, kind="consent_record", url=f"db://consents/{requester_id}/{claim_id}",
        extracted=snapshot, extraction_source="rule",
    )
    decision = repo.record_decision(
        conn, run_id=run.id, kind="consent", result=result.value, reason=reason, evidence_id=evidence.id,
        subject_ref=f"{claim_id}:{action}",
        input_refs={"requester_id": requester_id, "claim_id": claim_id, "action": action,
                    "patient_id": claim.patient_id if claim else None,
                    "consent_id": chosen.id if chosen else None},
    )
    if created_run and result is not ConsentResult.ALLOW:
        repo.update_run(conn, run.id, state=RunState.BLOCKED.value)
    logger.info("consent checked", extra={"run_id": run.id, "claim_id": claim_id, "action": action,
                                          "result": result.value})
    return ConsentDecision(
        result=result, reason=reason, run_id=run.id, decision_id=decision.id, evidence_id=evidence.id,
        requester_id=requester_id, claim_id=claim_id, action=action,
        patient_id=claim.patient_id if claim else None, consent_id=chosen.id if chosen else None,
    )


def check_consent(db: Database, requester_id: str, claim_id: str, action: str, *,
                  run_id: str | None = None, at: str | None = None) -> ConsentDecision:
    """Check and record whether `requester_id` may perform `action` on `claim_id`.

    With no `run_id`, a run is started for the check; a denied check marks it `blocked`.
    """
    with db.transaction() as conn:
        return _check(conn, requester_id, claim_id, action, run_id=run_id, at=at or utc_now())


def require_consent(db: Database, requester_id: str, claim_id: str, action: str, *,
                    run_id: str | None = None, at: str | None = None) -> ConsentDecision:
    """Like `check_consent`, but raise ConsentDenied unless the result is ALLOW."""
    decision = check_consent(db, requester_id, claim_id, action, run_id=run_id, at=at)
    if not decision.allowed:
        raise ConsentDenied(decision)
    return decision


# ---------------- guarded protected actions ----------------


def start_run(db: Database, requester_id: str, claim_id: str, *, test_case: str | None = None) -> repo.Run:
    """Start a run for a known requester (state `running`)."""
    with db.transaction() as conn:
        try:
            repo.get_person(conn, requester_id)
        except repo.NotFound as exc:
            raise UnknownRequester(f"unknown requester {requester_id!r}") from exc
        run = repo.create_run(conn, requester_id=requester_id, claim_id=claim_id, test_case=test_case)
        return repo.update_run(conn, run.id, state=RunState.RUNNING.value)


def guarded_portal_read(db: Database, requester_id: str, claim_id: str, *, run_id: str,
                        reader: Callable[[str, str], T], at: str | None = None) -> T:
    """Re-check read_status consent, then call `reader(run_id, claim_id)`. The reader is never
    called (and so never touches the portal) unless the check returns ALLOW."""
    require_consent(db, requester_id, claim_id, ConsentAction.READ_STATUS.value, run_id=run_id, at=at)
    return reader(run_id, claim_id)


def guarded_upload_change(db: Database, action_id: str, person_id: str,
                          apply: Callable[[sqlite3.Connection, repo.Action, ConsentDecision], T], *,
                          at: str | None = None) -> T:
    """Re-check upload_docs consent and apply an action change in the same write transaction.

    `apply(conn, action, decision)` runs only on ALLOW, with the action as read inside the
    transaction; a denial is committed as a decision and raised as ConsentDenied."""
    with db.transaction() as conn:
        action = repo.get_action(conn, action_id)
        decision = _check(conn, person_id, action.claim_id, ConsentAction.UPLOAD_DOCS.value,
                          run_id=action.run_id, at=at or utc_now())
        if decision.allowed:
            return apply(conn, action, decision)
    raise ConsentDenied(decision)  # the denied decision is committed above


def _guarded_action(db: Database, action_id: str, person_id: str, at: str | None,
                    apply: Callable[[sqlite3.Connection, repo.Action], repo.Action]) -> repo.Action:
    """Re-check upload_docs consent and apply an action change in the same transaction."""
    return guarded_upload_change(db, action_id, person_id, lambda conn, action, _: apply(conn, action), at=at)


def guarded_approve_upload(db: Database, action_id: str, *, approved_by: str, at: str | None = None) -> repo.Action:
    """Human approval of a proposed upload, allowed only while upload consent holds."""
    return _guarded_action(db, action_id, approved_by, at,
                           lambda conn, a: repo.approve_action(conn, a.id, approved_by=approved_by))


def guarded_start_upload(db: Database, action_id: str, *, requester_id: str, at: str | None = None) -> repo.Action:
    """Move an approved upload to EXECUTING, re-checking consent immediately before."""
    return _guarded_action(db, action_id, requester_id, at,
                           lambda conn, a: repo.transition_action(conn, a.id, "EXECUTING"))


# ---------------- consent requests ----------------


def _plus(at: str, delta: timedelta) -> str:
    return to_utc_iso(parse_utc(at) + delta)


def _expire_if_stale(db: Database, request_id: str, at: str) -> repo.ConsentRequest:
    """Mark a pending request expired once its review window has passed."""
    with db.transaction() as conn:
        request = repo.get_consent_request(conn, request_id)
        if request.status == ConsentRequestStatus.PENDING.value and at >= request.expires_at:
            request = repo.close_consent_request(conn, request_id, status="expired", at=at)
        return request


def request_consent(db: Database, requester_id: str, claim_id: str, actions: list[str], *,
                    run_id: str | None = None, at: str | None = None) -> repo.ConsentRequest:
    """Create (or reuse) a pending request asking the claim's patient for consent.

    One pending request exists per requester, patient and claim; asking again for more
    actions widens it. The patient comes from the trusted registry.
    """
    at = at or utc_now()
    with db.transaction() as conn:
        try:
            repo.get_person(conn, requester_id)
        except repo.NotFound as exc:
            raise UnknownRequester(f"unknown requester {requester_id!r}") from exc
        try:
            claim = repo.get_claim_record(conn, claim_id)
        except repo.NotFound as exc:
            raise UnknownClaim(f"{claim_id} is not in the trusted claim registry") from exc
        if claim.patient_id == requester_id:
            raise ConsentError("patients do not need consent for their own claims")
        existing = repo.pending_consent_request(conn, requester_id, claim.patient_id, claim_id)
        if existing is not None and at >= existing.expires_at:
            repo.close_consent_request(conn, existing.id, status="expired", at=at)
            existing = None
        if existing is not None:
            wanted = set(existing.actions) | set(actions)
            if wanted == set(existing.actions):
                return existing
            return repo.set_consent_request_actions(conn, existing.id, sorted(wanted))
        return repo.create_consent_request(
            conn, requester_id=requester_id, patient_id=claim.patient_id, policy_no=claim.policy_no,
            claim_id=claim_id, actions=actions, run_id=run_id, created_at=at, expires_at=_plus(at, REQUEST_TTL),
        )


def view_request(db: Database, request_id: str, *, viewer_id: str, at: str | None = None) -> repo.ConsentRequest:
    """A request as seen by its patient or requester (anyone else is refused)."""
    request = _expire_if_stale(db, request_id, at or utc_now())
    if viewer_id not in (request.patient_id, request.requester_id):
        raise ConsentPermissionError("only the patient or the requester can view this consent request")
    return request


def approve_request(db: Database, request_id: str, *, patient_session_id: str, actions: list[str] | None = None,
                    duration_days: int = 7, all_claims: bool = False, at: str | None = None) -> repo.Consent:
    """Grant consent from a pending request.

    `patient_session_id` must be the person signed in through the patient OTP flow; it must
    equal the request's patient. The patient may grant fewer actions than requested, and
    widens scope to all their claims on the policy only by setting `all_claims` explicitly.
    """
    at = at or utc_now()
    request = _expire_if_stale(db, request_id, at)
    if request.status != ConsentRequestStatus.PENDING.value:
        raise RequestNotPending(f"consent request {request_id} is {request.status}")
    if patient_session_id != request.patient_id:
        raise ConsentPermissionError("only the patient named in the request can approve it")
    granted = sorted(set(actions if actions is not None else request.actions))
    if not granted:
        raise repo.ValidationError("choose at least one action to allow")
    if not set(granted) <= set(request.actions):
        raise repo.ValidationError("cannot grant actions that were not requested")
    if duration_days not in ALLOWED_DURATIONS_DAYS:
        raise repo.ValidationError(f"duration must be one of {ALLOWED_DURATIONS_DAYS} days")
    with db.transaction() as conn:
        consent = repo.create_consent(
            conn, patient_id=request.patient_id, grantee_id=request.requester_id, policy_no=request.policy_no,
            claim_id="*" if all_claims else request.claim_id, actions=granted, granted_at=at,
            expires_at=_plus(at, timedelta(days=duration_days)),
        )
        repo.close_consent_request(conn, request_id, status="approved", at=at,
                                   decided_by=patient_session_id, consent_id=consent.id)
    logger.info("consent request approved", extra={"request_id": request_id, "consent_id": consent.id})
    return consent


def deny_request(db: Database, request_id: str, *, patient_session_id: str,
                 at: str | None = None) -> repo.ConsentRequest:
    """Refuse a pending request. Only the patient can."""
    at = at or utc_now()
    request = _expire_if_stale(db, request_id, at)
    if request.status != ConsentRequestStatus.PENDING.value:
        raise RequestNotPending(f"consent request {request_id} is {request.status}")
    if patient_session_id != request.patient_id:
        raise ConsentPermissionError("only the patient named in the request can deny it")
    with db.transaction() as conn:
        return repo.close_consent_request(conn, request_id, status="denied", at=at, decided_by=patient_session_id)


def revoke(db: Database, consent_id: str, *, by_person_id: str, at: str | None = None) -> repo.Consent:
    """Revoke a consent. The patient can revoke; the grantee can give up access."""
    with db.transaction() as conn:
        consent = repo.get_consent(conn, consent_id)
        if by_person_id not in (consent.patient_id, consent.grantee_id):
            raise ConsentPermissionError("only the patient or the grantee can revoke this consent")
        return repo.revoke_consent(conn, consent_id, at=at or utc_now())


def overview(db: Database, person_id: str, *, at: str | None = None) -> dict[str, Any]:
    """Everything a signed-in person can review: requests and consents in both directions."""
    at = at or utc_now()
    with db.read() as conn:
        incoming = repo.consent_requests_for(conn, patient_id=person_id)
        outgoing = repo.consent_requests_for(conn, requester_id=person_id)
    for req in incoming + outgoing:
        if req.status == ConsentRequestStatus.PENDING.value and at >= req.expires_at:
            _expire_if_stale(db, req.id, at)
    with db.read() as conn:
        return {
            "incoming_requests": repo.consent_requests_for(conn, patient_id=person_id),
            "outgoing_requests": repo.consent_requests_for(conn, requester_id=person_id),
            "given": repo.consents_for(conn, patient_id=person_id),
            "received": repo.consents_for(conn, grantee_id=person_id),
            "people": {p.id: p for p in repo.list_people(conn)},
            "now": at,
        }
