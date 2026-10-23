"""The ClaimSaathi workflow: a LangGraph state machine checkpointed in the agent's SQLite database.

    START
      |
    intake_validate --------- invalid request ----------------------------------------> finish --> END
      |
    consent_gate ------------ no read consent (asks the patient) ---------------------> finish
      |
    portal_login <------------------------------------------------------------+
      |  OTP needed            gave up / portal down ---> finish, or            |
      v                        verify_and_reply if the claim was already read   |
    otp_entry  [INTERRUPT: a person types the OTP]                              |
      |  wrong OTP: ask again (3x), then a new login (3x)                       |
      |  signed in: back to the node that needed the portal                     |
      v                                                                         |
    read_claim -------------- session expired --------------------------------->+
      |       \\------------- portal failed: FETCH_FAILED ---> verify_and_reply |
    extract_and_normalise --- nothing pending / unreadable ---> verify_and_reply|
      |                                                                         |
    match_documents --------- status question, no consent to upload, or no      |
      |                       safe (MATCHED) file ---> verify_and_reply         |
      v                                                                         |
    human_approval  [INTERRUPT: approve / reject the card; retry if the portal  |
      |              did not get the file]  reject: next file or verify_and_reply
      v                                                                         |
    execute_upload ---------- session expired --------------------------------->+
      |  RETRY_ALLOWED: back to human_approval (bounded by the executor's attempts)
      |  more files: human_approval
      v
    verify_and_reply  re-reads after any upload (UNKNOWN is re-read before anything else),
      |               records status deterministically, guards the reply
      v
     END

Safety
- Decisions are made only by the deterministic services: consent (`agent.consent`), status mapping
  (`agent.status`), document eligibility (`agent.matcher`), upload state (`agent.actions`). A model may
  only extract fields (validated against the page) and rephrase the reply (guarded); its output never
  routes the graph.
- The thread id is the run id and every step is checkpointed in SQLite (`checkpoints`, `writes`), so a
  run resumes after a restart from its last completed node. Uploads go through `UploadExecutor`, which
  is idempotent: a resumed or repeated node cannot send a file twice, and an uncertain upload is
  re-read before anything else.
- Every node execution is stored in `graph_steps` (input refs, decision, result, evidence, next node),
  including interrupts and crashes, for the UI and the audit.
- Loops are bounded: OTP tries per login, logins per run, approval re-asks, and upload attempts.
"""

from __future__ import annotations

import logging
import operator
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent import actions as act
from agent import consent as cs
from agent import repository as repo
from agent.db import Database, utc_now
from agent.extract import extract_claim
from agent.llm import LlmClient
from agent.matcher import MatcherError, is_pending, match_claim, needed_type, propose_upload
from agent.models import (
    TERMINAL_ACTION_STATES,
    ActionState,
    CanonicalStatus,
    ConsentAction,
    ConsentResult,
    RunState,
    StepResult,
    WorkflowIntent,
)
from agent.portal_client import (
    DocumentListResult,
    LoginState,
    LoginStatus,
    OtpRejected,
    OtpRequired,
    PortalError,
    ReadResult,
    SessionExpired,
    UploadResult,
)
from agent.reply import ReplyFacts, compose_reply, render_template
from agent.status import record_status

logger = logging.getLogger("agent.graph")

MAX_LOGINS = 3        # portal logins per run
MAX_OTP_TRIES = 3     # OTP entries per login
MAX_APPROVAL_ASKS = 3  # re-asks of one card (changed card, wrong person)
RECURSION_LIMIT = 80   # hard stop on node executions per invoke, far above any legitimate path

_CLAIM_ID = re.compile(r"^CLM-\d{4}-\d{4}$")
_POLICY = re.compile(r"^POL-[A-Z0-9][A-Z0-9-]{2,40}$")
_OTP = re.compile(r"^\d{6}$")


# ---------------- state ----------------


class ClaimState(TypedDict, total=False):
    """Everything the workflow knows about one run. Only plain JSON-like values (it is checkpointed)."""

    run_id: str
    requester_id: str
    claim_id: str
    policy_no: str | None
    intent: str                        # WorkflowIntent
    consent: dict[str, Any]            # read / upload results with decision and evidence ids
    upload_allowed: bool
    portal_session: dict[str, Any]     # reference, login state, attempt counters, last error
    after_login: str                   # node to return to once the portal is signed in
    raw_claim: dict[str, Any] | None   # the claim page as read, with its evidence id
    extracted_claim: dict[str, Any] | None
    canonical_status: dict[str, Any] | None
    requested_documents: list[dict[str, Any]]
    uploaded_documents: list[dict[str, Any]]
    vault_candidates: list[dict[str, Any]]
    matching_results: list[dict[str, Any]]
    pending_actions: list[str]         # upload actions still to decide or run, in order
    action_ids: list[str]              # every upload proposed in this run (for the summary)
    proposed_action: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    approval_asks: int
    action_result: dict[str, Any] | None
    action_results: dict[str, dict[str, Any]]
    verification_result: dict[str, Any] | None
    evidence_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    outcome: str | None                # completed | blocked | failed
    blocked_reason: str | None
    final_response: dict[str, Any] | None
    next_node: str


# Allowed transitions. A node naming any other target is a bug and stops the run.
EDGES: dict[str, tuple[str, ...]] = {
    "intake_validate": ("consent_gate", "finish"),
    "consent_gate": ("portal_login", "finish"),
    "portal_login": ("otp_entry", "read_claim", "execute_upload", "verify_and_reply", "finish"),
    "otp_entry": ("otp_entry", "portal_login", "read_claim", "execute_upload", "verify_and_reply", "finish"),
    "read_claim": ("extract_and_normalise", "portal_login", "verify_and_reply", "finish"),
    "extract_and_normalise": ("match_documents", "verify_and_reply"),
    "match_documents": ("human_approval", "verify_and_reply"),
    "human_approval": ("human_approval", "execute_upload", "verify_and_reply"),
    "execute_upload": ("human_approval", "portal_login", "verify_and_reply"),
    "verify_and_reply": ("portal_login", END),
    "finish": (END,),
}
RESUMABLE_AFTER_LOGIN = ("read_claim", "execute_upload", "verify_and_reply")


class WorkflowPortal(Protocol):
    """The portal calls the workflow uses (`PortalClient`, or `SessionPortal` in the web app)."""

    @property
    def state(self) -> LoginState: ...

    def login(self) -> LoginStatus: ...

    def submit_otp(self, otp: str) -> LoginStatus: ...

    def read_claim(self, claim_id: str, *, run_id: str) -> ReadResult: ...

    def list_documents(self, claim_id: str, *, run_id: str) -> DocumentListResult: ...

    def upload(self, *, action_id: str) -> UploadResult: ...


@dataclass
class WorkflowDeps:
    """What the nodes use. Never checkpointed; rebuilt on every process start."""

    db: Database
    portal_for: Callable[[str], WorkflowPortal]   # requester id -> portal handle
    policy: act.UploadPolicy
    llm: LlmClient | None = None                  # extraction (validated) and reply rephrase (guarded) only


# ---------------- node plumbing ----------------


@dataclass
class Step:
    """What a node did: the state update, where to go next, and what to record."""

    next: str
    decision: str
    result: StepResult = StepResult.OK
    update: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    error: str | None = None


def _set_run_state(db: Database, run_id: str, state: RunState, final_status: str | None = None) -> None:
    with db.transaction() as conn:
        run = repo.get_run(conn, run_id)
        if run.state == state.value and final_status is None:
            return
        if RunState(run.state) not in (RunState.BLOCKED, RunState.COMPLETED, RunState.FAILED):
            repo.update_run(conn, run_id, state=state.value, final_status=final_status)


