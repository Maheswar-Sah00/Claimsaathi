"""Domain vocabulary shared across the agent.

These enums are the single source of truth for the database CHECK constraints
(see `agent.migrations`) and the repository's validation. Changing a value
later needs a new migration, not an edit to migration 1.
"""

from __future__ import annotations

from enum import Enum


class CanonicalStatus(str, Enum):
    """Normalised claim status. Unknown portal text maps to UNCERTAIN, never upward."""

    QUERY_RAISED = "QUERY_RAISED"
    UNDER_REVIEW = "UNDER_REVIEW"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    APPROVED = "APPROVED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"
    UNCERTAIN = "UNCERTAIN"


class ConsentAction(str, Enum):
    """Actions a consent artefact can allow."""

    READ_STATUS = "read_status"
    UPLOAD_DOCS = "upload_docs"


class PersonRole(str, Enum):
    """Role of a person in the demo family."""

    PATIENT = "patient"
    FAMILY = "family"


class RunState(str, Enum):
    """Overall state of one agent run. The last three are terminal."""

    CREATED = "created"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_RUN_STATES = frozenset({RunState.BLOCKED, RunState.COMPLETED, RunState.FAILED})


class EvidenceKind(str, Enum):
    """What an evidence record captured."""

    PORTAL_PAGE = "portal_page"
    DOCUMENT_LIST = "document_list"
    UPLOAD_RESPONSE = "upload_response"
    VAULT_EXTRACTION = "vault_extraction"
    CONSENT_RECORD = "consent_record"
    HUMAN_INPUT = "human_input"


class ExtractionSource(str, Enum):
    """Who produced extracted fields. Model output is evidence, never a decision."""

    RULE = "rule"
    MODEL = "model"
    HUMAN = "human"
    SEED_LABEL = "seed_label"


class DecisionKind(str, Enum):
    """Kinds of recorded decisions."""

    CONSENT = "consent"
    STATUS = "status"
    MATCH = "match"
    VERIFICATION = "verification"
    REPLY_GUARD = "reply_guard"


class DecidedBy(str, Enum):
    """Only deterministic rules or a human may make a decision; a model may not."""

    RULE = "rule"
    HUMAN = "human"


class EvidenceState(str, Enum):
    """Audit convention: every decision says whether it has evidence, and if not, why."""

    LINKED = "linked"                 # evidence_id points at the supporting record
    MISSING = "missing"               # evidence was needed but could not be obtained
    NOT_APPLICABLE = "not_applicable" # the decision needs no portal evidence (e.g. a consent rule)


class ConsentResult(str, Enum):
    """Outcomes of the consent gate."""

    ALLOW = "ALLOW"
    NEEDS_CONSENT = "NEEDS_CONSENT"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    EXPIRED_OR_REVOKED = "EXPIRED_OR_REVOKED"


class ConsentRequestStatus(str, Enum):
    """Lifecycle of a request asking a patient for consent."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ClaimSource(str, Enum):
    """Where a claim registry entry came from."""

    SEED = "seed"
    PORTAL_LIST = "portal_list"


class MatchResult(str, Enum):
    """Outcome of matching one requested document against the family vault."""

    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    HOSPITAL_SHOULD_PROVIDE = "HOSPITAL_SHOULD_PROVIDE"


class VerificationResult(str, Enum):
    """Outcome of re-reading the portal after an action."""

    VERIFIED = "VERIFIED"
    NOT_FOUND = "NOT_FOUND"
    MISMATCH = "MISMATCH"
    FETCH_FAILED = "FETCH_FAILED"


class GuardResult(str, Enum):
    """Outcome of the reply guard."""

    PASSED = "PASSED"
    BLOCKED = "BLOCKED"


ALLOWED_RESULTS: dict[DecisionKind, frozenset[str]] = {
    DecisionKind.CONSENT: frozenset(r.value for r in ConsentResult),
    DecisionKind.STATUS: frozenset(s.value for s in CanonicalStatus),
    DecisionKind.MATCH: frozenset(m.value for m in MatchResult),
    DecisionKind.VERIFICATION: frozenset(v.value for v in VerificationResult),
    DecisionKind.REPLY_GUARD: frozenset(g.value for g in GuardResult),
}


class ActionState(str, Enum):
    """Upload action lifecycle. A timeout is UNKNOWN: the server may have stored the file."""

    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    PENDING_VERIFY = "PENDING_VERIFY"
    UNKNOWN = "UNKNOWN"
    RETRY_ALLOWED = "RETRY_ALLOWED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


# Allowed moves. Anything else is refused by the repository. APPROVED / RETRY_ALLOWED -> VERIFIED is
# only for a fresh re-read showing the exact file already listed under the requirement (nothing sent).
ACTION_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PROPOSED: frozenset({ActionState.APPROVED, ActionState.REJECTED}),
    ActionState.APPROVED: frozenset({ActionState.EXECUTING, ActionState.VERIFIED}),
    ActionState.EXECUTING: frozenset({ActionState.PENDING_VERIFY, ActionState.UNKNOWN, ActionState.FAILED}),
    ActionState.PENDING_VERIFY: frozenset({ActionState.VERIFIED, ActionState.FAILED}),
    ActionState.UNKNOWN: frozenset({ActionState.VERIFIED, ActionState.RETRY_ALLOWED, ActionState.FAILED}),
    ActionState.RETRY_ALLOWED: frozenset({ActionState.EXECUTING, ActionState.VERIFIED}),
    ActionState.REJECTED: frozenset(),
    ActionState.VERIFIED: frozenset(),
    ActionState.FAILED: frozenset(),
}

TERMINAL_ACTION_STATES = frozenset({ActionState.REJECTED, ActionState.VERIFIED, ActionState.FAILED})


class WorkflowIntent(str, Enum):
    """What the person asked the workflow for. Only UPLOAD can ever lead to an upload."""

    STATUS = "status"
    UPLOAD = "upload"


class StepResult(str, Enum):
    """Outcome of one workflow node execution, as shown to the person."""

    OK = "OK"
    BLOCKED = "BLOCKED"          # a safety rule stopped the run (or this branch of it)
    INTERRUPTED = "INTERRUPTED"  # waiting for a person (OTP or upload approval)
    FETCH_FAILED = "FETCH_FAILED"
    FAILED = "FAILED"            # the node raised; the run can be continued from its last checkpoint
    SKIPPED = "SKIPPED"


class ApprovalDecision(str, Enum):
    """What a person decided about one proposed upload."""

    APPROVED = "approved"
    REJECTED = "rejected"


class CheckPhase(str, Enum):
    """When an action's eligibility was checked."""

    APPROVAL = "approval"  # when the person approved the card
    EXECUTE = "execute"    # immediately before an upload attempt (first or retry)


class AttemptOutcome(str, Enum):
    """What one upload attempt got back. Only a later re-read decides whether the file is stored."""

    ACCEPTED = "accepted"        # confirmation page showed our hash
    REJECTED = "rejected"        # portal error page (HTTP status recorded)
    TIMEOUT = "timeout"          # no response within the bound: may or may not be stored
    UNCONFIRMED = "unconfirmed"  # a response, but no confirmation or error
    ERROR = "error"              # the request failed or could not be sent
    ABANDONED = "abandoned"      # no result was ever recorded (process stopped mid-attempt)
