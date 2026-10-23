"""Human-approved, idempotent document uploads with independent read-back verification (TC1, TC4).

Lifecycle (`agent.models.ACTION_TRANSITIONS`)::

    PROPOSED --approve--> APPROVED --execute--> EXECUTING --> PENDING_VERIFY --re-read--> VERIFIED | FAILED
       \\--reject--> REJECTED                         \\--> UNKNOWN --re-read--> VERIFIED | RETRY_ALLOWED | FAILED
                                                                  RETRY_ALLOWED --execute--> EXECUTING ...

Rules
- Nothing is uploaded unless the requester, signed in, approved the exact card they were shown:
  patient, claim, requirement, file name, preview, SHA-256, matching evidence and destination.
  The approval is stored bound to that claim, requirement and file; SQLite refuses to re-point an
  action, reopen it, or move it to EXECUTING without that bound approval.
- One action per idempotency key (claim, requirement, file hash; unique in SQLite). Proposing,
  approving or executing again returns the existing action. A reconnecting client never resets an
  EXECUTING or UNKNOWN action.
- Immediately before every attempt, first or retry: upload and read consent, the approval binding,
  file registration and bytes, patient, document type and date, legibility, the requirement on a
  fresh claim-page read, and the hashes already on the claim. The move to EXECUTING happens in one
  write transaction with a final consent check, and opens the attempt row; SQLite allows one open
  attempt per action, so concurrent requests cannot both upload.
- The portal's answer is not proof. Every attempt ends with a fresh read of the uploaded-document
  list and status stamp: VERIFIED only when the same hash is listed under the same requirement.
  A timeout, a 5xx or an unconfirmed answer is UNKNOWN until that re-read decides; RETRY_ALLOWED
  only when it shows the file absent and every check passes again. A retry is a new, fully
  re-checked attempt started by a person, never a loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agent import consent as cs
from agent import repository as repo
from agent.db import Database, parse_utc
from agent.matcher import HOSPITAL_HELD, VaultMetadata, admission_window, is_pending, needed_type, propose_upload, vault_bytes
from agent.models import (
    TERMINAL_ACTION_STATES,
    ActionState,
    AttemptOutcome,
    CheckPhase,
    ConsentAction,
    VerificationResult,
)
from agent.portal_client import DocumentListResult, PortalError, ReadResult, UploadResult

logger = logging.getLogger("agent.actions")

MAX_ATTEMPTS = 3
STALE_GRACE_SECONDS = 60  # an EXECUTING attempt with no result this long after its deadline is treated as UNKNOWN
UPLOAD = ConsentAction.UPLOAD_DOCS.value
READ = ConsentAction.READ_STATUS.value

CHECK_LABELS = {
    "consent": "Consent", "portal_read": "Fresh portal read", "approval": "Your approval",
    "file_registered": "Registered file", "patient": "Patient", "legible": "Readable",
    "requirement": "Requirement", "document_type": "Document type", "document_date": "Document date",
    "not_on_claim": "Not already on the claim", "attempts": "Attempts",
}


# ---------------- errors ----------------


class ActionError(Exception):
    """Base class. `code` and `http_status` are what the web layer returns."""

    code = "action_error"
    http_status = 409


class NotYourAction(ActionError):
    """The action does not exist for this person (answered like a missing record)."""

    code, http_status = "not_found", 404


class ApprovalRequired(ActionError):
    code = "approval_required"


class CardChanged(ActionError):
    """The card the person approved is not the card as it stands now."""

    code = "card_changed"


class NotEligible(ActionError):
    """The proposed file fails a check, so it cannot be approved."""

    code = "not_eligible"

    def __init__(self, checks: list[Check]) -> None:
        super().__init__("cannot approve: " + "; ".join(c.detail for c in checks if not c.ok))
        self.checks = checks


class ExecutionBlocked(ActionError):
    """A check failed immediately before the upload. Nothing was sent; the state is unchanged."""

    code = "execution_blocked"

    def __init__(self, checks: list[Check]) -> None:
        super().__init__("not uploaded: " + "; ".join(c.detail for c in checks if not c.ok))
        self.checks = checks


# ---------------- configuration and portal contract ----------------


@dataclass(frozen=True)
class UploadPolicy:
    """What the executor needs to know beyond the database and the portal."""

    vault_dir: Path
    portal_base_url: str
    upload_timeout_ms: int = 10_000
    pre_admission_days: int | None = None
    post_admission_days: int | None = None
    max_attempts: int = MAX_ATTEMPTS
    stale_after_seconds: float | None = None  # default: the attempt's deadline plus STALE_GRACE_SECONDS

    @classmethod
    def from_settings(cls, settings: Any) -> UploadPolicy:
        return cls(vault_dir=settings.vault_dir, portal_base_url=settings.portal_base_url,
                   upload_timeout_ms=settings.portal_upload_timeout_ms,
                   pre_admission_days=settings.policy_pre_admission_days,
                   post_admission_days=settings.policy_post_admission_days)


class UploadPortal(Protocol):
    """What the executor needs from the portal adapter (`PortalClient`, or a proxy to its thread)."""

    def read_claim(self, claim_id: str, *, run_id: str) -> ReadResult: ...

    def list_documents(self, claim_id: str, *, run_id: str) -> DocumentListResult: ...

    def upload(self, *, action_id: str) -> UploadResult: ...


# ---------------- eligibility checks ----------------


@dataclass(frozen=True)
class Check:
    """One rule, whether it holds, and why, in words a family member can read."""

    name: str
    ok: bool
    detail: str

    @property
    def label(self) -> str:
        return CHECK_LABELS.get(self.name, self.name)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _metadata(conn: sqlite3.Connection, vf: repo.VaultFile, run_id: str) -> VaultMetadata | None:
    """What the file says about itself: its registered fields, else the latest valid read of it in this run."""
    if any(v is not None for v in (vf.doc_type, vf.patient_name, vf.doc_date, vf.legible)):
        return VaultMetadata(doc_type=vf.doc_type, patient_name=vf.patient_name, doc_date=vf.doc_date,
                             invoice_no=vf.invoice_no, hospital=vf.hospital, amount_inr=vf.amount_inr,
                             legible=vf.legible)
    row = conn.execute(
        "SELECT parsed_json FROM extractions WHERE run_id = ? AND subject = 'vault_file' AND subject_ref = ? "
        "AND valid = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1", (run_id, vf.id),
    ).fetchone()
    return VaultMetadata.model_validate(json.loads(row["parsed_json"])) if row else None


def _requirement(claim: dict[str, Any] | None, requirement_id: str) -> dict[str, Any] | None:
    if claim is None:
        return None
    return next((r for r in claim.get("requirements", []) if r.get("req_id") == requirement_id), None)


def eligibility_checks(conn: sqlite3.Connection, action: repo.Action, *, claim: dict[str, Any] | None,
                       policy: UploadPolicy, require_approval: bool, today: date | None = None) -> list[Check]:
    """Every rule an upload must pass, judged against `claim` (a claim-page read, `ClaimSnapshot.to_json()`).

    Pure over stored data and the given read: no portal calls and no writes."""
    run = repo.get_run(conn, action.run_id)
    registry = repo.get_claim_record(conn, action.claim_id)
    patient = repo.get_person(conn, registry.patient_id).name
    checks: list[Check] = []

    if require_approval:
        approval = repo.get_action_approval(conn, action.id)
        bound = (
            approval is not None and approval.decision == "approved"
            and (approval.idem_key, approval.claim_id, approval.requirement_id, approval.file_sha256,
                 approval.vault_file_id)
            == (action.idem_key, action.claim_id, action.requirement_id, action.file_sha256, action.vault_file_id)
            and approval.decided_by == action.approved_by == run.requester_id
        )
        checks.append(Check("approval", bound,
                            f"approved by {approval.decided_by} at {approval.decided_at} for this claim, requirement "
                            "and file only" if bound and approval else
                            "no approval by the requester is recorded for this exact claim, requirement and file"))

    vf: repo.VaultFile | None = None
    if action.vault_file_id:
        try:
            vf = repo.get_vault_file(conn, action.vault_file_id)
        except repo.NotFound:
            vf = None
    if vf is None:
        checks.append(Check("file_registered", False, "the upload has no registered vault file"))
    elif vf.owner_id != run.requester_id:
        checks.append(Check("file_registered", False, "the file is not registered to the person asking"))
    elif vf.sha256 != action.file_sha256:
        checks.append(Check("file_registered", False, "the registered file's SHA-256 differs from the proposed one"))
    else:
        _, problems = vault_bytes(policy.vault_dir, vf)
        checks.append(Check("file_registered", not problems, "; ".join(problems) if problems else
                            f"{vf.original_name} is in your vault and its bytes match SHA-256 {vf.sha256[:12]}…"))

    meta = _metadata(conn, vf, action.run_id) if vf is not None else None
    if meta is None:
        checks.append(Check("patient", False, "the file's details could not be read"))
        checks.append(Check("legible", False, "the file's details could not be read"))
    else:
        if not meta.patient_name:
            checks.append(Check("patient", False, "no patient name could be found on the file"))
        elif meta.patient_name.casefold().strip() != patient.casefold().strip():
            checks.append(Check("patient", False,
                                f"the file is for {meta.patient_name}, but {action.claim_id} is for {patient}"))
        else:
            checks.append(Check("patient", True, f"the file names {meta.patient_name}, the patient on {action.claim_id}"))
        checks.append(Check("legible", meta.legible is True, "the file is readable" if meta.legible is True else
                            "the file is not readable" if meta.legible is False else "readability is unknown"))

    if claim is None or claim.get("claim_id") != action.claim_id:
        missing = "no read of the claim page is on record to check against"
        checks += [Check(n, False, missing) for n in ("requirement", "document_type", "document_date", "not_on_claim")]
        return checks

    req = _requirement(claim, action.requirement_id)
    needed = needed_type(req) if req else None
    cashless = (claim.get("route") or "").strip().lower() == "cashless"
    if req is None:
        checks.append(Check("requirement", False, f"{action.requirement_id} is not requested on {action.claim_id}"))
    elif not is_pending(req.get("state")):
        checks.append(Check("requirement", False, f"{action.requirement_id} is {req.get('state')}, no longer pending"))
    elif cashless and needed in HOSPITAL_HELD:
        checks.append(Check("requirement", False,
                            f"{action.requirement_id} is a hospital-held record on a cashless claim; the hospital provides it"))
    else:
        checks.append(Check("requirement", True, f"{action.requirement_id} is pending: {req.get('description')}"))

    if meta is None or needed is None:
        checks.append(Check("document_type", False, "the needed or actual document type is unknown"))
    else:
        checks.append(Check("document_type", meta.doc_type == needed,
                            f"the file is a {(meta.doc_type or 'unknown type').replace('_', ' ')}"
                            + ("" if meta.doc_type == needed else f", not a {needed.replace('_', ' ')}")))

    window = admission_window(claim["admission_date"], claim.get("discharge_date"),
                              pre_admission_days=policy.pre_admission_days,
                              post_admission_days=policy.post_admission_days, today=today)
    if meta is None or not meta.doc_date:
        checks.append(Check("document_date", False, "no date could be found on the file"))
    else:
        inside = window[0] <= date.fromisoformat(meta.doc_date) <= window[1]
        checks.append(Check("document_date", inside, f"dated {meta.doc_date}, "
                            + ("inside" if inside else "outside") + f" {window[0]} to {window[1]}"))

    already = [d for d in claim.get("uploaded", []) if d.get("sha256") == action.file_sha256]
    checks.append(Check("not_on_claim", not already, "this exact file is not on the claim yet" if not already else
                        "this exact file is already on the claim under "
                        + ", ".join(d.get("requirement_id") or "no request" for d in already)))
    return checks


# ---------------- approval card ----------------


@dataclass(frozen=True)
class ApprovalCard:
    """Everything a person sees before approving, and its digest (what an approval is bound to)."""

    action: repo.Action
    card: dict[str, Any]
    card_sha256: str
    checks: list[Check]

    @property
    def can_approve(self) -> bool:
        return self.action.state == ActionState.PROPOSED.value and all(c.ok for c in self.checks)


def _owned(conn: sqlite3.Connection, action_id: str, person_id: str) -> tuple[repo.Action, repo.Run]:
    try:
        action = repo.get_action(conn, action_id)
    except repo.NotFound as exc:
        raise NotYourAction(f"no upload {action_id} for you") from exc
    run = repo.get_run(conn, action.run_id)
    if run.requester_id != person_id:
        raise NotYourAction(f"no upload {action_id} for you")
    return action, run


def _claim_from(evidence: repo.Evidence | None) -> dict[str, Any] | None:
    if evidence is None or evidence.kind != "portal_page" or not isinstance(evidence.extracted, dict):
        return None
    claim = evidence.extracted.get("claim") if evidence.extracted.get("ok") else None
    return claim if isinstance(claim, dict) else None


def approval_card(db: Database, action_id: str, *, person_id: str, policy: UploadPolicy,
                  today: date | None = None) -> ApprovalCard:
    """Build the card for an upload: patient, claim, requirement, file (with preview link and hash),
    the evidence the match rests on, the destination, and every check. Only the requester sees it."""
    with db.read() as conn:
        action, _ = _owned(conn, action_id, person_id)
        registry = repo.get_claim_record(conn, action.claim_id)
        patient = repo.get_person(conn, registry.patient_id)
        vf = None
        if action.vault_file_id:
            try:
                vf = repo.get_vault_file(conn, action.vault_file_id)
            except repo.NotFound:
                vf = None
        match = repo.get_decision(conn, action.match_decision_id) if action.match_decision_id else None
        evidence = repo.get_evidence(conn, match.evidence_id) if match and match.evidence_id else None
        claim = _claim_from(evidence)
        checks = eligibility_checks(conn, action, claim=claim, policy=policy, require_approval=False, today=today)
    req = _requirement(claim, action.requirement_id) or {}
    destination = f"{policy.portal_base_url.rstrip('/')}/claims/{action.claim_id}/documents"
    card = {
        "action_id": action.id,
        "idem_key": action.idem_key,
        "patient": {"id": patient.id, "name": patient.name},
        "claim": {"claim_id": action.claim_id, "policy_no": registry.policy_no,
                  "route": (claim or {}).get("route"), "hospital": (claim or {}).get("hospital")},
        "requirement": {"requirement_id": action.requirement_id, "description": req.get("description"),
                        "needed_doc_type": needed_type(req) if req else None, "due_date": req.get("due_date"),
                        "state": req.get("state")},
        "file": None if vf is None else {
            "vault_file_id": vf.id, "filename": vf.original_name, "content_type": vf.content_type,
            "size_bytes": vf.size_bytes, "sha256": action.file_sha256, "preview_url": f"/vault/files/{vf.id}",
            "doc_type": vf.doc_type, "patient_name": vf.patient_name, "doc_date": vf.doc_date, "legible": vf.legible,
            "invoice_no": vf.invoice_no, "amount_inr": vf.amount_inr, "hospital": vf.hospital,
        },
        "matching_evidence": {
            "match_decision_id": match.id if match else None, "result": match.result if match else None,
            "reason": match.reason if match else None, "decided_at": match.created_at if match else None,
            "window": match.input_refs.get("window") if match else None,
            "portal_evidence_id": evidence.id if evidence else None,
            "portal_read_at": evidence.fetched_at if evidence else None,
            "portal_html_sha256": evidence.html_sha256 if evidence else None,
        },
        "destination": {"portal": policy.portal_base_url, "upload_url": destination,
                        "requirement_id": action.requirement_id, "method": "the portal's own upload form"},
        "checks": [c.to_json() for c in checks],
    }
    return ApprovalCard(action, card, repo.card_digest(card), checks)


def _already_decided(db: Database, action: repo.Action, person_id: str, wanted: str) -> repo.Action | None:
    """For a repeated approve/reject: the action unchanged if this person already made this decision."""
    if action.state == ActionState.PROPOSED.value:
        return None
    with db.read() as conn:
        approval = repo.get_action_approval(conn, action.id)
    if approval is not None and approval.decision == wanted and approval.decided_by == person_id:
        return action
    done = approval.decision if approval else action.state.lower()
    raise repo.InvalidTransition(f"this upload was already {done}; it cannot now be {wanted}")


def approve_upload(db: Database, action_id: str, *, person_id: str, card_sha256: str, policy: UploadPolicy,
                   today: date | None = None) -> tuple[repo.Action, bool]:
    """Record a signed-in person's explicit approval of the card they were shown.

    Returns (action, newly_approved). Approving again (double click, reconnect) returns the action as
    it stands, whatever its state; it is never reset. Consent is re-checked in the same transaction."""
    card = approval_card(db, action_id, person_id=person_id, policy=policy, today=today)
    existing = _already_decided(db, card.action, person_id, "approved")
    if existing is not None:
        return existing, False
    if card_sha256 != card.card_sha256:
        raise CardChanged("the upload details changed since they were shown; review the card again")
    if not card.can_approve:
        raise NotEligible(card.checks)

    newly: list[bool] = []

    def apply(conn: sqlite3.Connection, action: repo.Action, decision: cs.ConsentDecision) -> repo.Action:
        if action.state != ActionState.PROPOSED.value:
            return action  # a concurrent approval got here first
        repo.record_action_check(conn, action_id=action.id, phase=CheckPhase.APPROVAL.value, passed=True,
                                 checks=[c.to_json() for c in card.checks],
                                 evidence_id=card.card["matching_evidence"]["portal_evidence_id"],
                                 consent_decision_id=decision.decision_id)
        newly.append(True)
        return repo.approve_action(conn, action.id, approved_by=person_id, card=card.card)

    action = cs.guarded_upload_change(db, action_id, person_id, apply)
    logger.info("upload approved", extra={"action_id": action_id, "new": bool(newly)})
    return action, bool(newly)


def reject_upload(db: Database, action_id: str, *, person_id: str, policy: UploadPolicy,
                  today: date | None = None) -> tuple[repo.Action, bool]:
    """Record a person's rejection. Nothing is ever uploaded for a rejected action."""
    card = approval_card(db, action_id, person_id=person_id, policy=policy, today=today)
    existing = _already_decided(db, card.action, person_id, "rejected")
    if existing is not None:
        return existing, False
    with db.transaction() as conn:
        action = repo.reject_action(conn, action_id, rejected_by=person_id, card=card.card)
    logger.info("upload rejected", extra={"action_id": action_id})
    return action, True


