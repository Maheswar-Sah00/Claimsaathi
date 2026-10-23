"""Create, read and update records. Every function takes an open connection, so callers
compose several calls inside one `Database.transaction()`.

Validation happens here before SQL; the schema's CHECKs and triggers are the second
line of defence. Logs carry ids and kinds only, never extracted content or file bytes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from agent.db import DatabaseError, utc_now
from agent.models import (
    ACTION_TRANSITIONS,
    ALLOWED_RESULTS,
    TERMINAL_RUN_STATES,
    ActionState,
    ApprovalDecision,
    AttemptOutcome,
    CanonicalStatus,
    CheckPhase,
    ClaimSource,
    ConsentAction,
    ConsentRequestStatus,
    DecidedBy,
    DecisionKind,
    EvidenceKind,
    EvidenceState,
    ExtractionSource,
    PersonRole,
    RunState,
    StepResult,
)

logger = logging.getLogger("agent.repository")

MAX_JSON_BYTES = 256 * 1024
_CLAIM_ID = re.compile(r"^CLM-\d{4}-\d{4}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REQ_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SQLITE_MESSAGES = (
    ("UNIQUE constraint failed: actions.idem_key", "an action with this idempotency key already exists"),
    ("UNIQUE constraint failed: actions.claim_id, actions.requirement_id, actions.file_sha256",
     "an action for this claim, requirement and file already exists"),
    ("UNIQUE constraint failed: action_attempts.action_id", "an upload attempt for this action is already in flight"),
    ("FOREIGN KEY constraint failed", "a referenced record does not exist, or the record is still referenced"),
)


# ---------------- errors ----------------


class RepositoryError(DatabaseError):
    """Base class for repository errors."""


class NotFound(RepositoryError):
    """The requested record does not exist."""


class ValidationError(RepositoryError):
    """Input was rejected before reaching the database."""


class ConstraintViolation(RepositoryError):
    """The database refused the write (uniqueness, foreign key, CHECK or trigger)."""


class InvalidTransition(RepositoryError):
    """An action or run state change that the lifecycle does not allow."""


def _integrity(exc: sqlite3.IntegrityError, what: str) -> ConstraintViolation:
    raw = str(exc)
    for needle, friendly in _SQLITE_MESSAGES:
        if needle in raw:
            return ConstraintViolation(f"{what}: {friendly} ({raw})")
    return ConstraintViolation(f"{what}: {raw}")


def _execute(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], what: str) -> sqlite3.Cursor:
    try:
        return conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise _integrity(exc, what) from exc


def new_id(prefix: str) -> str:
    """Stable random primary key, e.g. RUN-3f9c1a2b7d4e."""
    return f"{prefix}-{secrets.token_hex(6)}"


# ---------------- validation helpers ----------------


def canonical_json(value: Any, *, expect: type | tuple[type, ...] = (dict, list)) -> str:
    """Validate and serialise JSON deterministically. Strings are parsed first.

    Rejects NaN/Infinity, non-string keys, unserialisable values and payloads over 256 KB.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, expect):
        raise ValidationError(f"JSON must be {expect}, got {type(value).__name__}")
    _check_string_keys(value)
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value is not valid JSON: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValidationError("JSON payload larger than 256 KB")
    return text


