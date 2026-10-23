"""Versioned schema migrations, tracked with `PRAGMA user_version` and `schema_migrations`.

Each migration runs in one transaction together with its version bump, so a
failed migration leaves the database exactly as it was. Never edit an applied
migration; append a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.models import (
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

# UTC ISO-8601 with a Z suffix, second precision: 2026-10-22T08:30:00Z
TS = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
DATE = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
CLAIM = "CLM-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]"


def _in(enum: type[Enum]) -> str:
    return "(" + ", ".join(f"'{e.value}'" for e in enum) + ")"


def _hex64(col: str) -> str:
    return f"(length({col}) = 64 AND {col} NOT GLOB '*[^0-9a-f]*')"


def _safe_rel_path(col: str) -> str:
    """Relative path with forward slashes, no parent segments, no drive or root."""
    return (
        f"(length({col}) BETWEEN 1 AND 255 AND {col} NOT LIKE '/%' AND {col} NOT LIKE '%..%' "
        f"AND {col} NOT LIKE '%:%' AND instr({col}, char(92)) = 0)"
    )


@dataclass(frozen=True)
class Migration:
    """One schema step."""

    version: int
    name: str
    sql: str


MIGRATION_1 = f"""
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL CHECK (applied_at GLOB '{TS}')
);

CREATE TABLE people (
    id                TEXT PRIMARY KEY CHECK (length(id) BETWEEN 3 AND 64),
    name              TEXT NOT NULL CHECK (length(trim(name)) > 0),
    role              TEXT NOT NULL CHECK (role IN {_in(PersonRole)}),
    contact_ref       TEXT NOT NULL UNIQUE CHECK (contact_ref LIKE 'synthetic:%'),
    portal_member_id  TEXT UNIQUE,
    created_at        TEXT NOT NULL CHECK (created_at GLOB '{TS}')
);

CREATE TABLE consents (
    id          TEXT PRIMARY KEY,
    patient_id  TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    grantee_id  TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    policy_no   TEXT NOT NULL CHECK (length(policy_no) > 0),
    claim_id    TEXT NOT NULL CHECK (claim_id = '*' OR claim_id GLOB '{CLAIM}'),
    actions     TEXT NOT NULL CHECK (json_valid(actions) AND json_type(actions) = 'array'
                                     AND json_array_length(actions) > 0),
    granted_at  TEXT NOT NULL CHECK (granted_at GLOB '{TS}'),
    expires_at  TEXT NOT NULL CHECK (expires_at GLOB '{TS}'),
    revoked     INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    revoked_at  TEXT CHECK (revoked_at IS NULL OR revoked_at GLOB '{TS}'),
    CHECK (patient_id <> grantee_id),
    CHECK (expires_at > granted_at),
    CHECK ((revoked = 1) = (revoked_at IS NOT NULL))
);
CREATE INDEX idx_consents_lookup ON consents (grantee_id, patient_id, revoked);

CREATE TABLE runs (
    id            TEXT PRIMARY KEY,
    requester_id  TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    claim_id      TEXT NOT NULL CHECK (claim_id GLOB '{CLAIM}'),
    state         TEXT NOT NULL DEFAULT 'created' CHECK (state IN {_in(RunState)}),
    final_status  TEXT CHECK (final_status IS NULL OR final_status IN {_in(CanonicalStatus)}),
    test_case     TEXT CHECK (test_case IS NULL OR test_case GLOB 'TC[0-9]*' OR test_case GLOB 'E[0-9]*'),
    started_at    TEXT NOT NULL CHECK (started_at GLOB '{TS}'),
    finished_at   TEXT CHECK (finished_at IS NULL OR finished_at GLOB '{TS}'),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK ((state IN ('blocked', 'completed', 'failed')) = (finished_at IS NOT NULL))
);
CREATE INDEX idx_runs_claim ON runs (claim_id, started_at);
CREATE INDEX idx_runs_test_case ON runs (test_case);

