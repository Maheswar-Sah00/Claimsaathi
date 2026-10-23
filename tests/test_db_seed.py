"""Seed determinism, idempotent init, reset reproducibility and the CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent import cli
from agent import repository as repo
from agent.db import Database
from agent.seed import DOCUMENTS, init_database, reset_demo_data
from tests.conftest import allow_portal
from tests.conftest import matched

DATA_TABLES = ("people", "claims", "consents", "consent_requests", "runs", "evidence", "vault_files", "decisions",
               "actions")


def snapshot(db: Database) -> dict[str, list[tuple]]:
    """Every row of every data table, in a stable order."""
    with db.read() as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in DATA_TABLES}


def test_seed_contents(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    result = init_database(db, tmp_path / "vault")
    assert result["seed_inserted"] == {"people": 3, "claims": 3, "consents": 2, "vault_files": 6}
    with db.read() as conn:
        assert [p.id for p in repo.list_people(conn)] == ["P-ARJUN", "P-LAKSHMI", "P-RAMESH"]
        files = repo.vault_files_for(conn, "P-ARJUN")
    assert all(f.extraction_source == "seed_label" for f in files)
    for f in files:
        data = (tmp_path / "vault" / f.storage_path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f.sha256 and len(data) == f.size_bytes
        if f.content_type == "image/png":
            assert data.startswith(bytes([0x89]) + b"PNG") and f.legible is False
        else:
            assert data.startswith(b"%PDF-") and b"SYNTHETIC DEMO DOCUMENT" in data


def test_repeated_init_adds_no_duplicates(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    init_database(db, tmp_path / "vault")
    first = snapshot(db)
    again = init_database(db, tmp_path / "vault")
    assert again["seed_inserted"] == {"people": 0, "claims": 0, "consents": 0, "vault_files": 0}
    assert again["seed_files_written"] == 0
    assert snapshot(db) == first


def test_reset_is_reproducible_and_removes_run_data(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    vault = tmp_path / "vault"
    init_database(db, vault)
    fresh = snapshot(db)

    with db.transaction() as conn:
        run = repo.create_run(conn, requester_id="P-ARJUN", claim_id="CLM-2026-0142")
        allow_portal(conn, run.id)
        ev = repo.add_evidence(conn, run_id=run.id, kind="portal_page", url="u", html_sha256="a" * 64)
        repo.record_decision(conn, run_id=run.id, kind="status", result="QUERY_RAISED", reason="r", evidence_id=ev.id)
        repo.propose_action(conn, run_id=run.id, claim_id="CLM-2026-0142", requirement_id="REQ-0142-01",
                            file_sha256="b" * 64,
            match_decision_id=matched(conn, run.id, "REQ-0142-01", "b" * 64))
        repo.revoke_consent(conn, "CON-SEED-01")
    (vault / DOCUMENTS[0].storage_path).write_bytes(b"%PDF-tampered")

    reset_demo_data(db, vault)
    after_first = snapshot(db)
    reset_demo_data(db, vault)
    assert after_first == fresh == snapshot(db)
    data = (vault / DOCUMENTS[0].storage_path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == fresh["vault_files"][0][4]  # file restored


def test_seed_is_identical_across_separate_databases(tmp_path: Path) -> None:
    a, b = Database(tmp_path / "a.db"), Database(tmp_path / "b.db")
    init_database(a, tmp_path / "va")
    init_database(b, tmp_path / "vb")
    assert snapshot(a) == snapshot(b)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    return tmp_path


def test_cli_init_health_reset(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["db-health"]) == 1
    capsys.readouterr()
    assert cli.main(["init-db"]) == 0
    assert json.loads(capsys.readouterr().out)["seed_inserted"]["people"] == 3
    assert cli.main(["db-health"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["vault_files"] == 6
    assert cli.main(["reset-demo"]) == 2
    assert cli.main(["reset-demo", "--yes"]) == 0