def _check_string_keys(value: Any) -> None:
    """json.dumps would silently turn 1 into "1"; refuse instead so stored data matches the input."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"JSON object keys must be strings, got {type(key).__name__}")
            _check_string_keys(item)
    elif isinstance(value, list):
        for item in value:
            _check_string_keys(item)


def _check_claim_id(claim_id: str) -> None:
    if not _CLAIM_ID.match(claim_id):
        raise ValidationError(f"claim id must look like CLM-2026-0142, got {claim_id!r}")


def _check_requirement_id(requirement_id: str) -> None:
    if not isinstance(requirement_id, str) or not _REQ_ID.match(requirement_id):
        raise ValidationError(f"requirement id must be 1-64 letters, digits or hyphens, got {requirement_id!r}")


def _check_hex64(value: str, field: str) -> None:
    if not _HEX64.match(value):
        raise ValidationError(f"{field} must be a lowercase 64-character hex SHA-256")


def safe_relative_path(path: str) -> str:
    """Normalise a storage path. Must be relative, inside its root, with no parent segments."""
    candidate = path.replace("\\", "/").strip()
    pure = PurePosixPath(candidate)
    if (
        not candidate
        or pure.is_absolute()
        or ":" in candidate
        or any(part in ("..", "") for part in candidate.split("/"))
        or len(candidate) > 255
    ):
        raise ValidationError(f"unsafe storage path: {path!r}")
    return str(pure)


def _enum(enum: type, value: Any, field: str) -> str:
    try:
        return enum(value).value
    except ValueError as exc:
        allowed = ", ".join(e.value for e in enum)
        raise ValidationError(f"{field} must be one of: {allowed}; got {value!r}") from exc


# ---------------- records ----------------


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    contact_ref: str
    portal_member_id: str | None
    created_at: str


@dataclass(frozen=True)
class Consent:
    id: str
    patient_id: str
    grantee_id: str
    policy_no: str
    claim_id: str
    actions: tuple[str, ...]
    granted_at: str
    expires_at: str
    revoked: bool
    revoked_at: str | None

    def covers(self, claim_id: str, action: str, at: str) -> bool:
        """True if this consent allows `action` on `claim_id` at time `at` (UTC string)."""
        return (
            not self.revoked
            and self.granted_at <= at < self.expires_at
            and self.claim_id in ("*", claim_id)
            and action in self.actions
        )


@dataclass(frozen=True)
class Run:
    id: str
    requester_id: str
    claim_id: str
    state: str
    final_status: str | None
    test_case: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class Evidence:
    id: str
    run_id: str
    kind: str
    url: str
    fetched_at: str
    html_sha256: str | None
    screenshot_path: str | None
    extracted: Any
    extraction_source: str | None


@dataclass(frozen=True)
class VaultFile:
    id: str
    owner_id: str
    storage_path: str
    original_name: str
    sha256: str
    size_bytes: int
    content_type: str
    doc_type: str | None
    patient_name: str | None
    doc_date: str | None
    invoice_no: str | None
    hospital: str | None
    amount_inr: int | None
    legible: bool | None
    extraction_source: str | None
    added_at: str


@dataclass(frozen=True)
class Decision:
    id: str
    run_id: str
    kind: str
    subject_ref: str | None
    input_refs: dict[str, Any]
    result: str
    reason: str
    evidence_id: str | None
    evidence_state: str
    decided_by: str
    created_at: str


@dataclass(frozen=True)
class Action:
    id: str
    idem_key: str
    run_id: str
    claim_id: str
    requirement_id: str
    file_sha256: str
    vault_file_id: str | None
    state: str
    approved_by: str | None
    approved_at: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    match_decision_id: str | None = None


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], what: str) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NotFound(what)
    return row


# ---------------- people ----------------


def create_person(conn: sqlite3.Connection, *, id: str, name: str, role: str, contact_ref: str,
                  portal_member_id: str | None = None, created_at: str | None = None) -> Person:
    """Insert a person. `contact_ref` must be a synthetic identifier (`synthetic:...`)."""
    if not contact_ref.startswith("synthetic:"):
        raise ValidationError("contact_ref must be a synthetic identifier like 'synthetic:ph-ramesh'")
    _execute(
        conn,
        "INSERT INTO people (id, name, role, contact_ref, portal_member_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (id, name, _enum(PersonRole, role, "role"), contact_ref, portal_member_id, created_at or utc_now()),
        f"create person {id}",
    )
    return get_person(conn, id)


def get_person(conn: sqlite3.Connection, person_id: str) -> Person:
    """Fetch a person or raise NotFound."""
    return Person(**dict(_one(conn, "SELECT * FROM people WHERE id = ?", (person_id,), f"person {person_id}")))


def list_people(conn: sqlite3.Connection) -> list[Person]:
    """All people, ordered by id."""
    return [Person(**dict(r)) for r in conn.execute("SELECT * FROM people ORDER BY id")]


# ---------------- consents ----------------


def _consent(row: sqlite3.Row) -> Consent:
    data = dict(row)
    data["actions"] = tuple(json.loads(data["actions"]))
    data["revoked"] = bool(data["revoked"])
    return Consent(**data)


def create_consent(conn: sqlite3.Connection, *, patient_id: str, grantee_id: str, policy_no: str,
                   claim_id: str, actions: list[str], granted_at: str, expires_at: str,
                   id: str | None = None) -> Consent:
    """Record a consent artefact. `claim_id` may be '*' for all of the patient's claims."""
    if claim_id != "*":
        _check_claim_id(claim_id)
    if not actions:
        raise ValidationError("a consent must allow at least one action")
    clean = sorted({_enum(ConsentAction, a, "consent action") for a in actions})
    consent_id = id or new_id("CON")
    _execute(
        conn,
        "INSERT INTO consents (id, patient_id, grantee_id, policy_no, claim_id, actions, granted_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (consent_id, patient_id, grantee_id, policy_no, claim_id, canonical_json(clean, expect=list),
         granted_at, expires_at),
        f"create consent {consent_id}",
    )
    logger.info("consent recorded", extra={"consent_id": consent_id, "patient_id": patient_id,
                                           "grantee_id": grantee_id})
    return get_consent(conn, consent_id)


def get_consent(conn: sqlite3.Connection, consent_id: str) -> Consent:
    """Fetch a consent or raise NotFound."""
    return _consent(_one(conn, "SELECT * FROM consents WHERE id = ?", (consent_id,), f"consent {consent_id}"))


def consents_between(conn: sqlite3.Connection, grantee_id: str, patient_id: str) -> list[Consent]:
    """Every consent (active or not) from a patient to a grantee, newest first."""
    rows = conn.execute(
        "SELECT * FROM consents WHERE grantee_id = ? AND patient_id = ? ORDER BY granted_at DESC, id",
        (grantee_id, patient_id),
    )
    return [_consent(r) for r in rows]


def revoke_consent(conn: sqlite3.Connection, consent_id: str, at: str | None = None) -> Consent:
    """Mark a consent revoked. Revoking twice is refused."""
    consent = get_consent(conn, consent_id)
    if consent.revoked:
        raise InvalidTransition(f"consent {consent_id} is already revoked")
    conn.execute("UPDATE consents SET revoked = 1, revoked_at = ? WHERE id = ?", (at or utc_now(), consent_id))
    logger.info("consent revoked", extra={"consent_id": consent_id})
    return get_consent(conn, consent_id)


# ---------------- runs ----------------


def create_run(conn: sqlite3.Connection, *, requester_id: str, claim_id: str, test_case: str | None = None,
               started_at: str | None = None) -> Run:
    """Start a run in state `created`."""
    _check_claim_id(claim_id)
    run_id = new_id("RUN")
    _execute(
        conn,
        "INSERT INTO runs (id, requester_id, claim_id, test_case, started_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, requester_id, claim_id, test_case, started_at or utc_now()),
        f"create run for {claim_id}",
    )
    logger.info("run created", extra={"run_id": run_id, "claim_id": claim_id, "test_case": test_case})
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: str) -> Run:
    """Fetch a run or raise NotFound."""
    return Run(**dict(_one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,), f"run {run_id}")))


def update_run(conn: sqlite3.Connection, run_id: str, *, state: str,
               final_status: str | None = None) -> Run:
    """Move a run to a new state. Terminal states stamp `finished_at` and cannot be left."""
    run = get_run(conn, run_id)
    new_state = RunState(_enum(RunState, state, "run state"))
    if RunState(run.state) in TERMINAL_RUN_STATES:
        raise InvalidTransition(f"run {run_id} is already {run.state}")
    status = _enum(CanonicalStatus, final_status, "final status") if final_status is not None else run.final_status
    finished = utc_now() if new_state in TERMINAL_RUN_STATES else None
    conn.execute(
        "UPDATE runs SET state = ?, final_status = ?, finished_at = ? WHERE id = ?",
        (new_state.value, status, finished, run_id),
    )
    logger.info("run updated", extra={"run_id": run_id, "state": new_state.value, "final_status": status})
    return get_run(conn, run_id)


