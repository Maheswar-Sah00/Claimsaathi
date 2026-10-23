"""SQLite access: connections, transactions, versioned migrations and a health check.

Conventions
- Foreign keys are on for every connection.
- Writes go through `Database.transaction()` (BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any error).
- Timestamps are UTC strings like 2026-10-22T08:30:00Z; the schema rejects anything else.
- SQLite holds file metadata and safe relative paths, never file contents.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.migrations import LATEST_VERSION, LEGACY_TABLES, MIGRATIONS

logger = logging.getLogger("agent.db")

EXPECTED_TABLES = (
    "action_approvals", "action_attempts", "action_checks", "actions", "claims", "consent_requests", "consents", "decisions", "evidence", "extractions", "graph_steps", "people", "runs", "scenario_results",
    "schema_migrations", "vault_files",
)


class DatabaseError(Exception):
    """Base class for data-layer errors."""


class SchemaError(DatabaseError):
    """The database file cannot be brought to the expected schema."""


# ---------------- time ----------------


def utc_now() -> str:
    """Current UTC time in the stored format."""
    return to_utc_iso(datetime.now(timezone.utc))


def to_utc_iso(value: datetime) -> str:
    """Convert an aware datetime to the stored UTC format. Naive datetimes are refused."""
    if value.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone before storing")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """Parse a stored UTC timestamp."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------- connections ----------------


class Database:
    """A SQLite database file and how to open it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        """Open a connection in manual-transaction mode with foreign keys enforced."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit of work: everything inside commits together or not at all."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads only."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


# ---------------- migrations ----------------


def schema_version(conn: sqlite3.Connection) -> int:
    """The applied schema version (0 for a new or pre-versioned file)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def _clear_empty_legacy_tables(conn: sqlite3.Connection) -> None:
    """Phase 1 created unversioned tables. Drop them only if they hold no rows."""
    legacy = [t for t in LEGACY_TABLES if t in _existing_tables(conn)]
    if not legacy:
        return
    non_empty = [t for t in legacy if conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]]  # noqa: S608
    if non_empty:
        raise SchemaError(
            f"Unversioned tables with data found ({', '.join(non_empty)}). "
            "Back up and delete the database file, then run `python -m agent.cli init-db`."
        )
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in legacy:
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA foreign_keys = ON")
    logger.info("dropped empty pre-versioned tables", extra={"tables": legacy})


def migrate(db: Database) -> int:
    """Apply pending migrations in order. Returns the resulting version. Idempotent."""
    with db.read() as conn:
        current = schema_version(conn)
        if current > LATEST_VERSION:
            raise SchemaError(f"Database version {current} is newer than this code ({LATEST_VERSION}).")
        if current == 0:
            _clear_empty_legacy_tables(conn)
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                f"INSERT INTO schema_migrations (version, name, applied_at) "
                f"VALUES ({migration.version}, '{migration.name}', '{utc_now()}');\n"
                f"PRAGMA user_version = {migration.version};\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise SchemaError(f"Migration {migration.version} ({migration.name}) failed: {exc}") from exc
            conn.execute("PRAGMA foreign_keys = ON")  # executescript does not reset it, but be explicit
            logger.info("applied migration", extra={"version": migration.version})
            current = migration.version
        return current


# ---------------- health ----------------


def health(db: Database) -> dict[str, Any]:
    """Check file, schema version, tables, foreign keys and integrity. Never raises."""
    if not db.path.exists():
        return {"ok": False, "detail": "database file missing; run init-db"}
    try:
        with db.read() as conn:
            version = schema_version(conn)
            missing = sorted(set(EXPECTED_TABLES) - _existing_tables(conn))
            fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        return {"ok": False, "detail": f"sqlite error: {exc}"}
    ok = version == LATEST_VERSION and not missing and fk_on and fk_violations == 0 and integrity == "ok"
    detail = "ok" if ok else (
        f"version {version}/{LATEST_VERSION}, missing tables {missing}, "
        f"foreign_keys {'on' if fk_on else 'off'}, fk violations {fk_violations}, integrity {integrity}"
    )
    return {
        "ok": ok,
        "detail": detail,
        "schema_version": version,
        "expected_version": LATEST_VERSION,
        "foreign_keys": fk_on,
        "fk_violations": fk_violations,
        "integrity": integrity,
    }