CREATE TABLE evidence (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    kind               TEXT NOT NULL CHECK (kind IN {_in(EvidenceKind)}),
    url                TEXT NOT NULL CHECK (length(url) BETWEEN 1 AND 2048),
    fetched_at         TEXT NOT NULL CHECK (fetched_at GLOB '{TS}'),
    html_sha256        TEXT CHECK (html_sha256 IS NULL OR {_hex64('html_sha256')}),
    screenshot_path    TEXT CHECK (screenshot_path IS NULL OR {_safe_rel_path('screenshot_path')}),
    extracted_json     TEXT CHECK (extracted_json IS NULL OR json_valid(extracted_json)),
    extraction_source  TEXT CHECK (extraction_source IS NULL OR extraction_source IN {_in(ExtractionSource)}),
    CHECK ((extracted_json IS NULL) = (extraction_source IS NULL))
);
CREATE INDEX idx_evidence_run ON evidence (run_id, fetched_at);

CREATE TABLE vault_files (
    id                 TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    storage_path       TEXT NOT NULL UNIQUE CHECK {_safe_rel_path('storage_path')},
    original_name      TEXT NOT NULL CHECK (length(original_name) BETWEEN 1 AND 120),
    sha256             TEXT NOT NULL CHECK {_hex64('sha256')},
    size_bytes         INTEGER NOT NULL CHECK (size_bytes > 0),
    content_type       TEXT NOT NULL CHECK (content_type IN ('application/pdf', 'image/png', 'image/jpeg')),
    doc_type           TEXT,
    patient_name       TEXT,
    doc_date           TEXT CHECK (doc_date IS NULL OR doc_date GLOB '{DATE}'),
    invoice_no         TEXT,
    hospital           TEXT,
    amount_inr         INTEGER CHECK (amount_inr IS NULL OR amount_inr >= 0),
    legible            INTEGER CHECK (legible IS NULL OR legible IN (0, 1)),
    extraction_source  TEXT CHECK (extraction_source IS NULL OR extraction_source IN {_in(ExtractionSource)}),
    added_at           TEXT NOT NULL CHECK (added_at GLOB '{TS}'),
    UNIQUE (owner_id, sha256)
);
CREATE INDEX idx_vault_owner ON vault_files (owner_id, doc_type);

CREATE TABLE decisions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    kind            TEXT NOT NULL CHECK (kind IN {_in(DecisionKind)}),
    subject_ref     TEXT,
    input_refs      TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(input_refs) AND json_type(input_refs) = 'object'),
    result          TEXT NOT NULL CHECK (length(result) > 0),
    reason          TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence_id     TEXT REFERENCES evidence(id) ON DELETE RESTRICT,
    evidence_state  TEXT NOT NULL CHECK (evidence_state IN {_in(EvidenceState)}),
    decided_by      TEXT NOT NULL CHECK (decided_by IN {_in(DecidedBy)}),
    created_at      TEXT NOT NULL CHECK (created_at GLOB '{TS}'),
    CHECK ((evidence_state = 'linked') = (evidence_id IS NOT NULL))
);
CREATE INDEX idx_decisions_run ON decisions (run_id, kind);
CREATE INDEX idx_decisions_evidence ON decisions (evidence_id);

CREATE TRIGGER trg_decisions_evidence_same_run
BEFORE INSERT ON decisions
WHEN NEW.evidence_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM evidence WHERE id = NEW.evidence_id AND run_id = NEW.run_id)
BEGIN
    SELECT RAISE(ABORT, 'decision evidence must exist and belong to the same run');
END;

CREATE TRIGGER trg_decisions_append_only
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER trg_evidence_append_only
BEFORE UPDATE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'evidence is append-only');
END;