# ---------------- evidence ----------------


def _evidence(row: sqlite3.Row) -> Evidence:
    data = dict(row)
    raw = data.pop("extracted_json")
    data["extracted"] = json.loads(raw) if raw is not None else None
    return Evidence(**data)


def add_evidence(conn: sqlite3.Connection, *, run_id: str, kind: str, url: str,
                 html_sha256: str | None = None, screenshot_path: str | None = None,
                 extracted: Any = None, extraction_source: str | None = None,
                 fetched_at: str | None = None) -> Evidence:
    """Append an evidence record. `extracted` is validated JSON; it is never logged."""
    kind = _enum(EvidenceKind, kind, "evidence kind")
    if html_sha256 is not None:
        _check_hex64(html_sha256, "html_sha256")
    path = safe_relative_path(screenshot_path) if screenshot_path else None
    payload = canonical_json(extracted) if extracted is not None else None
    if (payload is None) != (extraction_source is None):
        raise ValidationError("extracted data and extraction_source must be given together")
    source = _enum(ExtractionSource, extraction_source, "extraction source") if extraction_source else None
    evidence_id = new_id("EV")
    _execute(
        conn,
        "INSERT INTO evidence (id, run_id, kind, url, fetched_at, html_sha256, screenshot_path, extracted_json, "
        "extraction_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence_id, run_id, kind, url, fetched_at or utc_now(), html_sha256, path, payload, source),
        f"add evidence to run {run_id}",
    )
    logger.info("evidence recorded", extra={"evidence_id": evidence_id, "run_id": run_id, "kind": kind})
    return get_evidence(conn, evidence_id)


def get_evidence(conn: sqlite3.Connection, evidence_id: str) -> Evidence:
    """Fetch evidence or raise NotFound."""
    return _evidence(_one(conn, "SELECT * FROM evidence WHERE id = ?", (evidence_id,), f"evidence {evidence_id}"))


def evidence_for_run(conn: sqlite3.Connection, run_id: str) -> list[Evidence]:
    """All evidence for a run in capture order."""
    rows = conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY fetched_at, id", (run_id,))
    return [_evidence(r) for r in rows]


# ---------------- vault files ----------------

_VAULT_FIELDS = ("doc_type", "patient_name", "doc_date", "invoice_no", "hospital", "amount_inr", "legible")


def _vault(row: sqlite3.Row) -> VaultFile:
    data = dict(row)
    data["legible"] = None if data["legible"] is None else bool(data["legible"])
    return VaultFile(**data)


def add_vault_file(conn: sqlite3.Connection, *, owner_id: str, storage_path: str, original_name: str,
                   sha256: str, size_bytes: int, content_type: str, id: str | None = None,
                   added_at: str | None = None, **fields: Any) -> VaultFile:
    """Register a stored file's metadata (never its bytes). Optional extracted fields via `fields`."""
    _check_hex64(sha256, "sha256")
    unknown = set(fields) - set(_VAULT_FIELDS) - {"extraction_source"}
    if unknown:
        raise ValidationError(f"unknown vault fields: {sorted(unknown)}")
    vault_id = id or new_id("VF")
    _execute(
        conn,
        "INSERT INTO vault_files (id, owner_id, storage_path, original_name, sha256, size_bytes, content_type, "
        "added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (vault_id, owner_id, safe_relative_path(storage_path), original_name, sha256, size_bytes, content_type,
         added_at or utc_now()),
        f"add vault file {original_name}",
    )
    if fields:
        source = fields.pop("extraction_source", None)
        if source is None:
            raise ValidationError("extraction_source is required when setting extracted fields")
        update_vault_fields(conn, vault_id, source=source, **fields)
    return get_vault_file(conn, vault_id)


