"""Schema creation, migrations, constraints, foreign keys, transactions and health."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent import repository as repo
from agent.db import Database, SchemaError, health, migrate, schema_version
from agent.migrations import LATEST_VERSION
from agent.seed import init_database
from tests.conftest import allow_portal, matched

TS = "2026-10-22T08:00:00Z"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    init_database(database, tmp_path / "vault")
    return database


def test_fresh_database_has_all_tables_indexes_and_version(db: Database) -> None:
    with db.read() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert schema_version(conn) == LATEST_VERSION
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")] == list(
            range(1, LATEST_VERSION + 1))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert {"people", "consents", "evidence", "vault_files", "decisions", "actions", "runs"} <= tables
    assert {"idx_consents_lookup", "idx_runs_claim", "idx_evidence_run", "idx_decisions_run",
            "idx_actions_run", "idx_actions_claim_req", "idx_vault_owner"} <= indexes
    assert {"trg_decisions_evidence_same_run", "trg_decisions_append_only", "trg_evidence_append_only"} <= triggers


def test_vault_table_stores_metadata_not_file_bytes(db: Database) -> None:
    with db.read() as conn:
        columns = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(vault_files)")}
    assert "BLOB" not in columns.values()
    assert {"storage_path", "sha256", "size_bytes"} <= set(columns)


def test_migrate_is_idempotent(db: Database) -> None:
    assert migrate(db) == LATEST_VERSION
    assert migrate(db) == LATEST_VERSION
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == LATEST_VERSION


def test_empty_phase1_tables_are_replaced(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT, role TEXT, phone TEXT)")
        conn.execute("CREATE TABLE actions (idem_key TEXT PRIMARY KEY)")
    assert migrate(Database(path)) == LATEST_VERSION
    with Database(path).read() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
    assert "contact_ref" in cols and "phone" not in cols


def test_phase1_tables_with_data_are_not_dropped(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO people VALUES ('x', 'kept')")
    with pytest.raises(SchemaError, match="Unversioned tables with data"):
        migrate(Database(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM people").fetchone()[0] == "kept"


def test_newer_database_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    with pytest.raises(SchemaError, match="newer than this code"):
        migrate(Database(path))


@pytest.mark.parametrize(
    "sql, params",
    [
        ("INSERT INTO people VALUES ('P-X', 'X', 'doctor', 'synthetic:x', NULL, ?)", (TS,)),
        ("INSERT INTO people VALUES ('P-X', 'X', 'family', '+91 98765 43210', NULL, ?)", (TS,)),
        ("INSERT INTO people VALUES ('P-X', 'X', 'family', 'synthetic:x', NULL, '22/10/2026')", ()),
        ("INSERT INTO consents (id, patient_id, grantee_id, policy_no, claim_id, actions, granted_at, expires_at) "
         "VALUES ('C1', 'P-ARJUN', 'P-ARJUN', 'POL', '*', '[\"read_status\"]', ?, '2027-01-01T00:00:00Z')", (TS,)),
        ("INSERT INTO consents (id, patient_id, grantee_id, policy_no, claim_id, actions, granted_at, expires_at) "
         "VALUES ('C1', 'P-RAMESH', 'P-ARJUN', 'POL', '*', 'not json', ?, '2027-01-01T00:00:00Z')", (TS,)),
        ("INSERT INTO consents (id, patient_id, grantee_id, policy_no, claim_id, actions, granted_at, expires_at) "
         "VALUES ('C1', 'P-RAMESH', 'P-ARJUN', 'POL', '*', '[\"read_status\"]', ?, '2020-01-01T00:00:00Z')", (TS,)),
        ("INSERT INTO runs (id, requester_id, claim_id, started_at, final_status) "
         "VALUES ('R1', 'P-ARJUN', 'CLM-2026-0142', ?, 'PROBABLY_FINE')", (TS,)),
        ("INSERT INTO runs (id, requester_id, claim_id, started_at, state) "
         "VALUES ('R1', 'P-ARJUN', 'CLM-2026-0142', ?, 'completed')", (TS,)),
        ("INSERT INTO vault_files (id, owner_id, storage_path, original_name, sha256, size_bytes, content_type, added_at) "
         "VALUES ('V1', 'P-ARJUN', '../../etc/passwd', 'x', ?, 1, 'application/pdf', ?)", ("a" * 64, TS)),
    ],
    ids=["bad-role", "real-phone", "non-utc-time", "self-consent", "actions-not-json", "expires-before-grant",
         "unknown-status", "terminal-without-finish", "path-traversal"],
)
def test_check_constraints_reject_bad_rows(db: Database, sql: str, params: tuple) -> None:
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(sql, params)


def test_action_cannot_pass_proposed_without_approval_at_sql_level(db: Database) -> None:
    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
        match_id = matched(conn, run.id, "REQ-0142-01", "c" * 64)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.transaction() as conn:
        conn.execute(
            "INSERT INTO actions (id, idem_key, run_id, claim_id, requirement_id, file_sha256, state, attempts, "
            "created_at, updated_at, match_decision_id) "
            "VALUES ('A1', ?, ?, 'CLM-2026-0142', 'REQ-0142-01', ?, 'EXECUTING', 1, ?, ?, ?)",
            ("b" * 64, run.id, "c" * 64, TS, TS, match_id),
        )


def test_upload_action_needs_a_matched_decision(db: Database) -> None:
    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
        mismatch = repo.record_decision(conn, run_id=run.id, kind="match", result="MISMATCH", reason="wrong patient",
                                        subject_ref="REQ-0142-01", evidence_state="not_applicable",
                                        input_refs={"matched_sha256": "c" * 64}).id
    for decision_id in (None, mismatch):
        with pytest.raises(repo.ConstraintViolation, match="MATCHED match decision"), db.transaction() as conn:
            repo.propose_action(conn, run_id=run.id, claim_id="CLM-2026-0142", requirement_id="REQ-0142-01",
                                file_sha256="c" * 64, match_decision_id=decision_id)


def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        conn.execute("INSERT INTO runs (id, requester_id, claim_id, started_at) VALUES ('R1', 'P-NOBODY', "
                     "'CLM-2026-0142', ?)", (TS,))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.transaction() as conn:
        conn.execute("DELETE FROM people WHERE id = 'P-ARJUN'")  # still referenced by consents and vault


def test_transaction_rolls_back_everything_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
        repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0163")
        raise RuntimeError("crash mid-way")
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_transaction_commits_several_writes_together(db: Database) -> None:
    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
        allow_portal(conn, run.id)
        repo.add_evidence(conn, run_id=run.id, kind="portal_page", url="http://portal/claims/CLM-2026-0142",
                          html_sha256="d" * 64)
    with db.read() as conn:
        assert len(repo.evidence_for_run(conn, run.id)) == 1


def test_health_ok_then_detects_wrong_version(db: Database) -> None:
    assert health(db)["ok"] is True
    with db.read() as conn:
        conn.execute("PRAGMA user_version = 0")
    report = health(db)
    assert report["ok"] is False and f"version 0/{LATEST_VERSION}" in report["detail"]


def test_health_reports_missing_file(tmp_path: Path) -> None:
    assert health(Database(tmp_path / "absent.db")) == {"ok": False, "detail": "database file missing; run init-db"}