CREATE TABLE actions (
    id              TEXT PRIMARY KEY,
    idem_key        TEXT NOT NULL UNIQUE CHECK {_hex64('idem_key')},
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    claim_id        TEXT NOT NULL CHECK (claim_id GLOB '{CLAIM}'),
    requirement_id  TEXT NOT NULL CHECK (length(requirement_id) BETWEEN 1 AND 64),
    file_sha256     TEXT NOT NULL CHECK {_hex64('file_sha256')},
    vault_file_id   TEXT REFERENCES vault_files(id) ON DELETE RESTRICT,
    state           TEXT NOT NULL CHECK (state IN {_in(ActionState)}),
    approved_by     TEXT REFERENCES people(id) ON DELETE RESTRICT,
    approved_at     TEXT CHECK (approved_at IS NULL OR approved_at GLOB '{TS}'),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      TEXT,
    created_at      TEXT NOT NULL CHECK (created_at GLOB '{TS}'),
    updated_at      TEXT NOT NULL CHECK (updated_at GLOB '{TS}'),
    -- Nothing past PROPOSED/REJECTED may exist without a recorded human approval.
    CHECK (state IN ('PROPOSED', 'REJECTED') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (state <> 'EXECUTING' OR attempts > 0)
);
CREATE INDEX idx_actions_run ON actions (run_id);
CREATE INDEX idx_actions_claim_req ON actions (claim_id, requirement_id);
"""

MIGRATION_2 = f"""
-- Trusted claim registry: which patient and policy a claim belongs to, known without
-- reading the protected claim page.
CREATE TABLE claims (
    claim_id       TEXT PRIMARY KEY CHECK (claim_id GLOB '{CLAIM}'),
    policy_no      TEXT NOT NULL CHECK (length(policy_no) > 0),
    patient_id     TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    source         TEXT NOT NULL CHECK (source IN {_in(ClaimSource)}),
    registered_at  TEXT NOT NULL CHECK (registered_at GLOB '{TS}')
);
CREATE INDEX idx_claims_patient ON claims (patient_id);

CREATE TABLE consent_requests (
    id            TEXT PRIMARY KEY,
    requester_id  TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    patient_id    TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    policy_no     TEXT NOT NULL CHECK (length(policy_no) > 0),
    claim_id      TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    actions       TEXT NOT NULL CHECK (json_valid(actions) AND json_type(actions) = 'array'
                                       AND json_array_length(actions) > 0),
    run_id        TEXT REFERENCES runs(id) ON DELETE RESTRICT,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN {_in(ConsentRequestStatus)}),
    created_at    TEXT NOT NULL CHECK (created_at GLOB '{TS}'),
    expires_at    TEXT NOT NULL CHECK (expires_at GLOB '{TS}'),
    decided_at    TEXT CHECK (decided_at IS NULL OR decided_at GLOB '{TS}'),
    decided_by    TEXT REFERENCES people(id) ON DELETE RESTRICT,
    consent_id    TEXT REFERENCES consents(id) ON DELETE RESTRICT,
    CHECK (requester_id <> patient_id),
    CHECK (expires_at > created_at),
    CHECK ((status = 'approved') = (consent_id IS NOT NULL)),
    CHECK ((status IN ('approved', 'denied')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    -- Only the patient can decide a request about their own data.
    CHECK (decided_by IS NULL OR decided_by = patient_id)
);
CREATE INDEX idx_consent_requests_patient ON consent_requests (patient_id, status);
CREATE UNIQUE INDEX uq_consent_requests_one_pending
    ON consent_requests (requester_id, patient_id, claim_id) WHERE status = 'pending';

-- Backstop for the consent gate: protected portal evidence can only be recorded in a run
-- that already holds an ALLOW consent decision. The service checks first; this catches bugs.
CREATE TRIGGER trg_evidence_requires_consent
BEFORE INSERT ON evidence
WHEN NEW.kind IN ('portal_page', 'document_list', 'upload_response')
 AND NOT EXISTS (SELECT 1 FROM decisions WHERE run_id = NEW.run_id AND kind = 'consent' AND result = 'ALLOW')
BEGIN
    SELECT RAISE(ABORT, 'protected portal evidence needs an ALLOW consent decision in the same run');
END;
"""

MIGRATION_3 = f"""
-- Every extraction attempt (claim page or vault file), valid or not, with its inputs and errors.
CREATE TABLE extractions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    source_evidence_id  TEXT NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
    subject             TEXT NOT NULL CHECK (subject IN ('claim_page', 'vault_file')),
    subject_ref         TEXT NOT NULL CHECK (length(subject_ref) BETWEEN 1 AND 64),
    method              TEXT NOT NULL CHECK (method IN ('dom', 'gemini', 'seed_label')),
    model               TEXT,
    raw_text            TEXT,
    raw_output          TEXT,
    parsed_json         TEXT CHECK (parsed_json IS NULL OR json_valid(parsed_json)),
    valid               INTEGER NOT NULL CHECK (valid IN (0, 1)),
    errors_json         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(errors_json) AND json_type(errors_json) = 'array'),
    created_at          TEXT NOT NULL CHECK (created_at GLOB '{TS}'),
    CHECK (valid = 0 OR (parsed_json IS NOT NULL AND json_array_length(errors_json) = 0)),
    CHECK (method <> 'gemini' OR model IS NOT NULL)
);
CREATE INDEX idx_extractions_run ON extractions (run_id, subject);