def update_vault_fields(conn: sqlite3.Connection, vault_id: str, *, source: str, **fields: Any) -> VaultFile:
    """Set extracted fields on a vault file and record who produced them."""
    get_vault_file(conn, vault_id)
    unknown = set(fields) - set(_VAULT_FIELDS)
    if unknown:
        raise ValidationError(f"unknown vault fields: {sorted(unknown)}")
    if "legible" in fields and fields["legible"] is not None:
        fields["legible"] = int(bool(fields["legible"]))
    if "amount_inr" in fields and fields["amount_inr"] is not None:
        amount = fields["amount_inr"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValidationError("amount_inr must be a non-negative whole number of rupees")
    assignments = ", ".join(f"{k} = ?" for k in fields) + ", extraction_source = ?"
    _execute(
        conn,
        f"UPDATE vault_files SET {assignments} WHERE id = ?",  # noqa: S608 - keys checked against _VAULT_FIELDS
        (*fields.values(), _enum(ExtractionSource, source, "extraction source"), vault_id),
        f"update vault file {vault_id}",
    )
    return get_vault_file(conn, vault_id)


def get_vault_file(conn: sqlite3.Connection, vault_id: str) -> VaultFile:
    """Fetch a vault file or raise NotFound."""
    return _vault(_one(conn, "SELECT * FROM vault_files WHERE id = ?", (vault_id,), f"vault file {vault_id}"))


def vault_files_for(conn: sqlite3.Connection, owner_id: str) -> list[VaultFile]:
    """All files a person has added, ordered by id."""
    return [_vault(r) for r in conn.execute("SELECT * FROM vault_files WHERE owner_id = ? ORDER BY id", (owner_id,))]


def vault_file_by_sha(conn: sqlite3.Connection, owner_id: str, sha256: str) -> VaultFile | None:
    """The owner's file with this hash, if any."""
    row = conn.execute("SELECT * FROM vault_files WHERE owner_id = ? AND sha256 = ?", (owner_id, sha256)).fetchone()
    return _vault(row) if row else None


# ---------------- decisions ----------------


def _decision(row: sqlite3.Row) -> Decision:
    data = dict(row)
    data["input_refs"] = json.loads(data["input_refs"])
    return Decision(**data)


def record_decision(conn: sqlite3.Connection, *, run_id: str, kind: str, result: str, reason: str,
                    evidence_id: str | None = None, evidence_state: str | None = None,
                    input_refs: dict[str, Any] | None = None, subject_ref: str | None = None,
                    decided_by: str = DecidedBy.RULE.value) -> Decision:
    """Append a decision, following the audit convention.

    - `decided_by` is `rule` or `human`; model output belongs in evidence, never here.
    - `result` must be one of the allowed results for `kind`.
    - Evidence: give `evidence_id` (state becomes `linked`), or say honestly that it is
      `missing` or `not_applicable`. A status decision without evidence can only be UNCERTAIN.
    """
    kind_enum = DecisionKind(_enum(DecisionKind, kind, "decision kind"))
    by = _enum(DecidedBy, decided_by, "decided_by")
    if result not in ALLOWED_RESULTS[kind_enum]:
        raise ValidationError(
            f"result {result!r} is not allowed for {kind_enum.value} decisions; "
            f"allowed: {', '.join(sorted(ALLOWED_RESULTS[kind_enum]))}"
        )
    if not reason or not reason.strip():
        raise ValidationError("every decision needs a reason")
    if evidence_id is not None:
        if evidence_state not in (None, EvidenceState.LINKED.value):
            raise ValidationError("evidence_state must be 'linked' when evidence_id is given")
        state = EvidenceState.LINKED.value
    else:
        if evidence_state is None:
            raise ValidationError("say why there is no evidence: evidence_state 'missing' or 'not_applicable'")
        state = _enum(EvidenceState, evidence_state, "evidence_state")
        if state == EvidenceState.LINKED.value:
            raise ValidationError("evidence_state 'linked' needs an evidence_id")
    if kind_enum is DecisionKind.STATUS and state != EvidenceState.LINKED.value and result != CanonicalStatus.UNCERTAIN.value:
        raise ValidationError("a status decision without portal evidence can only be UNCERTAIN")
    decision_id = new_id("DEC")
    _execute(
        conn,
        "INSERT INTO decisions (id, run_id, kind, subject_ref, input_refs, result, reason, evidence_id, "
        "evidence_state, decided_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, run_id, kind_enum.value, subject_ref, canonical_json(input_refs or {}, expect=dict),
         result, reason.strip(), evidence_id, state, by, utc_now()),
        f"record {kind_enum.value} decision for run {run_id}",
    )
    logger.info("decision recorded", extra={"decision_id": decision_id, "run_id": run_id,
                                            "kind": kind_enum.value, "result": result, "evidence_state": state})
    return get_decision(conn, decision_id)


def get_decision(conn: sqlite3.Connection, decision_id: str) -> Decision:
    """Fetch a decision or raise NotFound."""
    return _decision(_one(conn, "SELECT * FROM decisions WHERE id = ?", (decision_id,), f"decision {decision_id}"))


def decisions_for_run(conn: sqlite3.Connection, run_id: str) -> list[Decision]:
    """All decisions for a run in order."""
    rows = conn.execute("SELECT * FROM decisions WHERE run_id = ? ORDER BY created_at, rowid", (run_id,))
    return [_decision(r) for r in rows]


# ---------------- actions ----------------


_IDEM_DOMAIN = b"claimsaathi/upload-idempotency/v1"


def idempotency_key(claim_id: str, requirement_id: str, file_sha256: str) -> str:
    """Same claim + requirement + file always gives the same key, across runs and retries.

    SHA-256 over a length-prefixed encoding (4-byte big-endian length, then UTF-8 bytes) of a
    versioned domain tag and the three fields, so two different triples never encode alike."""
    _check_claim_id(claim_id)
    _check_requirement_id(requirement_id)
    _check_hex64(file_sha256, "file_sha256")
    parts = (_IDEM_DOMAIN, claim_id.encode(), requirement_id.encode(), file_sha256.encode())
    return hashlib.sha256(b"".join(len(p).to_bytes(4, "big") + p for p in parts)).hexdigest()


def propose_action(conn: sqlite3.Connection, *, run_id: str, claim_id: str, requirement_id: str,
                   file_sha256: str, match_decision_id: str,
                   vault_file_id: str | None = None) -> tuple[Action, bool]:
    """Propose an upload. Returns (action, created). An existing key returns the existing action,
    whatever state it is in: proposing again never resets an approved, running or unknown upload.

    `match_decision_id` must be a MATCHED match decision for this run, requirement and file
    (enforced by trg_actions_require_match)."""
    key = idempotency_key(claim_id, requirement_id, file_sha256)
    existing = action_by_key(conn, key) or action_by_binding(conn, claim_id, requirement_id, file_sha256)
    if existing is not None:
        return existing, False
    now = utc_now()
    action_id = new_id("ACT")
    _execute(
        conn,
        "INSERT INTO actions (id, idem_key, run_id, claim_id, requirement_id, file_sha256, vault_file_id, state, "
        "created_at, updated_at, match_decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, key, run_id, claim_id, requirement_id, file_sha256, vault_file_id,
         ActionState.PROPOSED.value, now, now, match_decision_id),
        f"propose upload for {claim_id}/{requirement_id}",
    )
    logger.info("action proposed", extra={"action_id": action_id, "run_id": run_id, "claim_id": claim_id,
                                          "requirement_id": requirement_id})
    return get_action(conn, action_id), True


def get_action(conn: sqlite3.Connection, action_id: str) -> Action:
    """Fetch an action or raise NotFound."""
    return Action(**dict(_one(conn, "SELECT * FROM actions WHERE id = ?", (action_id,), f"action {action_id}")))


def action_by_key(conn: sqlite3.Connection, idem_key: str) -> Action | None:
    """The action with this idempotency key, if any."""
    row = conn.execute("SELECT * FROM actions WHERE idem_key = ?", (idem_key,)).fetchone()
    return Action(**dict(row)) if row else None


def action_by_binding(conn: sqlite3.Connection, claim_id: str, requirement_id: str,
                      file_sha256: str) -> Action | None:
    """The action for this claim, requirement and file, if any (unique in SQLite)."""
    row = conn.execute(
        "SELECT * FROM actions WHERE claim_id = ? AND requirement_id = ? AND file_sha256 = ?",
        (claim_id, requirement_id, file_sha256),
    ).fetchone()
    return Action(**dict(row)) if row else None