def _record(db: Database, state: ClaimState, node: str, started: str, result: StepResult, decision: str, *,
            next_node: str | None = None, inputs: dict[str, Any] | None = None,
            outputs: dict[str, Any] | None = None, evidence: list[str] | None = None,
            error: str | None = None) -> None:
    with db.transaction() as conn:
        repo.record_step(conn, run_id=state["run_id"], node=node, started_at=started, result=result.value,
                         decision=decision, next_node=next_node, input_refs=inputs, output_refs=outputs,
                         evidence_ids=[e for e in (evidence or []) if e], error=error)


def _interrupt_value(exc: GraphInterrupt) -> Any:
    try:
        return exc.args[0][0].value
    except (IndexError, AttributeError, TypeError):
        return {}


def _wrap(deps: WorkflowDeps, name: str, fn: Callable[[ClaimState], Step]) -> Callable[[ClaimState], dict[str, Any]]:
    """Run a node, persist what it did, and turn its Step into a state update."""

    def run(state: ClaimState) -> dict[str, Any]:
        started = utc_now()
        try:
            step = fn(state)
        except GraphInterrupt as exc:  # paused for a person; the node runs again from the top on resume
            asked = _interrupt_value(exc)
            _record(deps.db, state, name, started, StepResult.INTERRUPTED,
                    f"waiting for a person: {asked.get('type', 'input') if isinstance(asked, dict) else 'input'}",
                    inputs={"interrupt": asked if isinstance(asked, dict) else {"value": str(asked)}})
            _set_run_state(deps.db, state["run_id"], RunState.AWAITING_HUMAN)
            raise
        except BaseException as exc:  # recorded, then the run stops; it can continue from its last checkpoint
            _record(deps.db, state, name, started, StepResult.FAILED, f"{name} raised {type(exc).__name__}",
                    error=str(exc)[:500])
            raise
        if step.next not in EDGES[name]:
            raise RuntimeError(f"{name} tried to move to {step.next!r}; allowed: {EDGES[name]}")
        _record(deps.db, state, name, started, step.result, step.decision, next_node=step.next, inputs=step.inputs,
                outputs=step.outputs, evidence=step.evidence, error=step.error)
        if step.next != END:
            _set_run_state(deps.db, state["run_id"], RunState.RUNNING)
        update = dict(step.update)
        update["next_node"] = step.next
        update["evidence_ids"] = [e for e in step.evidence if e]
        update["errors"] = [step.error] if step.error else []
        return update

    run.__name__ = name
    return run


def _ps(state: ClaimState) -> dict[str, Any]:
    return dict(state.get("portal_session") or {"ref": "portal-session", "state": "logged_out",
                                                 "logins": 0, "otp_tries": 0})


def _session_lost(state: ClaimState, node: str, exc: PortalError) -> Step | None:
    """A read/upload found the portal signed out: go and sign in, then come back here (bounded)."""
    if not isinstance(exc, (SessionExpired, OtpRequired)):
        return None
    ps = _ps(state)
    ps.update(state="logged_out", last_error=exc.code)
    if ps.get("gave_up"):
        return None
    return Step("portal_login", f"the portal session is not signed in ({exc.code}); signing in, then back to {node}",
                StepResult.BLOCKED, update={"portal_session": ps, "after_login": node},
                evidence=[exc.evidence_id] if exc.evidence_id else [])


# ---------------- nodes ----------------