def propose_from_match(db: Database, *, run_id: str, decision_id: str, person_id: str) -> tuple[repo.Action, bool]:
    """Turn a MATCHED decision in the person's own run into a PROPOSED upload (or return the existing one)."""
    with db.read() as conn:
        try:
            run = repo.get_run(conn, run_id)
        except repo.NotFound as exc:
            raise NotYourAction(f"no run {run_id} for you") from exc
    if run.requester_id != person_id:
        raise NotYourAction(f"no run {run_id} for you")
    return propose_upload(db, run_id=run_id, decision_id=decision_id)


# ---------------- execution and verification ----------------


@dataclass(frozen=True)
class ExecutionReport:
    """What one execute or reconcile call did, honestly."""

    action: repo.Action
    uploaded: bool                    # an upload request was sent during this call
    message: str
    path: tuple[str, ...] = ()        # states this call moved the action through
    verification: str | None = None   # result of this call's re-read, if one was made
    checks: tuple[Check, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"action": asdict(self.action), "uploaded": self.uploaded, "message": self.message,
                "path": list(self.path), "verification": self.verification,
                "checks": [c.to_json() for c in self.checks]}


def _observe(docs: DocumentListResult, action: repo.Action) -> tuple[VerificationResult, dict[str, Any]]:
    """Compare the re-read document list with what was intended."""
    ours = [d for d in docs.uploaded if d.sha256 == action.file_sha256]
    here = [d for d in ours if d.requirement_id == action.requirement_id]
    req = next((r for r in docs.requirements if r.req_id == action.requirement_id), None)
    observed = {
        "read_evidence_id": docs.evidence_id, "raw_status": docs.raw_status,
        "status_updated_at": docs.status_updated_at, "requirement_state": req.state if req else None,
        "documents_listed": len(docs.uploaded),
        "same_file_under_requirement": [asdict(d) for d in here],
        "same_file_elsewhere": [asdict(d) for d in ours if d not in here],
    }
    if here:
        return VerificationResult.VERIFIED, observed
    return (VerificationResult.MISMATCH if ours else VerificationResult.NOT_FOUND), observed