CREATE TRIGGER trg_extractions_same_run
BEFORE INSERT ON extractions
WHEN NOT EXISTS (SELECT 1 FROM evidence WHERE id = NEW.source_evidence_id AND run_id = NEW.run_id)
BEGIN
    SELECT RAISE(ABORT, 'extraction source evidence must belong to the same run');
END;

CREATE TRIGGER trg_extractions_append_only
BEFORE UPDATE ON extractions
BEGIN
    SELECT RAISE(ABORT, 'extractions are append-only');
END;
"""

MIGRATION_4 = """
-- An upload can only be proposed from a MATCHED match decision for the same run, requirement
-- and file hash. A MISMATCH / MISSING / HOSPITAL_SHOULD_PROVIDE item can never become an upload.
ALTER TABLE actions ADD COLUMN match_decision_id TEXT REFERENCES decisions(id) ON DELETE RESTRICT;

CREATE TRIGGER trg_actions_require_match
BEFORE INSERT ON actions
WHEN NEW.match_decision_id IS NULL
 OR NOT EXISTS (
    SELECT 1 FROM decisions d
    WHERE d.id = NEW.match_decision_id AND d.kind = 'match' AND d.result = 'MATCHED'
      AND d.run_id = NEW.run_id AND d.subject_ref = NEW.requirement_id
      AND json_extract(d.input_refs, '$.matched_sha256') = NEW.file_sha256
 )
BEGIN
    SELECT RAISE(ABORT, 'uploads need a MATCHED match decision for this run, requirement and file');
END;
"""

MIGRATION_5 = f"""
-- One action per claim, requirement and file, whatever the key encoding (the key is derived from these).
CREATE UNIQUE INDEX uq_actions_binding ON actions (claim_id, requirement_id, file_sha256);

-- A person's approve/reject decision, bound to exactly one action's claim, requirement and file,
-- with the card they were shown.
CREATE TABLE action_approvals (
    id              TEXT PRIMARY KEY,
    action_id       TEXT NOT NULL UNIQUE REFERENCES actions(id) ON DELETE RESTRICT,
    decision        TEXT NOT NULL CHECK (decision IN {_in(ApprovalDecision)}),
    decided_by      TEXT NOT NULL REFERENCES people(id) ON DELETE RESTRICT,
    decided_at      TEXT NOT NULL CHECK (decided_at GLOB '{TS}'),
    idem_key        TEXT NOT NULL CHECK {_hex64('idem_key')},
    claim_id        TEXT NOT NULL CHECK (claim_id GLOB '{CLAIM}'),
    requirement_id  TEXT NOT NULL,
    file_sha256     TEXT NOT NULL CHECK {_hex64('file_sha256')},
    vault_file_id   TEXT REFERENCES vault_files(id) ON DELETE RESTRICT,
    card_sha256     TEXT CHECK (card_sha256 IS NULL OR {_hex64('card_sha256')}),
    card_json       TEXT CHECK (card_json IS NULL OR json_valid(card_json)),
    CHECK ((card_sha256 IS NULL) = (card_json IS NULL))
);