def build_graph(deps: WorkflowDeps) -> StateGraph:
    """The workflow graph (uncompiled). `ClaimWorkflow` compiles it with the SQLite checkpointer."""
    db, policy = deps.db, deps.policy

    def executor(requester_id: str) -> act.UploadExecutor:
        return act.UploadExecutor(db, deps.portal_for(requester_id), policy)

    # 1 --------------------------------------------------------------------------------
    def intake_validate(state: ClaimState) -> Step:
        problems: list[str] = []
        claim_id, requester = state.get("claim_id") or "", state.get("requester_id") or ""
        intent = state.get("intent") or WorkflowIntent.STATUS.value
        inputs = {"requester_id": requester, "claim_id": claim_id, "policy_no": state.get("policy_no"),
                  "intent": intent}
        registry = None
        with db.read() as conn:
            try:
                repo.get_person(conn, requester)
            except repo.NotFound:
                problems.append(f"{requester!r} is not a known person")
            if not _CLAIM_ID.match(claim_id):
                problems.append(f"{claim_id!r} is not a claim id like CLM-2026-0142")
            else:
                try:
                    registry = repo.get_claim_record(conn, claim_id)
                except repo.NotFound:
                    problems.append(f"{claim_id} is not a claim on record for this family")
        policy_no = state.get("policy_no")
        if policy_no is not None and not _POLICY.match(policy_no):
            problems.append(f"{policy_no!r} is not a policy number like POL-HDF-778812")
        elif policy_no is not None and registry is not None and policy_no != registry.policy_no:
            problems.append(f"{claim_id} is on a different policy than {policy_no}")
        if intent not in {i.value for i in WorkflowIntent}:
            problems.append(f"unknown request type {intent!r}")
        if problems:
            reason = "; ".join(problems)
            return Step("finish", f"request rejected: {reason}", StepResult.BLOCKED, inputs=inputs,
                        update={"outcome": "blocked", "blocked_reason": reason})
        assert registry is not None
        return Step("consent_gate", f"{intent} request for {claim_id} on {registry.policy_no} is well formed",
                    inputs=inputs, update={"policy_no": registry.policy_no, "intent": intent})

    # 2 --------------------------------------------------------------------------------
    def consent_gate(state: ClaimState) -> Step:
        run_id, requester, claim_id = state["run_id"], state["requester_id"], state["claim_id"]
        read = cs.check_consent(db, requester, claim_id, ConsentAction.READ_STATUS.value, run_id=run_id)
        consent: dict[str, Any] = {"read": read.public() | {"evidence_id": read.evidence_id}}
        evidence = [read.evidence_id]
        wants_upload = state.get("intent") == WorkflowIntent.UPLOAD.value
        if not read.allowed:
            asked = _request_consent(requester, claim_id, run_id, wants_upload, read)
            consent["request_id"] = asked
            reason = f"{read.result.value}: {read.reason}"
            return Step("finish", f"consent stops the read: {reason}; the portal was not opened", StepResult.BLOCKED,
                        inputs={"claim_id": claim_id, "action": "read_status"},
                        outputs={"decision_id": read.decision_id, "consent_request_id": asked},
                        evidence=evidence, update={"consent": consent, "upload_allowed": False,
                                                   "outcome": "blocked", "blocked_reason": reason})
        upload_allowed = False
        note = "status only: nothing will be uploaded"
        if wants_upload:
            upload = cs.check_consent(db, requester, claim_id, ConsentAction.UPLOAD_DOCS.value, run_id=run_id)
            consent["upload"] = upload.public() | {"evidence_id": upload.evidence_id}
            evidence.append(upload.evidence_id)
            upload_allowed = upload.allowed
            if upload.allowed:
                note = "uploads allowed, each one only after a person approves it"
            else:
                consent["request_id"] = _request_consent(requester, claim_id, run_id, True, upload)
                note = f"uploads not allowed ({upload.result.value}): status only"
        return Step("portal_login", f"read_status ALLOW ({read.reason}); {note}",
                    inputs={"claim_id": claim_id, "intent": state.get("intent")},
                    outputs={"read_decision_id": read.decision_id,
                             "upload_decision_id": consent.get("upload", {}).get("decision_id")},
                    evidence=evidence,
                    update={"consent": consent, "upload_allowed": upload_allowed, "after_login": "read_claim"})

    def _request_consent(requester: str, claim_id: str, run_id: str, upload: bool,
                         decision: cs.ConsentDecision) -> str | None:
        if decision.result not in (ConsentResult.NEEDS_CONSENT, ConsentResult.EXPIRED_OR_REVOKED,
                                   ConsentResult.ACTION_NOT_ALLOWED):
            return None
        wanted = [ConsentAction.READ_STATUS.value] + ([ConsentAction.UPLOAD_DOCS.value] if upload else [])
        try:
            return cs.request_consent(db, requester, claim_id, wanted, run_id=run_id).id
        except cs.ConsentError:
            return None

    # 3 --------------------------------------------------------------------------------
    def portal_login(state: ClaimState) -> Step:
        portal = deps.portal_for(state["requester_id"])
        ps, target = _ps(state), state.get("after_login") or "read_claim"
        fallback = "verify_and_reply" if state.get("raw_claim") else "finish"
        if portal.state is LoginState.AUTHENTICATED:
            ps.update(state="authenticated")
            return Step(target, f"portal already signed in; continuing to {target}", update={"portal_session": ps})
        if portal.state is LoginState.OTP_REQUIRED and not ps.get("new_login"):
            ps.update(state="otp_required")
            return Step("otp_entry", "the portal is waiting for an OTP", update={"portal_session": ps})
        if ps.get("logins", 0) >= MAX_LOGINS:
            ps.update(gave_up=True)
            reason = f"could not sign in to the portal after {MAX_LOGINS} tries"
            return Step(fallback, reason, StepResult.BLOCKED, error=reason,
                        update={"portal_session": ps, **({} if state.get("raw_claim") else
                                                         {"outcome": "blocked", "blocked_reason": reason})})
        ps.update(logins=ps.get("logins", 0) + 1, otp_tries=0, new_login=False)
        try:
            status = portal.login()
        except PortalError as exc:
            ps.update(state="logged_out", last_error=exc.code, gave_up=True)
            reason = f"portal sign-in failed: {exc.code}: {exc.message}"
            result = StepResult.BLOCKED if exc.blocked else StepResult.FETCH_FAILED
            return Step(fallback, reason, result, error=reason,
                        update={"portal_session": ps, "verification_result": {"result": "FETCH_FAILED",
                                                                             "stage": "login", "error": exc.code},
                                **({} if state.get("raw_claim") else {"outcome": "failed", "blocked_reason": reason})})
        if status.state is LoginState.OTP_REQUIRED:
            ps.update(state="otp_required")
            return Step("otp_entry", f"login {ps['logins']}: the portal sent an OTP; a person must type it",
                        update={"portal_session": ps})
        ps.update(state="authenticated")
        return Step(target, "signed in without an OTP", update={"portal_session": ps})

    def otp_entry(state: ClaimState) -> Step:
        portal = deps.portal_for(state["requester_id"])
        ps, target = _ps(state), state.get("after_login") or "read_claim"
        if portal.state is LoginState.AUTHENTICATED:
            ps.update(state="authenticated")
            return Step(target, "portal already signed in", update={"portal_session": ps})
        if portal.state is not LoginState.OTP_REQUIRED:
            ps.update(state="logged_out")
            return Step("portal_login", "the OTP screen is gone (session lost or restarted); signing in again",
                        StepResult.BLOCKED, update={"portal_session": ps})
        answer = interrupt({
            "type": "otp", "run_id": state["run_id"], "claim_id": state["claim_id"],
            "message": "The portal sent an OTP to the registered phone. Type it to continue; ClaimSaathi never reads it.",
            "otp_screen_url": "/api/portal/session/otp-screen", "try": ps.get("otp_tries", 0) + 1,
            "max_tries": MAX_OTP_TRIES, "last_error": ps.get("otp_error"),
        })
        otp = str((answer or {}).get("otp", "")).strip() if isinstance(answer, dict) else ""
        person = answer.get("person_id") if isinstance(answer, dict) else None
        ps["otp_tries"] = ps.get("otp_tries", 0) + 1
        if person != state["requester_id"] or not _OTP.match(otp):
            ps["otp_error"] = "the OTP must be 6 digits, typed by the person who asked"
        else:
            try:
                portal.submit_otp(otp)
                ps.update(state="authenticated", otp_error=None, last_error=None)
                return Step(target, f"OTP accepted (typed by {person}); continuing to {target}",
                            inputs={"person_id": person}, update={"portal_session": ps})
            except OtpRejected as exc:
                ps["otp_error"] = exc.message
            except SessionExpired as exc:
                ps.update(state="logged_out", otp_error=exc.message)
                return Step("portal_login", f"the OTP expired: {exc.message}", StepResult.BLOCKED,
                            inputs={"person_id": person}, update={"portal_session": ps})
            except PortalError as exc:
                ps.update(state="logged_out", gave_up=True, last_error=exc.code)
                reason = f"portal failed during sign-in: {exc.code}"
                fallback = "verify_and_reply" if state.get("raw_claim") else "finish"
                return Step(fallback, reason, StepResult.FETCH_FAILED, error=reason, update={
                    "portal_session": ps, **({} if state.get("raw_claim") else {"outcome": "failed",
                                                                                 "blocked_reason": reason})})
        if ps["otp_tries"] < MAX_OTP_TRIES:
            return Step("otp_entry", f"OTP not accepted ({ps['otp_error']}); asking again", StepResult.BLOCKED,
                        inputs={"person_id": person}, update={"portal_session": ps})
        ps.update(state="logged_out", new_login=True)  # the old OTP screen must not be reused
        return Step("portal_login", f"{MAX_OTP_TRIES} OTP tries used; starting a new login", StepResult.BLOCKED,
                    inputs={"person_id": person}, update={"portal_session": ps})

    # 4 --------------------------------------------------------------------------------
    def read_claim(state: ClaimState) -> Step:
        run_id, requester, claim_id = state["run_id"], state["requester_id"], state["claim_id"]
        portal = deps.portal_for(requester)
        try:
            read = cs.guarded_portal_read(db, requester, claim_id, run_id=run_id,
                                          reader=lambda r, c: portal.read_claim(c, run_id=r))
        except cs.ConsentDenied as denied:
            reason = f"consent changed before the read: {denied.decision.result.value}"
            return Step("finish", reason, StepResult.BLOCKED, evidence=[denied.decision.evidence_id],
                        update={"outcome": "blocked", "blocked_reason": reason})
        except PortalError as exc:
            lost = _session_lost(state, "read_claim", exc)
            if lost:
                return lost
            reason = f"FETCH_FAILED: {exc.code}: {exc.message}"
            return Step("verify_and_reply", f"could not read the claim page ({reason})", StepResult.FETCH_FAILED,
                        evidence=[exc.evidence_id] if exc.evidence_id else [], error=reason,
                        update={"verification_result": {"result": "FETCH_FAILED", "stage": "read",
                                                        "error": exc.code, "evidence_id": exc.evidence_id}})
        snap = read.snapshot.to_json()
        return Step("extract_and_normalise",
                    f"read {claim_id}: '{snap['raw_status']}' as of {snap['status_updated_at']}",
                    inputs={"claim_id": claim_id}, outputs={"evidence_id": read.evidence_id},
                    evidence=[read.evidence_id],
                    update={"raw_claim": {"evidence_id": read.evidence_id, "snapshot": snap},
                            "uploaded_documents": snap["uploaded"]})

    # 5 --------------------------------------------------------------------------------
    def extract_and_normalise(state: ClaimState) -> Step:
        update, step_evidence = _extract(state)
        extracted = update["extracted_claim"]
        pending = [r for r in update["requested_documents"] if is_pending(r.get("state"))]
        status = update["canonical_status"]
        decision = (f"status {status['canonical']} from '{status['raw_status']}' ({status['reason']}); "
                    f"{len(pending)} pending document request(s)")
        nxt = "match_documents" if extracted["valid"] and pending else "verify_and_reply"
        return Step(nxt, decision, inputs={"evidence_id": state["raw_claim"]["evidence_id"]},
                    outputs={"extraction_id": extracted["extraction_id"], "status_decision_id": status["decision_id"]},
                    evidence=step_evidence, update=update,
                    error=None if extracted["valid"] else "extraction invalid: " + "; ".join(extracted["errors"][:3]))

    def _extract(state: ClaimState) -> tuple[dict[str, Any], list[str]]:
        """Validated extraction + deterministic status for the claim read now in state."""
        evidence_id = state["raw_claim"]["evidence_id"]
        out = extract_claim(db, run_id=state["run_id"], evidence_id=evidence_id, claim_id=state["claim_id"],
                            llm=deps.llm)
        raw = out.data.raw_status if out.data else state["raw_claim"]["snapshot"].get("raw_status")
        stamp = out.data.status_stamp if out.data else state["raw_claim"]["snapshot"].get("status_updated_at")
        status = record_status(db, run_id=state["run_id"], claim_id=state["claim_id"], evidence_id=evidence_id,
                               extraction_id=out.extraction_id, extraction_valid=out.valid, raw_status=raw,
                               status_stamp=stamp, extraction_errors=out.errors)
        data = out.data.model_dump(mode="json") if out.data else None
        return {
            "extracted_claim": {"extraction_id": out.extraction_id, "valid": out.valid, "errors": out.errors,
                                "method": out.method, "data": data},
            "canonical_status": {"canonical": status.canonical.value, "raw_status": status.raw_status,
                                 "status_stamp": status.status_stamp, "reason": status.reason,
                                 "decision_id": status.decision_id, "evidence_id": evidence_id},
            "requested_documents": (data or {}).get("requested_docs", []),
        }, [evidence_id]

    # 6 --------------------------------------------------------------------------------
    def match_documents(state: ClaimState) -> Step:
        run_id, requester = state["run_id"], state["requester_id"]
        try:
            report = match_claim(db, run_id=run_id, requester_id=requester, vault_dir=policy.vault_dir,
                                 extraction_id=state["extracted_claim"]["extraction_id"],
                                 pre_admission_days=policy.pre_admission_days,
                                 post_admission_days=policy.post_admission_days)
        except MatcherError as exc:
            return Step("verify_and_reply", f"documents not matched: {exc}", StepResult.SKIPPED, error=str(exc))
        results = [{k: v for k, v in asdict(i).items() if k != "candidates"} | {"result": i.result.value}
                   for i in report.items]
        candidates = [{"requirement_id": i.requirement_id, "vault_file_id": c.vault_file_id, "filename": c.filename,
                       "passed": c.passed, "failures": c.failures, "evidence_id": c.evidence_id}
                      for i in report.items for c in i.candidates]
        counts = ", ".join(f"{r['requirement_id']} {r['result']}" for r in results) or "nothing pending"
        update: dict[str, Any] = {"matching_results": results, "vault_candidates": candidates}
        safe = [i for i in report.items if i.eligible_for_upload]
        # An earlier decision on the same file and requirement stands (the idempotency key is permanent):
        # show it in the summary rather than offering an upload that will never happen.
        existing: list[str] = []
        with db.read() as conn:
            for item in safe:
                found = repo.action_by_binding(conn, state["claim_id"], item.requirement_id, item.file_sha256 or "")
                if found is not None and repo.get_run(conn, found.run_id).requester_id == requester:
                    existing.append(found.id)
        update["action_ids"] = existing
        if state.get("intent") != WorkflowIntent.UPLOAD.value:
            return Step("verify_and_reply", f"{counts}; status question, so nothing is proposed or uploaded",
                        outputs={"decision_ids": [i.decision_id for i in report.items]}, update=update)
        if not state.get("upload_allowed"):
            return Step("verify_and_reply", f"{counts}; consent does not allow uploads, so nothing is proposed",
                        StepResult.BLOCKED, update=update)
        pending: list[str] = []
        for item in safe:  # only MATCHED items; a mismatch is never proposed
            action, _ = propose_upload(db, run_id=run_id, decision_id=item.decision_id)
            with db.read() as conn:
                owner = repo.get_run(conn, action.run_id).requester_id
            if owner == requester and ActionState(action.state) not in TERMINAL_ACTION_STATES:
                pending.append(action.id)
        update["pending_actions"] = pending
        update["action_ids"] = list(dict.fromkeys([*existing, *pending]))
        if not pending:
            done = f"; already decided earlier: {', '.join(existing)}" if existing else ""
            return Step("verify_and_reply", f"{counts}; no new upload to propose{done}", update=update,
                        outputs={"existing_action_ids": existing})
        return Step("human_approval", f"{counts}; {len(pending)} upload(s) proposed, each waits for a person",
                    outputs={"action_ids": pending}, update=update)

    # 7 --------------------------------------------------------------------------------
    def human_approval(state: ClaimState) -> Step:
        pending = list(state.get("pending_actions") or [])
        requester = state["requester_id"]
        if not pending:
            return Step("verify_and_reply", "no upload waiting for a decision", StepResult.SKIPPED)
        action_id, rest = pending[0], pending[1:]
        after = "human_approval" if rest else "verify_and_reply"
        with db.read() as conn:
            action = repo.get_action(conn, action_id)
        st = ActionState(action.state)
        if st in TERMINAL_ACTION_STATES:
            return Step(after, f"{action_id} is already {st.value}", StepResult.SKIPPED,
                        update={"pending_actions": rest})
        if st in (ActionState.APPROVED, ActionState.EXECUTING, ActionState.UNKNOWN, ActionState.PENDING_VERIFY):
            return Step("execute_upload", f"{action_id} is {st.value}: its approval is already recorded",
                        update={"pending_actions": pending})

        card = act.approval_card(db, action_id, person_id=requester, policy=policy)
        proposed = {"action_id": action_id, "card_sha256": card.card_sha256, "card_url": f"/actions/{action_id}",
                    "requirement_id": action.requirement_id, "filename": (card.card.get("file") or {}).get("filename")}
        if st is ActionState.PROPOSED and not card.can_approve:
            failed = "; ".join(c.detail for c in card.checks if not c.ok)
            return Step(after, f"{action_id} cannot be approved: {failed}", StepResult.BLOCKED,
                        update={"pending_actions": rest, "proposed_action": proposed}, error=failed)
        retry = st is ActionState.RETRY_ALLOWED
        answer = interrupt({
            "type": "upload_retry" if retry else "upload_approval", "run_id": state["run_id"],
            "action_id": action_id, "card": card.card, "card_sha256": card.card_sha256,
            "card_url": f"/actions/{action_id}",
            "question": (f"The portal does not have {proposed['filename']} yet. Try the upload again?" if retry else
                         f"Upload {proposed['filename']} for {action.requirement_id} on {action.claim_id}?"),
        })
        answer = answer if isinstance(answer, dict) else {}
        person, choice = answer.get("person_id"), answer.get("decision")
        asks = state.get("approval_asks", 0) + 1
        inputs = {"action_id": action_id, "person_id": person, "decision": choice}

        def ask_again(why: str) -> Step:
            if asks < MAX_APPROVAL_ASKS:
                return Step("human_approval", f"{why}; asking again", StepResult.BLOCKED, inputs=inputs,
                            update={"approval_asks": asks, "proposed_action": proposed})
            return Step(after, f"{why}; stopped asking after {MAX_APPROVAL_ASKS} tries", StepResult.BLOCKED,
                        inputs=inputs, error=why, update={"approval_asks": 0, "pending_actions": rest})

        if person != requester or choice not in ("approve", "reject"):
            return ask_again("the answer must be approve or reject, from the person who asked")
        decided = {"action_id": action_id, "decision": choice, "by": person, "at": utc_now(), "retry": retry}
        if choice == "reject":
            if retry:
                return Step(after, f"{person} chose not to retry {action_id}; it stays RETRY_ALLOWED",
                            inputs=inputs, update={"pending_actions": rest, "approval_decision": decided,
                                                   "approval_asks": 0})
            rejected, _ = act.reject_upload(db, action_id, person_id=person, policy=policy)
            return Step(after, f"{person} rejected {action_id}: nothing will be uploaded", inputs=inputs,
                        outputs={"state": rejected.state},
                        update={"pending_actions": rest, "approval_decision": decided, "approval_asks": 0,
                                "proposed_action": proposed})
        if retry:
            return Step("execute_upload", f"{person} asked to retry {action_id}; every check runs again first",
                        inputs=inputs, update={"approval_decision": decided, "approval_asks": 0})
        try:
            approved, _ = act.approve_upload(db, action_id, person_id=person, card_sha256=str(
                answer.get("card_sha256") or ""), policy=policy)
        except act.CardChanged:
            return ask_again("the card changed since it was shown")
        except (act.NotEligible, cs.ConsentDenied, repo.InvalidTransition) as exc:
            reason = f"{action_id} not approved: {exc}"
            return Step(after, reason, StepResult.BLOCKED, inputs=inputs, error=reason,
                        update={"pending_actions": rest, "approval_asks": 0})
        return Step("execute_upload", f"{person} approved {action_id} (card {card.card_sha256[:12]}…)",
                    inputs=inputs, outputs={"state": approved.state, "approved_at": approved.approved_at},
                    evidence=[card.card["matching_evidence"]["portal_evidence_id"]],
                    update={"approval_decision": decided, "approval_asks": 0, "proposed_action": proposed})

    # 8 --------------------------------------------------------------------------------
    def execute_upload(state: ClaimState) -> Step:
        pending = list(state.get("pending_actions") or [])
        if not pending:
            return Step("verify_and_reply", "no approved upload to run", StepResult.SKIPPED)
        action_id, rest = pending[0], pending[1:]
        after = "human_approval" if rest else "verify_and_reply"
        results = dict(state.get("action_results") or {})
        try:
            report = executor(state["requester_id"]).execute(action_id, requester_id=state["requester_id"])
        except PortalError as exc:
            lost = _session_lost(state, "execute_upload", exc)
            if lost:
                return lost
            results[action_id] = {"state": None, "error": f"{exc.code}: {exc.message}", "uploaded": False}
            return Step(after, f"portal failed before the upload: {exc.code}; nothing was sent",
                        StepResult.FETCH_FAILED, error=exc.message, update={"pending_actions": rest,
                                                                             "action_results": results})
        except (act.ActionError, cs.ConsentDenied) as exc:
            with db.read() as conn:
                state_now = repo.get_action(conn, action_id).state
            results[action_id] = {"state": state_now, "error": str(exc), "uploaded": False}
            return Step(after, f"not uploaded: {exc}", StepResult.BLOCKED, error=str(exc),
                        update={"pending_actions": rest, "action_results": results})
        summary = {"state": report.action.state, "uploaded": report.uploaded, "message": report.message,
                   "path": list(report.path), "verification": report.verification,
                   "attempts": report.action.attempts}
        results[action_id] = summary
        with db.read() as conn:
            evidence = [a.evidence_id for a in repo.attempts_for(conn, action_id)] + \
                       [v.evidence_id for v in repo.verifications_for(conn, action_id)]
        nxt, keep = after, rest
        if report.action.state == ActionState.RETRY_ALLOWED.value:
            nxt, keep = "human_approval", pending  # a person decides on the retry
        return Step(nxt, f"{action_id}: {' -> '.join(report.path)}. {report.message}",
                    inputs={"action_id": action_id}, outputs=summary, evidence=evidence,
                    update={"pending_actions": keep, "action_results": results, "action_result": summary})

    # 9 --------------------------------------------------------------------------------
    def verify_and_reply(state: ClaimState) -> Step:
        requester, claim_id, run_id = state["requester_id"], state["claim_id"], state["run_id"]
        portal = deps.portal_for(requester)
        results = dict(state.get("action_results") or {})
        update: dict[str, Any] = {}
        evidence: list[str] = []
        notes: list[str] = []
        ps = _ps(state)
        needs_portal = bool(results) and state.get("raw_claim") is not None
        if needs_portal and portal.state is not LoginState.AUTHENTICATED and not ps.get("gave_up"):
            ps["state"] = "logged_out"
            return Step("portal_login", "the re-read needs the portal; signing in first", StepResult.BLOCKED,
                        update={"portal_session": ps, "after_login": "verify_and_reply"})

        # An uncertain upload is re-read before anything else; never re-sent from here.
        for action_id in results:
            with db.read() as conn:
                action = repo.get_action(conn, action_id)
            if action.state in (ActionState.UNKNOWN.value, ActionState.PENDING_VERIFY.value,
                                ActionState.EXECUTING.value):
                report = executor(requester).reconcile(action_id, requester_id=requester)
                results[action_id] = {**results[action_id], "state": report.action.state,
                                      "verification": report.verification, "message": report.message}
                notes.append(f"{action_id} re-read: {report.verification or report.action.state}")
        update["action_results"] = results

        # After an upload, read the claim again: status and documents come from that fresh read.
        fetch = (state.get("verification_result") or {}).get("result")
        if needs_portal and fetch != "FETCH_FAILED":
            try:
                read = cs.guarded_portal_read(db, requester, claim_id, run_id=run_id,
                                              reader=lambda r, c: portal.read_claim(c, run_id=r))
                snap = read.snapshot.to_json()
                fresh = {**state, "raw_claim": {"evidence_id": read.evidence_id, "snapshot": snap}}
                extracted, ev = _extract(fresh)  # type: ignore[arg-type]
                update.update(extracted, raw_claim=fresh["raw_claim"], uploaded_documents=snap["uploaded"])
                evidence += ev
                update["verification_result"] = {"result": "REREAD", "evidence_id": read.evidence_id,
                                                 "raw_status": snap["raw_status"],
                                                 "status_updated_at": snap["status_updated_at"]}
                notes.append(f"claim re-read ({read.evidence_id})")
            except (PortalError, cs.ConsentDenied) as exc:
                code = exc.code if isinstance(exc, PortalError) else "consent_denied"
                update["verification_result"] = {"result": "FETCH_FAILED", "stage": "reread", "error": code}
                notes.append(f"re-read failed ({code}); the earlier read is reported, not assumed unchanged")
        merged: dict[str, Any] = {**state, **update}
        final = _final_response(db, merged)  # type: ignore[arg-type]
        evidence += final["evidence"]
        outcome = "failed" if final["fetch"] == "FETCH_FAILED" and not merged.get("canonical_status") else "completed"
        _set_run_state(db, run_id, RunState.FAILED if outcome == "failed" else RunState.COMPLETED,
                       final_status=final["canonical_status"])
        update.update(final_response=final, outcome=outcome)
        return Step(END, f"{final['canonical_status']}: reply guarded ({final['reply_guard']}); next step: "
                    f"{final['next_step']}" + (f"; {'; '.join(notes)}" if notes else ""),
                    StepResult.FETCH_FAILED if final["fetch"] == "FETCH_FAILED" else StepResult.OK,
                    outputs={"reply_guard_decision_id": final["reply_guard_decision_id"]},
                    evidence=sorted(set(evidence)), update=update)

    # end of a blocked run --------------------------------------------------------------
    def finish(state: ClaimState) -> Step:
        final = _blocked_response(db, state)
        outcome = state.get("outcome") or "blocked"
        _set_run_state(db, state["run_id"], RunState.FAILED if outcome == "failed" else RunState.BLOCKED)
        return Step(END, f"stopped ({outcome}): {state.get('blocked_reason') or 'blocked'}",
                    StepResult.BLOCKED if outcome == "blocked" else StepResult.FETCH_FAILED,
                    update={"final_response": final, "outcome": outcome})

    nodes: dict[str, Callable[[ClaimState], Step]] = {
        "intake_validate": intake_validate, "consent_gate": consent_gate, "portal_login": portal_login,
        "otp_entry": otp_entry, "read_claim": read_claim, "extract_and_normalise": extract_and_normalise,
        "match_documents": match_documents, "human_approval": human_approval, "execute_upload": execute_upload,
        "verify_and_reply": verify_and_reply, "finish": finish,
    }
    graph = StateGraph(ClaimState)
    for name, fn in nodes.items():
        graph.add_node(name, _wrap(deps, name, fn))
    graph.add_edge(START, "intake_validate")
    for name, targets in EDGES.items():
        graph.add_conditional_edges(name, lambda s: s["next_node"], {t: t for t in targets})
    return graph