def actions_for_requester(conn: sqlite3.Connection, person_id: str) -> list[Action]:
    """Uploads proposed in runs this person started, newest first."""
    rows = conn.execute(
        "SELECT a.* FROM actions a JOIN runs r ON r.id = a.run_id WHERE r.requester_id = ? "
        "ORDER BY a.created_at DESC, a.id", (person_id,),
    )
    return [Action(**dict(r)) for r in rows]


def approve_action(conn: sqlite3.Connection, action_id: str, *, approved_by: str,
                   card: dict[str, Any] | None = None) -> Action:
    """Record a human approval: PROPOSED -> APPROVED. The approval row is bound to this action's
    exact claim, requirement and file, and keeps the card the person was shown (if given)."""
    get_person(conn, approved_by)
    _record_human_decision(conn, action_id, ActionState.APPROVED, approved_by, card)
    return _transition(conn, action_id, ActionState.APPROVED, approved_by=approved_by)


def reject_action(conn: sqlite3.Connection, action_id: str, *, rejected_by: str,
                  card: dict[str, Any] | None = None) -> Action:
    """Record a human rejection: PROPOSED -> REJECTED."""
    get_person(conn, rejected_by)
    _record_human_decision(conn, action_id, ActionState.REJECTED, rejected_by, card)
    return _transition(conn, action_id, ActionState.REJECTED, error=f"rejected by {rejected_by}")


def card_digest(card: dict[str, Any]) -> str:
    """SHA-256 of an approval card's canonical JSON: what the person saw, as one value."""
    return hashlib.sha256(canonical_json(card, expect=dict).encode("utf-8")).hexdigest()


def _record_human_decision(conn: sqlite3.Connection, action_id: str, target: ActionState, person_id: str,
                           card: dict[str, Any] | None) -> None:
    action = get_action(conn, action_id)
    if target not in ACTION_TRANSITIONS[ActionState(action.state)]:
        raise InvalidTransition(f"action {action_id}: {action.state} -> {target.value} is not allowed")
    decision = ApprovalDecision.APPROVED if target is ActionState.APPROVED else ApprovalDecision.REJECTED
    _execute(
        conn,
        "INSERT INTO action_approvals (id, action_id, decision, decided_by, decided_at, idem_key, claim_id, "
        "requirement_id, file_sha256, vault_file_id, card_sha256, card_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id("APR"), action.id, decision.value, person_id, utc_now(), action.idem_key, action.claim_id,
         action.requirement_id, action.file_sha256, action.vault_file_id,
         card_digest(card) if card is not None else None,
         canonical_json(card, expect=dict) if card is not None else None),
        f"record {decision.value} for action {action_id}",
    )
    logger.info("action decided", extra={"action_id": action_id, "decision": decision.value, "by": person_id})


def transition_action(conn: sqlite3.Connection, action_id: str, to_state: str, *,
                      error: str | None = None) -> Action:
    """Move an action along its lifecycle. Entering EXECUTING counts an attempt."""
    target = ActionState(_enum(ActionState, to_state, "action state"))
    if target in (ActionState.APPROVED, ActionState.REJECTED):
        raise InvalidTransition("use approve_action / reject_action so the person is recorded")
    return _transition(conn, action_id, target, error=error)