-- Approvals recorded before this migration (approved_by on the action) keep working.
INSERT INTO action_approvals (id, action_id, decision, decided_by, decided_at, idem_key, claim_id,
                              requirement_id, file_sha256, vault_file_id)
SELECT 'APR-' || substr(id, 5), id, 'approved', approved_by, approved_at, idem_key, claim_id, requirement_id,
       file_sha256, vault_file_id
FROM actions WHERE approved_by IS NOT NULL AND approved_at IS NOT NULL;

CREATE TRIGGER trg_action_approvals_bound
BEFORE INSERT ON action_approvals
WHEN NOT EXISTS (
    SELECT 1 FROM actions a
    WHERE a.id = NEW.action_id AND a.state = 'PROPOSED' AND a.idem_key = NEW.idem_key
      AND a.claim_id = NEW.claim_id AND a.requirement_id = NEW.requirement_id
      AND a.file_sha256 = NEW.file_sha256 AND a.vault_file_id IS NEW.vault_file_id
)
BEGIN
    SELECT RAISE(ABORT, 'an approval is bound to one proposed action and its exact claim, requirement and file');
END;

CREATE TRIGGER trg_action_approvals_append_only
BEFORE UPDATE ON action_approvals
BEGIN
    SELECT RAISE(ABORT, 'approvals are append-only');
END;

-- An action never changes what it uploads, where, or who approved it.
CREATE TRIGGER trg_actions_binding_immutable
BEFORE UPDATE OF idem_key, run_id, claim_id, requirement_id, file_sha256, vault_file_id, match_decision_id,
                 approved_by, approved_at ON actions
WHEN NEW.idem_key IS NOT OLD.idem_key OR NEW.run_id IS NOT OLD.run_id OR NEW.claim_id IS NOT OLD.claim_id
  OR NEW.requirement_id IS NOT OLD.requirement_id OR NEW.file_sha256 IS NOT OLD.file_sha256
  OR NEW.vault_file_id IS NOT OLD.vault_file_id OR NEW.match_decision_id IS NOT OLD.match_decision_id
  OR (OLD.approved_by IS NOT NULL AND NEW.approved_by IS NOT OLD.approved_by)
  OR (OLD.approved_at IS NOT NULL AND NEW.approved_at IS NOT OLD.approved_at)
BEGIN
    SELECT RAISE(ABORT, 'an action is bound to one claim, requirement, file and approval; it cannot be re-pointed');
END;

-- Once out of PROPOSED (approved, executing, unknown...), an action is never reopened; terminal states stay.
CREATE TRIGGER trg_actions_never_reopen
BEFORE UPDATE OF state ON actions
WHEN (NEW.state = 'PROPOSED' AND OLD.state <> 'PROPOSED')
  OR (OLD.state IN ('VERIFIED', 'FAILED', 'REJECTED') AND NEW.state IS NOT OLD.state)
BEGIN
    SELECT RAISE(ABORT, 'an action that left PROPOSED is never reopened, and a finished action never changes');
END;

CREATE TRIGGER trg_actions_insert_proposed
BEFORE INSERT ON actions
WHEN NEW.state <> 'PROPOSED' AND NEW.approved_by IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'actions start as PROPOSED; approval is recorded separately');
END;

CREATE TRIGGER trg_actions_decision_recorded
BEFORE UPDATE OF state ON actions
WHEN OLD.state = 'PROPOSED' AND NEW.state IN ('APPROVED', 'REJECTED')
 AND NOT EXISTS (
    SELECT 1 FROM action_approvals p
    WHERE p.action_id = NEW.id
      AND p.decision = CASE NEW.state WHEN 'APPROVED' THEN 'approved' ELSE 'rejected' END
      AND (NEW.state = 'REJECTED' OR p.decided_by = NEW.approved_by)
 )
BEGIN
    SELECT RAISE(ABORT, 'approve or reject needs a recorded decision by a person in action_approvals');
END;