# ---------------- the reply ----------------


def _type_words(doc_type: str | None) -> str:
    return (doc_type or "document").replace("_", " ")


def _next_step(state: ClaimState, actions: dict[str, repo.Action], files: dict[str, str]) -> str:
    """Exactly one next step, by priority. Plain words only (the reply guard checks them)."""
    status = (state.get("canonical_status") or {}).get("canonical")
    if not state.get("canonical_status"):
        return "try again in a while; the portal could not be read, so I cannot say whether anything changed"
    if status == CanonicalStatus.UNCERTAIN.value:
        return "check this claim with the TPA helpline, or ask me to read the portal again later"
    by_state = {a.state: a for a in actions.values()}
    for st, text in ((ActionState.UNKNOWN, "I will read the portal again before any retry of {f}; check back shortly"),
                     (ActionState.PENDING_VERIFY, "I will read the portal again to confirm {f}; check back shortly"),
                     (ActionState.EXECUTING, "an upload of {f} is in progress; check back shortly"),
                     (ActionState.RETRY_ALLOWED, "retry the upload of {f} from the Uploads page when you are ready"),
                     (ActionState.FAILED, "check {r} on the portal yourself; the upload of {f} did not go through"),
                     (ActionState.PROPOSED, "review the upload of {f} for {r} on the Uploads page"),
                     (ActionState.APPROVED, "start the upload of {f} for {r} from the Uploads page")):
        if st.value in by_state:
            a = by_state[st.value]
            return text.format(f=files.get(a.vault_file_id or "", "the file"), r=a.requirement_id)
    for m in state.get("matching_results") or []:
        has_action = any(a.requirement_id == m["requirement_id"] for a in actions.values())
        if m["result"] == "MATCHED" and not has_action:
            if state.get("intent") == WorkflowIntent.UPLOAD.value and not state.get("upload_allowed"):
                return "ask the patient to allow uploads for this claim on the Consent page"
            return (f"ask me to upload {files.get(m.get('vault_file_id') or '', 'the matching file')} for "
                    f"{m['requirement_id']}; you review it before anything is sent")
    for m in state.get("matching_results") or []:
        if m["result"] in ("MISMATCH", "MISSING"):
            return (f"find a {_type_words(m.get('needed_doc_type'))} for {m['requirement_id']} dated within the "
                    "hospital stay and add it to your documents")
    for m in state.get("matching_results") or []:
        if m["result"] == "HOSPITAL_SHOULD_PROVIDE":
            return f"send the drafted note to the hospital TPA desk for {m['requirement_id']}"
    if any(a.state == ActionState.VERIFIED.value for a in actions.values()):
        return "wait for the insurer to review the documents; ask me to check the status again later"
    return {
        CanonicalStatus.QUERY_RAISED.value: "ask the TPA which documents are still needed",
        CanonicalStatus.UNDER_REVIEW.value: "wait for the insurer's decision; ask me to check again later",
        CanonicalStatus.PARTIALLY_APPROVED.value: "ask the insurer which items were not covered and why",
        CanonicalStatus.APPROVED.value: "watch for the payment confirmation from the insurer",
        CanonicalStatus.SETTLED.value: "nothing more is needed for this claim",
        CanonicalStatus.REJECTED.value: "ask the insurer for the written reason and the policy clause",
        CanonicalStatus.CLOSED.value: "ask the insurer why the claim was closed",
    }.get(status or "", "ask me to check again later")