def _intended(action: repo.Action) -> dict[str, Any]:
    return {"claim_id": action.claim_id, "requirement_id": action.requirement_id,
            "file_sha256": action.file_sha256, "vault_file_id": action.vault_file_id}


def _describe(outcome: str, http_status: int | None, code: str | None, message: str | None) -> str:
    parts = [outcome] + ([f"HTTP {http_status}"] if http_status else []) + ([code] if code else [])
    return " / ".join(parts) + (f": {message}" if message else "")


class UploadExecutor:
    """Runs approved uploads against one portal and decides their outcome only from re-reads."""

    def __init__(self, db: Database, portal: UploadPortal, policy: UploadPolicy) -> None:
        self.db, self.portal, self.policy = db, portal, policy

    # ---- entry points ----

    def execute(self, action_id: str, *, requester_id: str) -> ExecutionReport:
        """Upload an approved (or retry-allowed) action once, then verify it by re-reading the portal.

        Safe to call repeatedly or concurrently: a finished action is returned as it is, one in flight is
        left alone, and an UNKNOWN one is re-read rather than re-sent."""
        action = self._owned(action_id, requester_id)
        state = ActionState(action.state)
        if state is ActionState.PROPOSED:
            raise ApprovalRequired("a person must approve this upload first; nothing was uploaded")
        if state in TERMINAL_ACTION_STATES:
            return ExecutionReport(action, False, self._final_message(action), (state.value,))
        if state in (ActionState.EXECUTING, ActionState.UNKNOWN, ActionState.PENDING_VERIFY):
            return self.reconcile(action_id, requester_id=requester_id)
        return self._attempt(action, requester_id)

    def reconcile(self, action_id: str, *, requester_id: str) -> ExecutionReport:
        """Re-read the portal for an action whose outcome is not settled. Never uploads.

        An EXECUTING action whose attempt never reported back (the process stopped mid-upload) is
        moved to UNKNOWN first; one still within its time bound is left alone."""
        action = self._owned(action_id, requester_id)
        path = [action.state]
        if action.state == ActionState.EXECUTING.value:
            with self.db.read() as conn:
                attempt = repo.open_attempt(conn, action.id)
            if attempt is not None and not self._stale(attempt):
                return ExecutionReport(action, False, "An upload attempt is in progress; nothing more was sent. "
                                       "Check again in a moment.", tuple(path))
            with self.db.transaction() as conn:
                current = repo.get_action(conn, action.id)
                if current.state == ActionState.EXECUTING.value:
                    if attempt is not None:
                        repo.finish_attempt(conn, attempt.id, outcome=AttemptOutcome.ABANDONED.value,
                                            message="no result was recorded within the time bound")
                    repo.transition_action(conn, action.id, ActionState.UNKNOWN.value,
                                           error=f"attempt {current.attempts}: no result was recorded; "
                                                 "whether the portal stored the file is unknown")
            path.append(ActionState.UNKNOWN.value)
        elif action.state not in (ActionState.UNKNOWN.value, ActionState.PENDING_VERIFY.value):
            message = (self._final_message(action) if ActionState(action.state) in TERMINAL_ACTION_STATES
                       else "Nothing to check: this upload has not been attempted.")
            return ExecutionReport(action, False, message, tuple(path))
        return self._verify(action.id, requester_id, path=path, uploaded=False)

    # ---- one attempt ----

    def _attempt(self, action: repo.Action, requester_id: str) -> ExecutionReport:
        start = action.state
        upload_consent = self._consent_or_block(action, requester_id, UPLOAD)
        self._consent_or_block(action, requester_id, READ)
        consent_ok = Check("consent", True, upload_consent.reason)
        try:
            read = self.portal.read_claim(action.claim_id, run_id=action.run_id)
        except PortalError as exc:
            self._blocked(action, [consent_ok, Check("portal_read", False, f"{exc.code}: {exc.message}")],
                          evidence_id=exc.evidence_id, consent_decision_id=upload_consent.decision_id)
            raise
        claim = read.snapshot.to_json()
        if any(d["sha256"] == action.file_sha256 and d["requirement_id"] == action.requirement_id
               for d in claim["uploaded"]):
            # Already stored (an earlier attempt landed late, or someone uploaded it). Verify, never re-send.
            return self._verify(action.id, requester_id, path=[start], uploaded=False)

        with self.db.read() as conn:
            checks = [consent_ok, Check("portal_read", True, f"claim page read (evidence {read.evidence_id})"),
                      *eligibility_checks(conn, action, claim=claim, policy=self.policy, require_approval=True)]
        if not all(c.ok for c in checks):
            self._blocked(action, checks, evidence_id=read.evidence_id, consent_decision_id=upload_consent.decision_id)
            raise ExecutionBlocked(checks)

        deadline_ms = int(getattr(self.portal, "upload_timeout_ms", None) or self.policy.upload_timeout_ms)
        opened: list[repo.ActionAttempt] = []

        def claim_attempt(conn: sqlite3.Connection, current: repo.Action, decision: cs.ConsentDecision) -> repo.Action:
            if current.state != start or current.attempts != action.attempts:
                return current  # another request already took this attempt
            check = repo.record_action_check(conn, action_id=current.id, phase=CheckPhase.EXECUTE.value, passed=True,
                                             checks=[c.to_json() for c in checks], evidence_id=read.evidence_id,
                                             consent_decision_id=decision.decision_id)
            running = repo.transition_action(conn, current.id, ActionState.EXECUTING.value)
            opened.append(repo.start_attempt(conn, action_id=current.id, check_id=check.id, deadline_ms=deadline_ms))
            return running

        try:
            current = cs.guarded_upload_change(self.db, action.id, requester_id, claim_attempt)
        except cs.ConsentDenied as denied:
            self._blocked(action, [Check("consent", False, denied.decision.reason)],
                          consent_decision_id=denied.decision.decision_id)
            raise
        if not opened:
            return ExecutionReport(current, False, "Another request is already handling this upload; nothing more "
                                   "was sent.", (start, current.state), checks=tuple(checks))
        return self._send(current, opened[0], requester_id, start, tuple(checks))

    def _send(self, action: repo.Action, attempt: repo.ActionAttempt, requester_id: str, start: str,
              checks: tuple[Check, ...]) -> ExecutionReport:
        """The bounded upload itself. Whatever comes back, the outcome is decided by a re-read."""
        http_status: int | None = None
        try:
            result = self.portal.upload(action_id=action.id)
            outcome, http_status, code, message, evidence_id = (result.outcome, result.http_status,
                                                                result.error_code, result.message, result.evidence_id)
        except PortalError as exc:
            outcome, code, message, evidence_id = AttemptOutcome.ERROR.value, exc.code, exc.message, exc.evidence_id
        except Exception as exc:  # noqa: BLE001 - whatever failed, whether bytes reached the portal is unknown
            logger.exception("upload call failed", extra={"action_id": action.id})
            outcome, code, message, evidence_id = AttemptOutcome.ERROR.value, type(exc).__name__, str(exc)[:200], None

        # Only a confirmation or a 4xx refusal is a definite answer; a timeout, a 5xx or anything
        # unconfirmed may have stored the file, so it is UNKNOWN until the re-read.
        definite = outcome == AttemptOutcome.ACCEPTED.value or (
            outcome == AttemptOutcome.REJECTED.value and http_status is not None and 400 <= http_status < 500)
        target = ActionState.PENDING_VERIFY if definite else ActionState.UNKNOWN
        error = None if outcome == AttemptOutcome.ACCEPTED.value else \
            f"attempt {attempt.attempt_no}: {_describe(outcome, http_status, code, message)}"
        with self.db.transaction() as conn:
            repo.finish_attempt(conn, attempt.id, outcome=outcome, http_status=http_status, error_code=code,
                                message=message, evidence_id=evidence_id)
            repo.transition_action(conn, action.id, target.value, error=error)
        logger.info("upload attempt finished", extra={"action_id": action.id, "attempt": attempt.attempt_no,
                                                      "outcome": outcome, "http_status": http_status,
                                                      "state": target.value})
        report = self._verify(action.id, requester_id, path=[start, ActionState.EXECUTING.value, target.value],
                              uploaded=True)
        return ExecutionReport(report.action, True, report.message, report.path, report.verification,
                               report.checks or checks)

    # ---- verification by re-reading ----

    def _verify(self, action_id: str, requester_id: str, *, path: list[str], uploaded: bool) -> ExecutionReport:
        """Re-read the uploaded-document list and status stamp, record intended vs observed, decide."""
        with self.db.read() as conn:
            action = repo.get_action(conn, action_id)
        before = ActionState(action.state)
        refs: dict[str, Any] = {"action_id": action.id, "idem_key": action.idem_key, "attempts": action.attempts,
                                "state_before": before.value, "intended": _intended(action),
                                "upload_sent_this_call": uploaded, "last_error": action.last_error}
        docs, read_error, failure_evidence = self._reread(action, requester_id)

        if docs is None:
            with self.db.transaction() as conn:
                current = repo.get_action(conn, action.id)
                repo.record_decision(
                    conn, run_id=action.run_id, kind="verification", result=VerificationResult.FETCH_FAILED.value,
                    reason=f"could not re-read the portal: {read_error}", subject_ref=action.id,
                    evidence_id=failure_evidence, evidence_state=None if failure_evidence else "missing",
                    input_refs={**refs, "state_after": current.state, "observed": None, "read_error": read_error},
                )
                if current.state == before.value and before in (ActionState.UNKNOWN, ActionState.PENDING_VERIFY):
                    current = repo.note_action_error(conn, action.id, expected_state=before.value,
                                                     error=f"re-read failed: {read_error}")
            message = (f"The portal could not be re-read ({read_error}). The upload stays "
                       f"{current.state}; nothing will be sent again until a re-read shows what the portal holds.")
            return ExecutionReport(current, uploaded, message, tuple(path),
                                   VerificationResult.FETCH_FAILED.value)

        result, observed = _observe(docs, action)
        retry_checks: list[Check] = []
        target: ActionState | None
        if result is VerificationResult.VERIFIED:
            target = ActionState.VERIFIED
        elif before in (ActionState.APPROVED, ActionState.RETRY_ALLOWED):
            target = None  # a moment ago the page listed it; now it does not. Change nothing, send nothing.
        elif result is VerificationResult.MISMATCH:
            target = ActionState.FAILED
        elif before is ActionState.UNKNOWN:
            retry_checks = self._retry_checks(action, requester_id, docs)
            target = ActionState.RETRY_ALLOWED if all(c.ok for c in retry_checks) else ActionState.FAILED
        else:
            target = ActionState.FAILED  # PENDING_VERIFY: the portal answered, but the file is not listed

        with self.db.transaction() as conn:
            current = repo.get_action(conn, action.id)
            moves = target is not None and current.state == before.value and target is not before
            after = target.value if moves and target else current.state
            reason = self._verification_reason(result, target, observed, retry_checks)
            repo.record_decision(
                conn, run_id=action.run_id, kind="verification", result=result.value, reason=reason,
                evidence_id=docs.evidence_id, subject_ref=action.id,
                input_refs={**refs, "state_after": after, "observed": observed,
                            "retry_checks": [c.to_json() for c in retry_checks]},
            )
            if moves and target:
                error = None if target is ActionState.VERIFIED else (
                    "; ".join(c.detail for c in retry_checks if not c.ok) or reason)
                if target is ActionState.RETRY_ALLOWED:
                    error = action.last_error
                current = repo.transition_action(conn, action.id, target.value, error=error)
        if moves and target:
            path.append(target.value)
        message = self._message(current, before, result, uploaded)
        logger.info("upload verified", extra={"action_id": action.id, "result": result.value, "state": current.state})
        return ExecutionReport(current, uploaded, message, tuple(path), result.value, tuple(retry_checks))

    def _reread(self, action: repo.Action, requester_id: str) -> tuple[DocumentListResult | None, str | None, str | None]:
        decision = cs.check_consent(self.db, requester_id, action.claim_id, READ, run_id=action.run_id)
        if not decision.allowed:
            return None, f"consent does not allow reading {action.claim_id} now ({decision.result.value})", None
        try:
            return self.portal.list_documents(action.claim_id, run_id=action.run_id), None, None
        except PortalError as exc:
            return None, f"{exc.code}: {exc.message}", exc.evidence_id

    def _retry_checks(self, action: repo.Action, requester_id: str, docs: DocumentListResult) -> list[Check]:
        """A retry is allowed only if consent and every eligibility rule still hold on this fresh read."""
        consent = cs.check_consent(self.db, requester_id, action.claim_id, UPLOAD, run_id=action.run_id)
        checks = [Check("consent", consent.allowed, consent.reason)]
        claim = docs.snapshot.to_json() if docs.snapshot is not None else None
        with self.db.read() as conn:
            checks += eligibility_checks(conn, action, claim=claim, policy=self.policy, require_approval=True)
        used = action.attempts
        checks.append(Check("attempts", used < self.policy.max_attempts,
                            f"{used} of {self.policy.max_attempts} attempts used"))
        return checks

    # ---- helpers ----

    def _owned(self, action_id: str, requester_id: str) -> repo.Action:
        with self.db.read() as conn:
            return _owned(conn, action_id, requester_id)[0]

    def _consent_or_block(self, action: repo.Action, requester_id: str, what: str) -> cs.ConsentDecision:
        decision = cs.check_consent(self.db, requester_id, action.claim_id, what, run_id=action.run_id)
        if not decision.allowed:
            self._blocked(action, [Check("consent", False, f"{decision.result.value}: {decision.reason}")],
                          consent_decision_id=decision.decision_id)
            raise cs.ConsentDenied(decision)
        return decision

    def _blocked(self, action: repo.Action, checks: list[Check], *, evidence_id: str | None = None,
                 consent_decision_id: str | None = None) -> None:
        with self.db.transaction() as conn:
            repo.record_action_check(conn, action_id=action.id, phase=CheckPhase.EXECUTE.value, passed=False,
                                     checks=[c.to_json() for c in checks], evidence_id=evidence_id,
                                     consent_decision_id=consent_decision_id)
        logger.warning("upload blocked before sending", extra={
            "action_id": action.id, "failed": [c.name for c in checks if not c.ok]})

    def _stale(self, attempt: repo.ActionAttempt) -> bool:
        limit = self.policy.stale_after_seconds
        if limit is None:
            limit = attempt.deadline_ms / 1000 + STALE_GRACE_SECONDS
        return (datetime.now(timezone.utc) - parse_utc(attempt.started_at)).total_seconds() >= limit

    @staticmethod
    def _verification_reason(result: VerificationResult, target: ActionState | None,
                             observed: dict[str, Any], retry_checks: list[Check]) -> str:
        stamp = f"(claim status '{observed['raw_status']}', updated {observed['status_updated_at']})"
        if result is VerificationResult.VERIFIED:
            return f"the re-read lists this exact file under the requirement {stamp}"
        if result is VerificationResult.MISMATCH:
            return f"the re-read lists this file, but under a different requirement {stamp}"
        if target is ActionState.RETRY_ALLOWED:
            return f"the re-read shows the file is not on the claim and every check still passes {stamp}"
        if retry_checks:
            return (f"the re-read shows the file is not on the claim; retry not allowed: "
                    + "; ".join(c.detail for c in retry_checks if not c.ok) + f" {stamp}")
        return f"the re-read does not list this file {stamp}"

    @staticmethod
    def _final_message(action: repo.Action) -> str:
        if action.state == ActionState.VERIFIED.value:
            return "Already done: a re-read of the portal showed this file under the requirement. Nothing was sent."
        if action.state == ActionState.REJECTED.value:
            return "You rejected this upload. Nothing was uploaded."
        return f"This upload failed and will not be retried automatically ({action.last_error}). Nothing was sent."

    @staticmethod
    def _message(action: repo.Action, before: ActionState, result: VerificationResult, uploaded: bool) -> str:
        where = f"{action.requirement_id} on {action.claim_id}"
        state = action.state
        if state == ActionState.VERIFIED.value:
            if before is ActionState.UNKNOWN:
                return (f"The portal did not give a clear answer, but a fresh read shows the file stored under "
                        f"{where}. Marked verified; no second upload was sent.")
            if uploaded:
                return f"Uploaded and verified: a fresh read of the portal lists this exact file under {where}."
            return f"The file is already on the portal under {where}; verified by a fresh read. Nothing was sent."
        if state == ActionState.RETRY_ALLOWED.value:
            return (f"The upload did not go through: a fresh read shows the file is not on {action.claim_id}. "
                    f"Every check still passes, so you can retry (attempt {action.attempts + 1}).")
        if state == ActionState.FAILED.value:
            return f"Not done: {action.last_error}. Please check the portal; nothing will be retried automatically."
        if result is VerificationResult.NOT_FOUND and before in (ActionState.APPROVED, ActionState.RETRY_ALLOWED):
            return "The portal's listing changed between two reads; nothing was sent. Try again."
        return f"The upload is {state}."


def action_history(db: Database, action_id: str, *, person_id: str) -> dict[str, Any]:
    """Approval, checks, attempts and verifications for one of the person's uploads."""
    with db.read() as conn:
        action, _ = _owned(conn, action_id, person_id)
        return {
            "action": action,
            "approval": repo.get_action_approval(conn, action_id),
            "checks": repo.checks_for(conn, action_id),
            "attempts": repo.attempts_for(conn, action_id),
            "verifications": repo.verifications_for(conn, action_id),
        }