CREATE TRIGGER trg_actions_execute_needs_approval
BEFORE UPDATE OF state ON actions
WHEN NEW.state = 'EXECUTING'
 AND NOT EXISTS (
    SELECT 1 FROM action_approvals p
    WHERE p.action_id = NEW.id AND p.decision = 'approved' AND p.decided_by = NEW.approved_by
      AND p.idem_key = NEW.idem_key AND p.claim_id = NEW.claim_id AND p.requirement_id = NEW.requirement_id
      AND p.file_sha256 = NEW.file_sha256 AND p.vault_file_id IS NEW.vault_file_id
 )
BEGIN
    SELECT RAISE(ABORT, 'uploads run only under a recorded human approval for this exact claim, requirement and file');
END;

-- Every eligibility check run for an action (at approval and before each attempt), passed or not.
CREATE TABLE action_checks (
    id                   TEXT PRIMARY KEY,
    action_id            TEXT NOT NULL REFERENCES actions(id) ON DELETE RESTRICT,
    phase                TEXT NOT NULL CHECK (phase IN {_in(CheckPhase)}),
    checked_at           TEXT NOT NULL CHECK (checked_at GLOB '{TS}'),
    passed               INTEGER NOT NULL CHECK (passed IN (0, 1)),
    checks_json          TEXT NOT NULL CHECK (json_valid(checks_json) AND json_type(checks_json) = 'array'),
    evidence_id          TEXT REFERENCES evidence(id) ON DELETE RESTRICT,
    consent_decision_id  TEXT REFERENCES decisions(id) ON DELETE RESTRICT
);
CREATE INDEX idx_action_checks_action ON action_checks (action_id, checked_at);

CREATE TRIGGER trg_action_checks_append_only
BEFORE UPDATE ON action_checks
BEGIN
    SELECT RAISE(ABORT, 'action checks are append-only');
END;