def _document_lines(state: ClaimState, actions: dict[str, repo.Action], files: dict[str, str]) -> list[dict[str, Any]]:
    matches = {m["requirement_id"]: m for m in state.get("matching_results") or []}
    notes = {
        "VERIFIED": "uploaded and confirmed by a fresh read of the portal",
        "UNKNOWN": "upload outcome unknown; the portal will be re-read before any retry",
        "PENDING_VERIFY": "uploaded; waiting for the portal to confirm",
        "EXECUTING": "upload in progress",
        "RETRY_ALLOWED": "not on the portal yet; a retry is available",
        "REJECTED": "you chose not to upload {f}",
        "FAILED": "the upload of {f} did not go through",
        "PROPOSED": "{f} is waiting for your decision",
        "APPROVED": "{f} is ready to upload",
    }
    lines = []
    for req in state.get("requested_documents") or []:
        rid = req["requirement_id"]
        action = next((a for a in actions.values() if a.requirement_id == rid), None)
        match = matches.get(rid)
        if action is not None:
            note = notes[action.state].format(f=files.get(action.vault_file_id or "", "the file"))
        elif match is None:
            note = "" if not is_pending(req.get("state")) else "not checked against your documents"
        elif match["result"] == "MATCHED":
            note = f"you have a suitable file: {files.get(match.get('vault_file_id') or '', 'in your documents')}"
        elif match["result"] == "HOSPITAL_SHOULD_PROVIDE":
            note = "the hospital should provide this; a note to its TPA desk is drafted"
        elif match["result"] == "MISMATCH":
            note = "no suitable file: " + match["reason"]
        else:
            note = "not found in your documents"
        file_id = (action.vault_file_id if action else None) or (match or {}).get("vault_file_id")
        lines.append({"requirement_id": rid, "description": req.get("description"),
                      "type": _type_words(needed_type(req)), "portal_state": req.get("state"),
                      "match": match["result"] if match else None, "action_state": action.state if action else None,
                      "matched_file": files.get(file_id) if file_id else None,
                      "reason": (match or {}).get("reason") or "", "note": note,
                      "next_action": _row_action(state, req, match, action),
                      "action_id": action.id if action else None,
                      "hospital_request": match.get("hospital_request") if match else None})
    return lines