def _transition(conn: sqlite3.Connection, action_id: str, target: ActionState, *,
                approved_by: str | None = None, error: str | None = None) -> Action:
    action = get_action(conn, action_id)
    current = ActionState(action.state)
    if target not in ACTION_TRANSITIONS[current]:
        raise InvalidTransition(f"action {action_id}: {current.value} -> {target.value} is not allowed")
    now = utc_now()
    attempts = action.attempts + (1 if target is ActionState.EXECUTING else 0)
    cursor = _execute(
        conn,
        "UPDATE actions SET state = ?, attempts = ?, last_error = ?, updated_at = ?, "
        "approved_by = COALESCE(?, approved_by), approved_at = CASE WHEN ? IS NULL THEN approved_at ELSE ? END "
        "WHERE id = ? AND state = ?",
        (target.value, attempts, error, now, approved_by, approved_by, now, action_id, current.value),
        f"move action {action_id} to {target.value}",
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(f"action {action_id} changed concurrently; re-read it before retrying")
    logger.info("action state", extra={"action_id": action_id, "from": current.value, "to": target.value,
                                       "attempts": attempts})
    return get_action(conn, action_id)


def note_action_error(conn: sqlite3.Connection, action_id: str, *, expected_state: str, error: str) -> Action:
    """Record why an action could not move on (e.g. a failed re-read) without changing its state."""
    cursor = conn.execute(
        "UPDATE actions SET last_error = ?, updated_at = ? WHERE id = ? AND state = ?",
        (error, utc_now(), action_id, expected_state),
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(f"action {action_id} changed concurrently; re-read it before retrying")
    return get_action(conn, action_id)


# ---------------- approvals, checks and upload attempts ----------------


@dataclass(frozen=True)
class ActionApproval:
    id: str
    action_id: str
    decision: str
    decided_by: str
    decided_at: str
    idem_key: str
    claim_id: str
    requirement_id: str
    file_sha256: str
    vault_file_id: str | None
    card_sha256: str | None
    card: dict[str, Any] | None


@dataclass(frozen=True)
class ActionCheck:
    id: str
    action_id: str
    phase: str
    checked_at: str
    passed: bool
    checks: list[dict[str, Any]]
    evidence_id: str | None
    consent_decision_id: str | None


@dataclass(frozen=True)
class ActionAttempt:
    id: str
    action_id: str
    attempt_no: int
    check_id: str
    started_at: str
    deadline_ms: int
    finished_at: str | None
    outcome: str | None
    http_status: int | None
    error_code: str | None
    message: str | None
    evidence_id: str | None


def get_action_approval(conn: sqlite3.Connection, action_id: str) -> ActionApproval | None:
    """The person's recorded decision on an action, if any (at most one)."""
    row = conn.execute("SELECT * FROM action_approvals WHERE action_id = ?", (action_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    raw = data.pop("card_json")
    data["card"] = json.loads(raw) if raw is not None else None
    return ActionApproval(**data)


def _action_check(row: sqlite3.Row) -> ActionCheck:
    data = dict(row)
    data["checks"] = json.loads(data.pop("checks_json"))
    data["passed"] = bool(data["passed"])
    return ActionCheck(**data)


def record_action_check(conn: sqlite3.Connection, *, action_id: str, phase: str, passed: bool,
                        checks: list[dict[str, Any]], evidence_id: str | None = None,
                        consent_decision_id: str | None = None) -> ActionCheck:
    """Append the result of one eligibility check run (every rule, with its outcome and detail)."""
    phase = _enum(CheckPhase, phase, "check phase")
    if passed != all(bool(c.get("ok")) for c in checks) or not checks:
        raise ValidationError("passed must be true exactly when every listed check is ok")
    check_id = new_id("CHK")
    _execute(
        conn,
        "INSERT INTO action_checks (id, action_id, phase, checked_at, passed, checks_json, evidence_id, "
        "consent_decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (check_id, action_id, phase, utc_now(), int(passed), canonical_json(checks, expect=list), evidence_id,
         consent_decision_id),
        f"record {phase} check for action {action_id}",
    )
    logger.info("action checked", extra={"action_id": action_id, "phase": phase, "passed": passed})
    return _action_check(_one(conn, "SELECT * FROM action_checks WHERE id = ?", (check_id,), f"check {check_id}"))


def checks_for(conn: sqlite3.Connection, action_id: str) -> list[ActionCheck]:
    """Every check run for an action, oldest first."""
    rows = conn.execute("SELECT * FROM action_checks WHERE action_id = ? ORDER BY checked_at, rowid", (action_id,))
    return [_action_check(r) for r in rows]


def start_attempt(conn: sqlite3.Connection, *, action_id: str, check_id: str, deadline_ms: int) -> ActionAttempt:
    """Open the attempt row for an action that just entered EXECUTING (numbered by its attempt count)."""
    action = get_action(conn, action_id)
    attempt_id = new_id("ATT")
    _execute(
        conn,
        "INSERT INTO action_attempts (id, action_id, attempt_no, check_id, started_at, deadline_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (attempt_id, action_id, action.attempts, check_id, utc_now(), deadline_ms),
        f"start attempt {action.attempts} for action {action_id}",
    )
    logger.info("attempt started", extra={"action_id": action_id, "attempt": action.attempts})
    return get_attempt(conn, attempt_id)


def finish_attempt(conn: sqlite3.Connection, attempt_id: str, *, outcome: str, http_status: int | None = None,
                   error_code: str | None = None, message: str | None = None,
                   evidence_id: str | None = None) -> ActionAttempt:
    """Record what an attempt got back. Allowed once per attempt."""
    _execute(
        conn,
        "UPDATE action_attempts SET finished_at = ?, outcome = ?, http_status = ?, error_code = ?, message = ?, "
        "evidence_id = ? WHERE id = ?",
        (utc_now(), _enum(AttemptOutcome, outcome, "attempt outcome"), http_status, error_code,
         (message or None) and message[:500], evidence_id, attempt_id),
        f"finish attempt {attempt_id}",
    )
    logger.info("attempt finished", extra={"attempt_id": attempt_id, "outcome": outcome, "http_status": http_status})
    return get_attempt(conn, attempt_id)


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> ActionAttempt:
    """Fetch an attempt or raise NotFound."""
    return ActionAttempt(**dict(_one(conn, "SELECT * FROM action_attempts WHERE id = ?", (attempt_id,),
                                     f"attempt {attempt_id}")))


def open_attempt(conn: sqlite3.Connection, action_id: str) -> ActionAttempt | None:
    """The attempt still waiting for a result, if any (at most one, enforced by a unique index)."""
    row = conn.execute("SELECT * FROM action_attempts WHERE action_id = ? AND finished_at IS NULL",
                       (action_id,)).fetchone()
    return ActionAttempt(**dict(row)) if row else None


def attempts_for(conn: sqlite3.Connection, action_id: str) -> list[ActionAttempt]:
    """Every attempt for an action, in order."""
    rows = conn.execute("SELECT * FROM action_attempts WHERE action_id = ? ORDER BY attempt_no", (action_id,))
    return [ActionAttempt(**dict(r)) for r in rows]


def verifications_for(conn: sqlite3.Connection, action_id: str) -> list[Decision]:
    """Every verification (portal re-read) decision recorded for an action, oldest first."""
    rows = conn.execute(
        "SELECT * FROM decisions WHERE kind = 'verification' AND subject_ref = ? ORDER BY created_at, rowid",
        (action_id,),
    )
    return [_decision(r) for r in rows]


# ---------------- workflow steps ----------------


@dataclass(frozen=True)
class GraphStep:
    id: str
    run_id: str
    seq: int
    node: str
    started_at: str
    finished_at: str
    result: str
    decision: str
    next_node: str | None
    input_refs: dict[str, Any]
    output_refs: dict[str, Any]
    evidence_ids: list[str]
    error: str | None


def _graph_step(row: sqlite3.Row) -> GraphStep:
    data = dict(row)
    for key in ("input_refs", "output_refs", "evidence_ids"):
        data[key] = json.loads(data[key])
    return GraphStep(**data)


def record_step(conn: sqlite3.Connection, *, run_id: str, node: str, started_at: str, result: str, decision: str,
                next_node: str | None = None, input_refs: dict[str, Any] | None = None,
                output_refs: dict[str, Any] | None = None, evidence_ids: list[str] | None = None,
                error: str | None = None) -> GraphStep:
    """Append one workflow node execution (numbered within the run)."""
    result = _enum(StepResult, result, "step result")
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM graph_steps WHERE run_id = ?", (run_id,)).fetchone()[0]
    step_id = new_id("STP")
    _execute(
        conn,
        "INSERT INTO graph_steps (id, run_id, seq, node, started_at, finished_at, result, decision, next_node, "
        "input_refs, output_refs, evidence_ids, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, run_id, seq, node, started_at, max(started_at, utc_now()), result, decision.strip()[:1000],
         next_node, canonical_json(input_refs or {}, expect=dict), canonical_json(output_refs or {}, expect=dict),
         canonical_json(list(evidence_ids or []), expect=list), (error or None) and error[:1000]),
        f"record step {node} for run {run_id}",
    )
    logger.info("workflow step", extra={"run_id": run_id, "node": node, "result": result, "next": next_node})
    return _graph_step(_one(conn, "SELECT * FROM graph_steps WHERE id = ?", (step_id,), f"step {step_id}"))


def steps_for_run(conn: sqlite3.Connection, run_id: str) -> list[GraphStep]:
    """Every node execution in a run, in order."""
    return [_graph_step(r) for r in conn.execute("SELECT * FROM graph_steps WHERE run_id = ? ORDER BY seq", (run_id,))]


def runs_for_requester(conn: sqlite3.Connection, person_id: str, limit: int = 50) -> list[Run]:
    """A person's runs, newest first."""
    rows = conn.execute("SELECT * FROM runs WHERE requester_id = ? ORDER BY started_at DESC, id LIMIT ?",
                        (person_id, limit))
    return [Run(**dict(r)) for r in rows]


# ---------------- scenario results ----------------


@dataclass(frozen=True)
class ScenarioResult:
    id: str
    test_case: str
    title: str
    run_id: str | None
    input: dict[str, Any]
    expected: str
    actual: str
    verdict: str
    checks: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    recorded_at: str


def _scenario(row: sqlite3.Row) -> ScenarioResult:
    data = dict(row)
    data["input"] = json.loads(data.pop("input_json"))
    data["checks"] = json.loads(data.pop("checks_json"))
    data["evidence"] = json.loads(data.pop("evidence_json"))
    return ScenarioResult(**data)


def record_scenario_result(conn: sqlite3.Connection, *, test_case: str, title: str, run_id: str | None,
                           input: dict[str, Any], expected: str, actual: str, passed: bool,
                           checks: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> ScenarioResult:
    """Store the verdict of one executed scenario. PASS only if it has checks and every check passed."""
    if passed != (bool(checks) and all(c.get("ok") for c in checks)):
        raise ValidationError("a scenario passes exactly when it has checks and every check passed")
    result_id = new_id("SCN")
    _execute(
        conn,
        "INSERT INTO scenario_results (id, test_case, title, run_id, input_json, expected, actual, verdict, "
        "checks_json, evidence_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (result_id, test_case, title, run_id, canonical_json(input, expect=dict), expected, actual,
         "PASS" if passed else "FAIL", canonical_json(checks, expect=list), canonical_json(evidence, expect=list),
         utc_now()),
        f"record scenario {test_case}",
    )
    return _scenario(_one(conn, "SELECT * FROM scenario_results WHERE id = ?", (result_id,), result_id))


def test_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Runs tagged with a test case, newest first, each with its recorded verdict (if any)."""
    rows = conn.execute(
        "SELECT r.*, s.id AS result_id, s.title, s.expected, s.actual, s.verdict, s.recorded_at "
        "FROM runs r LEFT JOIN scenario_results s ON s.run_id = r.id "
        "WHERE r.test_case IS NOT NULL ORDER BY r.started_at DESC, r.id"
    )
    return [dict(r) for r in rows]


test_runs.__test__ = False  # not a pytest test, despite the name


def scenario_results(conn: sqlite3.Connection) -> list[ScenarioResult]:
    """Every recorded scenario verdict, oldest first."""
    return [_scenario(r) for r in conn.execute("SELECT * FROM scenario_results ORDER BY recorded_at, rowid")]


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for every data table (used by health output and tests)."""
    tables = ("people", "claims", "consents", "consent_requests", "runs", "evidence", "vault_files",
              "decisions", "actions", "action_approvals", "action_checks", "action_attempts", "graph_steps",
              "scenario_results")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608



# ---------------- claim registry ----------------


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    policy_no: str
    patient_id: str
    source: str
    registered_at: str


def register_claim(conn: sqlite3.Connection, *, claim_id: str, policy_no: str, patient_id: str,
                   source: str, registered_at: str | None = None) -> ClaimRecord:
    """Add a claim to the trusted registry (claim -> patient -> policy)."""
    _check_claim_id(claim_id)
    _execute(
        conn,
        "INSERT INTO claims (claim_id, policy_no, patient_id, source, registered_at) VALUES (?, ?, ?, ?, ?)",
        (claim_id, policy_no, patient_id, _enum(ClaimSource, source, "claim source"), registered_at or utc_now()),
        f"register claim {claim_id}",
    )
    return get_claim_record(conn, claim_id)


def get_claim_record(conn: sqlite3.Connection, claim_id: str) -> ClaimRecord:
    """Fetch a registry entry or raise NotFound."""
    return ClaimRecord(**dict(_one(conn, "SELECT * FROM claims WHERE claim_id = ?", (claim_id,), f"claim {claim_id}")))


# ---------------- consent requests ----------------


@dataclass(frozen=True)
class ConsentRequest:
    id: str
    requester_id: str
    patient_id: str
    policy_no: str
    claim_id: str
    actions: tuple[str, ...]
    run_id: str | None
    status: str
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    consent_id: str | None


def _consent_request(row: sqlite3.Row) -> ConsentRequest:
    data = dict(row)
    data["actions"] = tuple(json.loads(data["actions"]))
    return ConsentRequest(**data)


def create_consent_request(conn: sqlite3.Connection, *, requester_id: str, patient_id: str, policy_no: str,
                           claim_id: str, actions: list[str], created_at: str, expires_at: str,
                           run_id: str | None = None) -> ConsentRequest:
    """Insert a pending request. Only one pending request per requester, patient and claim may exist."""
    clean = sorted({_enum(ConsentAction, a, "consent action") for a in actions})
    if not clean:
        raise ValidationError("a consent request must ask for at least one action")
    request_id = new_id("CRQ")
    _execute(
        conn,
        "INSERT INTO consent_requests (id, requester_id, patient_id, policy_no, claim_id, actions, run_id, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (request_id, requester_id, patient_id, policy_no, claim_id, canonical_json(clean, expect=list), run_id,
         created_at, expires_at),
        f"create consent request for {claim_id}",
    )
    logger.info("consent request created", extra={"request_id": request_id, "claim_id": claim_id,
                                                  "requester_id": requester_id, "patient_id": patient_id})
    return get_consent_request(conn, request_id)


def get_consent_request(conn: sqlite3.Connection, request_id: str) -> ConsentRequest:
    """Fetch a consent request or raise NotFound."""
    return _consent_request(
        _one(conn, "SELECT * FROM consent_requests WHERE id = ?", (request_id,), f"consent request {request_id}")
    )


def pending_consent_request(conn: sqlite3.Connection, requester_id: str, patient_id: str,
                            claim_id: str) -> ConsentRequest | None:
    """The single pending request for this requester, patient and claim, if any."""
    row = conn.execute(
        "SELECT * FROM consent_requests WHERE requester_id = ? AND patient_id = ? AND claim_id = ? "
        "AND status = 'pending'",
        (requester_id, patient_id, claim_id),
    ).fetchone()
    return _consent_request(row) if row else None


def consent_requests_for(conn: sqlite3.Connection, *, patient_id: str | None = None,
                         requester_id: str | None = None) -> list[ConsentRequest]:
    """Requests about a patient or made by a requester, newest first."""
    if (patient_id is None) == (requester_id is None):
        raise ValidationError("give exactly one of patient_id or requester_id")
    column, value = ("patient_id", patient_id) if patient_id else ("requester_id", requester_id)
    rows = conn.execute(
        f"SELECT * FROM consent_requests WHERE {column} = ? ORDER BY created_at DESC, id", (value,)  # noqa: S608
    )
    return [_consent_request(r) for r in rows]


def set_consent_request_actions(conn: sqlite3.Connection, request_id: str, actions: list[str]) -> ConsentRequest:
    """Widen a still-pending request's actions (e.g. read first, upload later)."""
    clean = sorted({_enum(ConsentAction, a, "consent action") for a in actions})
    cursor = conn.execute(
        "UPDATE consent_requests SET actions = ? WHERE id = ? AND status = 'pending'",
        (canonical_json(clean, expect=list), request_id),
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(f"consent request {request_id} is no longer pending")
    return get_consent_request(conn, request_id)


def close_consent_request(conn: sqlite3.Connection, request_id: str, *, status: str, at: str,
                          decided_by: str | None = None, consent_id: str | None = None) -> ConsentRequest:
    """Move a pending request to approved, denied or expired."""
    target = _enum(ConsentRequestStatus, status, "consent request status")
    if target == ConsentRequestStatus.PENDING.value:
        raise InvalidTransition("a request cannot be reopened")
    cursor = _execute(
        conn,
        "UPDATE consent_requests SET status = ?, decided_at = ?, decided_by = ?, consent_id = ? "
        "WHERE id = ? AND status = 'pending'",
        (target, None if target == "expired" else at, decided_by, consent_id, request_id),
        f"close consent request {request_id}",
    )
    if cursor.rowcount != 1:
        raise InvalidTransition(f"consent request {request_id} is no longer pending")
    logger.info("consent request closed", extra={"request_id": request_id, "status": target})
    return get_consent_request(conn, request_id)


def consents_for(conn: sqlite3.Connection, *, patient_id: str | None = None,
                 grantee_id: str | None = None) -> list[Consent]:
    """Consents a patient has given, or a grantee has received, newest first."""
    if (patient_id is None) == (grantee_id is None):
        raise ValidationError("give exactly one of patient_id or grantee_id")
    column, value = ("patient_id", patient_id) if patient_id else ("grantee_id", grantee_id)
    rows = conn.execute(f"SELECT * FROM consents WHERE {column} = ? ORDER BY granted_at DESC, id", (value,))  # noqa: S608
    return [_consent(r) for r in rows]


# ---------------- extractions ----------------


@dataclass(frozen=True)
class Extraction:
    id: str
    run_id: str
    source_evidence_id: str
    subject: str
    subject_ref: str
    method: str
    model: str | None
    raw_text: str | None
    raw_output: str | None
    parsed: dict[str, Any] | None
    valid: bool
    errors: list[str]
    created_at: str


def _extraction(row: sqlite3.Row) -> Extraction:
    data = dict(row)
    parsed = data.pop("parsed_json")
    data["parsed"] = json.loads(parsed) if parsed is not None else None
    data["errors"] = json.loads(data.pop("errors_json"))
    data["valid"] = bool(data["valid"])
    return Extraction(**data)


def add_extraction(conn: sqlite3.Connection, *, run_id: str, source_evidence_id: str, subject: str,
                   subject_ref: str, method: str, valid: bool, parsed: dict[str, Any] | None,
                   errors: list[str], raw_text: str | None = None, raw_output: str | None = None,
                   model: str | None = None) -> Extraction:
    """Record one extraction attempt. Invalid attempts are kept too, with their errors."""
    if valid and errors:
        raise ValidationError("a valid extraction cannot carry validation errors")
    extraction_id = new_id("EXT")
    _execute(
        conn,
        "INSERT INTO extractions (id, run_id, source_evidence_id, subject, subject_ref, method, model, raw_text, "
        "raw_output, parsed_json, valid, errors_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (extraction_id, run_id, source_evidence_id, subject, subject_ref, method, model, raw_text, raw_output,
         canonical_json(parsed, expect=dict) if parsed is not None else None, int(valid),
         canonical_json([str(e) for e in errors], expect=list), utc_now()),
        f"record {method} extraction for {subject_ref}",
    )
    logger.info("extraction recorded", extra={"extraction_id": extraction_id, "run_id": run_id,
                                              "subject_ref": subject_ref, "method": method, "valid": valid})
    return get_extraction(conn, extraction_id)


def get_extraction(conn: sqlite3.Connection, extraction_id: str) -> Extraction:
    """Fetch an extraction or raise NotFound."""
    return _extraction(_one(conn, "SELECT * FROM extractions WHERE id = ?", (extraction_id,),
                            f"extraction {extraction_id}"))