-- One row per upload attempt, opened in the same transaction that moves the action to EXECUTING.
CREATE TABLE action_attempts (
    id            TEXT PRIMARY KEY,
    action_id     TEXT NOT NULL REFERENCES actions(id) ON DELETE RESTRICT,
    attempt_no    INTEGER NOT NULL CHECK (attempt_no >= 1),
    check_id      TEXT NOT NULL REFERENCES action_checks(id) ON DELETE RESTRICT,
    started_at    TEXT NOT NULL CHECK (started_at GLOB '{TS}'),
    deadline_ms   INTEGER NOT NULL CHECK (deadline_ms > 0),
    finished_at   TEXT CHECK (finished_at IS NULL OR finished_at GLOB '{TS}'),
    outcome       TEXT CHECK (outcome IS NULL OR outcome IN {_in(AttemptOutcome)}),
    http_status   INTEGER CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    error_code    TEXT,
    message       TEXT,
    evidence_id   TEXT REFERENCES evidence(id) ON DELETE RESTRICT,
    UNIQUE (action_id, attempt_no),
    CHECK ((finished_at IS NULL) = (outcome IS NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
-- At most one attempt in flight per action: a second concurrent attempt cannot even be recorded.
CREATE UNIQUE INDEX uq_action_attempts_open ON action_attempts (action_id) WHERE finished_at IS NULL;

CREATE TRIGGER trg_action_attempts_need_executing
BEFORE INSERT ON action_attempts
WHEN NOT EXISTS (SELECT 1 FROM actions a WHERE a.id = NEW.action_id AND a.state = 'EXECUTING'
                 AND a.attempts = NEW.attempt_no)
  OR NOT EXISTS (SELECT 1 FROM action_checks c WHERE c.id = NEW.check_id AND c.action_id = NEW.action_id
                 AND c.phase = 'execute' AND c.passed = 1)
BEGIN
    SELECT RAISE(ABORT, 'an upload attempt needs an EXECUTING action and a passed pre-execution check');
END;

CREATE TRIGGER trg_action_attempts_finish_once
BEFORE UPDATE ON action_attempts
WHEN OLD.finished_at IS NOT NULL OR NEW.action_id IS NOT OLD.action_id OR NEW.attempt_no IS NOT OLD.attempt_no
  OR NEW.check_id IS NOT OLD.check_id OR NEW.started_at IS NOT OLD.started_at
  OR NEW.deadline_ms IS NOT OLD.deadline_ms
BEGIN
    SELECT RAISE(ABORT, 'an attempt is recorded once: only its outcome may be added, a single time');
END;
"""

MIGRATION_6 = f"""
-- One row per workflow node execution: what it was given, what it decided, what it produced.
-- A node that waits for a person is recorded INTERRUPTED, and again when it resumes.
CREATE TABLE graph_steps (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    seq           INTEGER NOT NULL CHECK (seq >= 1),
    node          TEXT NOT NULL CHECK (length(node) BETWEEN 1 AND 40),
    started_at    TEXT NOT NULL CHECK (started_at GLOB '{TS}'),
    finished_at   TEXT NOT NULL CHECK (finished_at GLOB '{TS}'),
    result        TEXT NOT NULL CHECK (result IN {_in(StepResult)}),
    decision      TEXT NOT NULL CHECK (length(trim(decision)) > 0),
    next_node     TEXT,
    input_refs    TEXT NOT NULL CHECK (json_valid(input_refs) AND json_type(input_refs) = 'object'),
    output_refs   TEXT NOT NULL CHECK (json_valid(output_refs) AND json_type(output_refs) = 'object'),
    evidence_ids  TEXT NOT NULL CHECK (json_valid(evidence_ids) AND json_type(evidence_ids) = 'array'),
    error         TEXT,
    UNIQUE (run_id, seq),
    CHECK (finished_at >= started_at)
);

CREATE TRIGGER trg_graph_steps_append_only
BEFORE UPDATE ON graph_steps
BEGIN
    SELECT RAISE(ABORT, 'workflow steps are append-only');
END;
"""

MIGRATION_7 = f"""
-- Verdicts of the official and edge scenarios (pytest -m scenario). Only executed cases have a row.
CREATE TABLE scenario_results (
    id             TEXT PRIMARY KEY,
    test_case      TEXT NOT NULL CHECK (test_case GLOB 'TC[0-9]*' OR test_case GLOB 'E[0-9]*'),
    title          TEXT NOT NULL CHECK (length(trim(title)) > 0),
    run_id         TEXT REFERENCES runs(id) ON DELETE RESTRICT,
    input_json     TEXT NOT NULL CHECK (json_valid(input_json) AND json_type(input_json) = 'object'),
    expected       TEXT NOT NULL CHECK (length(trim(expected)) > 0),
    actual         TEXT NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAIL')),
    checks_json    TEXT NOT NULL CHECK (json_valid(checks_json) AND json_type(checks_json) = 'array'),
    evidence_json  TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
    recorded_at    TEXT NOT NULL CHECK (recorded_at GLOB '{TS}')
);
CREATE INDEX idx_scenario_results_case ON scenario_results (test_case, recorded_at);

CREATE TRIGGER trg_scenario_results_append_only
BEFORE UPDATE ON scenario_results
BEGIN
    SELECT RAISE(ABORT, 'scenario results are append-only');
END;
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema: people, consents, runs, evidence, vault_files, decisions, actions", MIGRATION_1),
    Migration(2, "claim registry, consent requests, consent backstop trigger", MIGRATION_2),
    Migration(3, "extractions: raw text, parsed fields, method and validation errors", MIGRATION_3),
    Migration(4, "uploads must come from a MATCHED match decision", MIGRATION_4),
    Migration(5, "bound human approvals, pre-execution checks and upload attempts", MIGRATION_5),
    Migration(6, "workflow node steps", MIGRATION_6),
    Migration(7, "scenario results for the official and edge test cases", MIGRATION_7),
)

LATEST_VERSION = MIGRATIONS[-1].version

# Tables Phase 1 created without versioning; migration 1 replaces them if they are empty.
LEGACY_TABLES = ("people", "consents", "runs", "evidence", "vault_files", "decisions", "actions")