def _row_action(state: ClaimState, req: dict[str, Any], match: dict[str, Any] | None,
                action: repo.Action | None) -> str:
    """What the family can do about one requested document (the run's single next step is separate)."""
    if action is not None:
        return {"VERIFIED": "Nothing: it is on the portal", "UNKNOWN": "Wait: the portal is re-read before any retry",
                "PENDING_VERIFY": "Wait for the portal to confirm", "EXECUTING": "Wait for the upload to finish",
                "RETRY_ALLOWED": "Retry the upload when ready", "REJECTED": "Nothing: you chose not to upload it",
                "FAILED": "Check this request on the portal", "PROPOSED": "Review the upload card",
                "APPROVED": "Start the upload"}[action.state]
    if not is_pending(req.get("state")):
        return "Nothing: the portal has it"
    if match is None:
        return "Ask me to check your documents"
    if match["result"] == "MATCHED":
        if state.get("intent") == WorkflowIntent.UPLOAD.value and not state.get("upload_allowed"):
            return "Ask the patient to allow uploads"
        return "Ask me to upload this file"
    if match["result"] == "HOSPITAL_SHOULD_PROVIDE":
        return "Send the drafted note to the hospital"
    return "Add a suitable file to your documents"


def _final_response(db: Database, state: ClaimState) -> dict[str, Any]:
    """Concise, evidence-backed summary. The free text is the guarded reply; the rest is structured."""
    claim_id = state["claim_id"]
    status = state.get("canonical_status")
    verification = state.get("verification_result") or {}
    fetch = "FETCH_FAILED" if verification.get("result") == "FETCH_FAILED" else "OK"
    with db.read() as conn:
        actions = {aid: repo.get_action(conn, aid) for aid in (state.get("action_results") or {})}
        for p in [*(state.get("action_ids") or []), *(state.get("pending_actions") or [])]:
            actions.setdefault(p, repo.get_action(conn, p))
        files = {vf.id: vf.original_name for vf in repo.vault_files_for(conn, state["requester_id"])}
        patient = repo.get_person(conn, repo.get_claim_record(conn, claim_id).patient_id).name
    documents = _document_lines(state, actions, files)
    next_step = _next_step(state, actions, files)
    extracted = (state.get("extracted_claim") or {}).get("data") or {}
    missing = [f"{d['requirement_id']} ({d['type']})" for d in documents
               if is_pending(d["portal_state"]) and d["action_state"] != ActionState.VERIFIED.value]
    run_id = state["run_id"]
    if status is None:  # nothing was read: say so, never "no change"
        with db.transaction() as conn:
            decision = repo.record_decision(
                conn, run_id=run_id, kind="status", result=CanonicalStatus.UNCERTAIN.value, subject_ref=claim_id,
                reason="FETCH_FAILED: the claim page could not be read", evidence_state="missing",
                input_refs={"verification": verification})
        status = {"canonical": CanonicalStatus.UNCERTAIN.value, "raw_status": None, "status_stamp": None,
                  "reason": "the portal could not be read (FETCH_FAILED)", "decision_id": decision.id,
                  "evidence_id": None}
    canonical = CanonicalStatus(status["canonical"])
    valid = bool((state.get("extracted_claim") or {}).get("valid"))
    facts = ReplyFacts(
        claim_id=claim_id, patient_name=patient, canonical=canonical, raw_status=status.get("raw_status"),
        status_stamp=status.get("status_stamp"), extraction_valid=valid,
        preauth_status=extracted.get("preauth_status"), preauth_amount=extracted.get("preauth_amount"),
        missing_items=missing, next_step=next_step,
        uncertainty_reason=None if valid else (
            "the portal could not be read (FETCH_FAILED), so I cannot say whether anything changed"
            if fetch == "FETCH_FAILED" and state.get("raw_claim") is None
            else "the claim page could not be read reliably"),
    )
    reply_evidence = status.get("evidence_id")
    try:
        reply = compose_reply(db, facts, run_id=run_id, evidence_id=reply_evidence, llm=None)
        text, guard, guard_id = reply.text, "PASSED" if reply.guard.passed else "BLOCKED", reply.decision_id
    except RuntimeError as exc:  # the template failed its own guard: say less, never something unchecked
        logger.error("reply template blocked", extra={"run_id": run_id, "error": str(exc)})
        safe = ReplyFacts(claim_id=claim_id, patient_name=None, canonical=CanonicalStatus.UNCERTAIN,
                          raw_status=None, status_stamp=None, extraction_valid=False,
                          uncertainty_reason="the summary could not be checked safely")
        text, guard, guard_id = render_template(safe), "BLOCKED", None
    if fetch == "FETCH_FAILED" and state.get("raw_claim") is not None:
        text += (" I could not read the portal again after that, so this is what it showed at "
                 "the earlier read, not a confirmation that nothing changed.")
    evidence = [e for e in (
        (state.get("raw_claim") or {}).get("evidence_id"), status.get("evidence_id"), verification.get("evidence_id"),
        *[c.get("evidence_id") for c in (state.get("consent") or {}).values() if isinstance(c, dict)],
    ) if e]
    with db.read() as conn:
        for aid in actions:
            evidence += [v.evidence_id for v in repo.verifications_for(conn, aid) if v.evidence_id]
    evidence = list(dict.fromkeys(evidence))
    lines = [text, ""] + [f"- {d['requirement_id']} {d['type']}: portal says {d['portal_state']}"
                          + (f"; {d['note']}" if d["note"] else "") for d in documents]
    lines += ["", f"Next step: {next_step}.", "Evidence: " + (", ".join(evidence) or "none")]
    preauth = None
    if extracted.get("preauth_status"):
        preauth = {"status": extracted["preauth_status"], "amount_inr": extracted.get("preauth_amount"),
                   "note": "Pre-authorisation is the hospital's initial cashless go-ahead, not the final claim decision."}
    return {
        "run_id": run_id, "claim_id": claim_id, "patient_name": patient, "policy_no": state.get("policy_no"),
        "canonical_status": canonical.value, "raw_status": status.get("raw_status"),
        "status_stamp": status.get("status_stamp"), "status_reason": status.get("reason"), "preauth": preauth,
        "documents": documents, "next_step": next_step, "evidence": evidence, "fetch": fetch,
        "consent": _consent_summary(state), "text": text, "summary_text": "\n".join(lines).strip(),
        "reply_guard": guard, "reply_guard_decision_id": guard_id, "outcome": "completed",
    }


def _consent_summary(state: ClaimState) -> dict[str, Any]:
    consent = state.get("consent") or {}
    return {"read": (consent.get("read") or {}).get("result"), "read_reason": (consent.get("read") or {}).get("reason"),
            "upload": (consent.get("upload") or {}).get("result"),
            "upload_reason": (consent.get("upload") or {}).get("reason"), "request_id": consent.get("request_id")}


def _blocked_response(db: Database, state: ClaimState) -> dict[str, Any]:
    """The reply when the run stops early. It never says anything about the claim's status."""
    claim_id, reason = state.get("claim_id") or "", state.get("blocked_reason") or "the request was stopped"
    consent = state.get("consent") or {}
    request_id = consent.get("request_id")
    patient = None
    with db.read() as conn:
        try:
            patient = repo.get_person(conn, repo.get_claim_record(conn, claim_id).patient_id).name
        except repo.NotFound:
            patient = None
    if consent.get("read") and consent["read"]["result"] != "ALLOW":
        text = f"I have not looked at claim {claim_id}: {consent['read']['reason']}. The portal was not opened."
        next_step = (f"{patient or 'the patient'} can allow access on the Consent page (request {request_id})"
                     if request_id else "ask the patient to give you access on the Consent page")
    elif (state.get("outcome") or "") == "failed":
        text = f"I could not reach the portal for claim {claim_id} ({reason}). Nothing was read, so I cannot say " \
               "whether anything changed."
        next_step = "try again in a while"
    else:
        text = f"I could not continue with claim {claim_id or '(none)'}: {reason}."
        next_step = "check the claim id and try again" if "claim id" in reason or "not a claim" in reason \
            else "sign in to the portal and try again"
    evidence = [c.get("evidence_id") for c in consent.values() if isinstance(c, dict) and c.get("evidence_id")]
    return {"run_id": state.get("run_id"), "claim_id": claim_id, "patient_name": patient,
            "policy_no": state.get("policy_no"), "canonical_status": None, "raw_status": None, "status_stamp": None,
            "preauth": None, "consent": _consent_summary(state), "documents": [], "next_step": next_step, "evidence": evidence, "fetch": "NOT_ATTEMPTED"
            if state.get("outcome") != "failed" else "FETCH_FAILED", "text": text,
            "summary_text": f"{text}\n\nNext step: {next_step}.\nEvidence: {', '.join(evidence) or 'none'}",
            "consent_request_id": request_id, "outcome": state.get("outcome") or "blocked",
            "blocked_reason": reason, "reply_guard": None}


# ---------------- running, interrupting and resuming ----------------


class WorkflowError(Exception):
    code, http_status = "workflow_error", 409


class IntakeRejected(WorkflowError):
    code, http_status = "invalid_request", 400


class NotYourRun(WorkflowError):
    code, http_status = "not_found", 404


class NotWaiting(WorkflowError):
    """A resume arrived for a run that is not waiting for that input (e.g. a repeated approval)."""

    code = "not_waiting"


@dataclass(frozen=True)
class WorkflowView:
    """What the UI shows: where the run is, what it waits for, every step, and the reply."""

    run_id: str
    claim_id: str
    requester_id: str
    run_state: str
    status: str            # waiting_for_otp | waiting_for_approval | waiting_for_retry | running | completed | blocked | failed
    waiting: dict[str, Any] | None
    next_nodes: tuple[str, ...]
    steps: list[dict[str, Any]]
    final_response: dict[str, Any] | None
    errors: list[str]
    intent: str | None
    portal_state: str      # logged_out | otp_required | authenticated, for the OTP panel

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_WAIT_STATUS = {"otp": "waiting_for_otp", "upload_approval": "waiting_for_approval", "upload_retry": "waiting_for_retry"}


class ClaimWorkflow:
    """Starts, resumes and shows workflow runs. The thread id is the run id; checkpoints live in SQLite."""

    def __init__(self, deps: WorkflowDeps, *, checkpointer: Any = None) -> None:
        self.deps = deps
        self._conn: sqlite3.Connection | None = None
        if checkpointer is None:
            self._conn = sqlite3.connect(deps.db.path, check_same_thread=False, timeout=10)
            checkpointer = SqliteSaver(self._conn)
            checkpointer.setup()  # LangGraph's own tables (checkpoints, writes) in the agent database
        self.graph = build_graph(deps).compile(checkpointer=checkpointer)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- entry points ----

    def start(self, requester_id: str, claim_id: str, *, intent: str = WorkflowIntent.STATUS.value,
              policy_no: str | None = None, test_case: str | None = None) -> WorkflowView:
        """Start a run (a new thread) and drive it until it finishes or waits for a person."""
        if not _CLAIM_ID.match(claim_id or ""):
            raise IntakeRejected(f"{claim_id!r} is not a claim id like CLM-2026-0142")
        try:
            run = cs.start_run(self.deps.db, requester_id, claim_id, test_case=test_case)
        except cs.UnknownRequester as exc:
            raise IntakeRejected(str(exc)) from exc
        initial: ClaimState = {"run_id": run.id, "requester_id": requester_id, "claim_id": claim_id,
                               "policy_no": policy_no, "intent": intent, "evidence_ids": [], "errors": [],
                               "pending_actions": [], "action_results": {}}
        logger.info("workflow started", extra={"run_id": run.id, "claim_id": claim_id, "intent": intent})
        return self._invoke(run.id, initial)

    def resume(self, run_id: str, *, person_id: str, answer: dict[str, Any]) -> WorkflowView:
        """Give a waiting run what it asked for: {"otp"} or {"decision": approve|reject, "card_sha256"}."""
        self._owned(run_id, person_id)
        with self._lock(run_id):
            waiting = self._waiting(run_id)
            if waiting is None:
                raise NotWaiting(f"run {run_id} is not waiting for input")
            kind = waiting.get("type")
            if kind == "otp" and "otp" not in answer:
                raise NotWaiting("this run is waiting for the portal OTP")
            if kind in ("upload_approval", "upload_retry") and "decision" not in answer:
                raise NotWaiting("this run is waiting for an approve or reject decision")
            if kind in ("upload_approval", "upload_retry") and answer.get("action_id") not in (None,
                                                                                             waiting.get("action_id")):
                raise NotWaiting("that decision is for a different upload than the one waiting")
            resume = {k: answer.get(k) for k in ("otp", "decision", "card_sha256")} | {"person_id": person_id}
            return self._run(run_id, Command(resume=resume))

    def continue_run(self, run_id: str, *, person_id: str) -> WorkflowView:
        """Recover a run that stopped mid-node (crash or restart): re-run from its last checkpoint.
        A run waiting for a person, or finished, is returned as it is."""
        self._owned(run_id, person_id)
        with self._lock(run_id):
            snapshot = self.graph.get_state(self._config(run_id))
            if not snapshot.next or self._waiting(run_id) is not None:
                return self.view(run_id)
            return self._run(run_id, None)

    def view(self, run_id: str, *, person_id: str | None = None) -> WorkflowView:
        if person_id is not None:
            self._owned(run_id, person_id)
        snapshot = self.graph.get_state(self._config(run_id))
        values: dict[str, Any] = dict(snapshot.values or {})
        with self.deps.db.read() as conn:
            run = repo.get_run(conn, run_id)
            steps = [asdict(s) for s in repo.steps_for_run(conn, run_id)]
        waiting = self._waiting(run_id)
        if waiting is not None:
            status = _WAIT_STATUS.get(waiting.get("type", ""), "waiting")
        elif snapshot.next:
            status = "running"
        else:
            status = values.get("outcome") or {"completed": "completed", "blocked": "blocked",
                                                "failed": "failed"}.get(run.state, run.state)
        return WorkflowView(run_id=run_id, claim_id=run.claim_id, requester_id=run.requester_id, run_state=run.state,
                            status=status, waiting=waiting, next_nodes=tuple(snapshot.next or ()), steps=steps,
                            final_response=values.get("final_response"), errors=list(values.get("errors") or []),
                            intent=values.get("intent"),
                            portal_state=self.deps.portal_for(run.requester_id).state.value)

    # ---- helpers ----

    def _config(self, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _owned(self, run_id: str, person_id: str) -> repo.Run:
        with self.deps.db.read() as conn:
            try:
                run = repo.get_run(conn, run_id)
            except repo.NotFound as exc:
                raise NotYourRun(f"no run {run_id} for you") from exc
        if run.requester_id != person_id:
            raise NotYourRun(f"no run {run_id} for you")
        return run

    def _waiting(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.graph.get_state(self._config(run_id))
        for item in snapshot.interrupts or ():
            if isinstance(item.value, dict):
                return item.value
        return None

    def _invoke(self, run_id: str, payload: Any) -> WorkflowView:
        with self._lock(run_id):
            return self._run(run_id, payload)

    def _run(self, run_id: str, payload: Any) -> WorkflowView:
        self.graph.invoke(payload, self._config(run_id))
        return self.view(run_id)


def workflow_from_settings(settings: Any, db: Database, portal_for: Callable[[str], WorkflowPortal], *,
                           llm: LlmClient | None = None) -> ClaimWorkflow:
    """The configured workflow (used by the web app and the CLI)."""
    return ClaimWorkflow(WorkflowDeps(db=db, portal_for=portal_for, policy=act.UploadPolicy.from_settings(settings),
                                      llm=llm))
