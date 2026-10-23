"""Deterministic synthetic seed data for the agent database, plus init and reset.

The seed mirrors the demo portal's family (same names and member ids) but lives in
the agent's own database; the two apps never share storage. Fixed ids and fixed
timestamps make every reset produce byte-identical rows and files.

Scenario coverage
- Ramesh -> Arjun: read_status + upload_docs for CLM-2026-0142 only (TC1, TC4).
- Ramesh -> Arjun: read_status only for CLM-2026-0163 (TC3; an upload there is ACTION_NOT_ALLOWED).
- No Lakshmi -> Arjun consent (TC2: asking about CLM-2026-0150 is blocked with NEEDS_CONSENT).
- Vault (all synthetic): a correct pharmacy invoice for Ramesh (TC1), Lakshmi's invoice (wrong
  patient, E1), an invoice dated outside the admission, a discharge summary, a blurry photo of
  an invoice (PNG, unreadable) and indoor case papers (hospital-held for cashless claims, E6).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent import repository as repo
from agent.db import Database, migrate
from agent.models import ConsentAction, ExtractionSource
from shared.synthetic_pdf import synthetic_pdf
from shared.synthetic_png import blurry_document_png

logger = logging.getLogger("agent.seed")

SEED_TIME = "2026-10-14T06:00:00Z"
POLICY_NO = "POL-HDF-778812"
SEED_VAULT_PREFIX = "seed"

PEOPLE: tuple[dict[str, str], ...] = (
    {"id": "P-RAMESH", "name": "Ramesh", "role": "patient", "contact_ref": "synthetic:ph-ramesh", "portal_member_id": "M-01"},
    {"id": "P-LAKSHMI", "name": "Lakshmi", "role": "patient", "contact_ref": "synthetic:ph-lakshmi", "portal_member_id": "M-02"},
    {"id": "P-ARJUN", "name": "Arjun", "role": "family", "contact_ref": "synthetic:ph-arjun", "portal_member_id": "M-03"},
)

# Trusted claim registry: lets the consent gate find the patient without reading the claim page.
CLAIMS: tuple[dict[str, str], ...] = (
    {"claim_id": "CLM-2026-0142", "policy_no": POLICY_NO, "patient_id": "P-RAMESH", "source": "seed"},
    {"claim_id": "CLM-2026-0150", "policy_no": POLICY_NO, "patient_id": "P-LAKSHMI", "source": "seed"},
    {"claim_id": "CLM-2026-0163", "policy_no": POLICY_NO, "patient_id": "P-RAMESH", "source": "seed"},
)

CONSENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "CON-SEED-01",
        "patient_id": "P-RAMESH",
        "grantee_id": "P-ARJUN",
        "policy_no": POLICY_NO,
        "claim_id": "CLM-2026-0142",
        "actions": [ConsentAction.READ_STATUS.value, ConsentAction.UPLOAD_DOCS.value],
        "granted_at": SEED_TIME,
        "expires_at": "2027-04-14T06:00:00Z",
    },
    {
        "id": "CON-SEED-02",
        "patient_id": "P-RAMESH",
        "grantee_id": "P-ARJUN",
        "policy_no": POLICY_NO,
        "claim_id": "CLM-2026-0163",
        "actions": [ConsentAction.READ_STATUS.value],
        "granted_at": SEED_TIME,
        "expires_at": "2027-04-14T06:00:00Z",
    },
)


@dataclass(frozen=True)
class SeedDocument:
    """A synthetic family document: what it says, and the labelled fields it carries."""

    id: str
    filename: str
    lines: tuple[str, ...]
    fields: dict[str, Any]
    image: bool = False

    @property
    def storage_path(self) -> str:
        return f"{SEED_VAULT_PREFIX}/arjun/{self.filename}"

    @property
    def content_type(self) -> str:
        return "image/png" if self.image else "application/pdf"

    @property
    def data(self) -> bytes:
        if self.image:
            return blurry_document_png()
        return synthetic_pdf(["SYNTHETIC DEMO DOCUMENT - NOT A REAL RECORD", *self.lines])


SUNRISE = "Sunrise Multispeciality Hospital (demo)"
DOCUMENTS: tuple[SeedDocument, ...] = (
    SeedDocument(
        "VF-SEED-01", "pharmacy_invoice_ramesh.pdf",
        ("Pharmacy invoice INV-PH-2291", "Patient: Ramesh", "Date: 2026-10-18", f"{SUNRISE} pharmacy", "Total: Rs 4,860"),
        {"doc_type": "pharmacy_invoice", "patient_name": "Ramesh", "doc_date": "2026-10-18",
         "invoice_no": "INV-PH-2291", "hospital": SUNRISE, "amount_inr": 4860, "legible": True},
    ),
    SeedDocument(
        "VF-SEED-02", "pharmacy_invoice_lakshmi.pdf",
        ("Pharmacy invoice INV-PH-1874", "Patient: Lakshmi", "Date: 2026-10-05", "Lakeview Hospital (demo) pharmacy",
         "Total: Rs 2,310"),
        {"doc_type": "pharmacy_invoice", "patient_name": "Lakshmi", "doc_date": "2026-10-05",
         "invoice_no": "INV-PH-1874", "hospital": "Lakeview Hospital (demo)", "amount_inr": 2310, "legible": True},
    ),
    SeedDocument(
        "VF-SEED-03", "pharmacy_invoice_ramesh_august.pdf",
        ("Pharmacy invoice INV-PH-0912", "Patient: Ramesh", "Date: 2026-08-02", f"{SUNRISE} pharmacy", "Total: Rs 1,290"),
        {"doc_type": "pharmacy_invoice", "patient_name": "Ramesh", "doc_date": "2026-08-02",
         "invoice_no": "INV-PH-0912", "hospital": SUNRISE, "amount_inr": 1290, "legible": True},
    ),
    SeedDocument(
        "VF-SEED-04", "discharge_summary_ramesh.pdf",
        ("Discharge summary", "Patient: Ramesh", "Admitted: 2026-10-14", "Discharged: 2026-10-18", SUNRISE),
        {"doc_type": "discharge_summary", "patient_name": "Ramesh", "doc_date": "2026-10-18",
         "hospital": SUNRISE, "legible": True},
    ),
    SeedDocument(
        "VF-SEED-05", "pharmacy_invoice_ramesh_blurry_photo.png",
        ("[synthetic blurry photo: text unreadable]",),
        {"doc_type": "pharmacy_invoice", "legible": False},
        image=True,
    ),
    SeedDocument(
        "VF-SEED-06", "indoor_case_papers_ramesh.pdf",
        ("Indoor case papers (extract)", "Patient: Ramesh", "Ward notes 2026-10-14 to 2026-10-18", SUNRISE,
         "Prepared: 2026-10-18"),
        {"doc_type": "indoor_case_papers", "patient_name": "Ramesh", "doc_date": "2026-10-18",
         "hospital": SUNRISE, "legible": True},
    ),
)


def write_seed_files(vault_dir: Path) -> int:
    """Write the seed PDFs under `vault_dir/seed/`. Only rewrites files whose bytes differ."""
    written = 0
    for doc in DOCUMENTS:
        target = vault_dir / doc.storage_path
        data = doc.data
        if target.exists() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
    return written


def _seed_rows(conn: Any) -> dict[str, int]:
    """Insert any seed rows that are missing. Existing rows are left alone."""
    inserted = {"people": 0, "claims": 0, "consents": 0, "vault_files": 0}
    for person in PEOPLE:
        if conn.execute("SELECT 1 FROM people WHERE id = ?", (person["id"],)).fetchone() is None:
            repo.create_person(conn, created_at=SEED_TIME, **person)
            inserted["people"] += 1
    for claim in CLAIMS:
        if conn.execute("SELECT 1 FROM claims WHERE claim_id = ?", (claim["claim_id"],)).fetchone() is None:
            repo.register_claim(conn, registered_at=SEED_TIME, **claim)
            inserted["claims"] += 1
    for consent in CONSENTS:
        if conn.execute("SELECT 1 FROM consents WHERE id = ?", (consent["id"],)).fetchone() is None:
            repo.create_consent(conn, **consent)
            inserted["consents"] += 1
    for doc in DOCUMENTS:
        if conn.execute("SELECT 1 FROM vault_files WHERE id = ?", (doc.id,)).fetchone() is None:
            data = doc.data
            repo.add_vault_file(
                conn,
                id=doc.id,
                owner_id="P-ARJUN",
                storage_path=doc.storage_path,
                original_name=doc.filename,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                content_type=doc.content_type,
                added_at=SEED_TIME,
                extraction_source=ExtractionSource.SEED_LABEL.value,
                **doc.fields,
            )
            inserted["vault_files"] += 1
    return inserted


def init_database(db: Database, vault_dir: Path) -> dict[str, Any]:
    """Migrate to the latest schema and add missing seed data. Safe to run repeatedly."""
    version = migrate(db)
    files = write_seed_files(vault_dir)
    with db.transaction() as conn:
        inserted = _seed_rows(conn)
    logger.info("database initialised", extra={"schema_version": version, "seed_inserted": inserted,
                                               "seed_files_written": files})
    return {"schema_version": version, "seed_inserted": inserted, "seed_files_written": files}


def clear_evidence(evidence_dir: Path) -> int:
    """Delete per-run evidence folders (RUN-*) under the evidence directory. Other files stay."""
    removed = 0
    if evidence_dir.is_dir():
        for child in evidence_dir.iterdir():
            if child.is_dir() and child.name.startswith("RUN-"):
                shutil.rmtree(child)
                removed += 1
    return removed


def reset_scenario_state(db: Database, vault_dir: Path) -> dict[str, Any]:
    """Isolation between scenario cases: restore the seed vault files and labels and the seed consents
    (un-revoked), and remove upload actions. Runs, steps, decisions and evidence are kept for review.

    Needed because an upload's idempotency key is permanent: the same file can never be sent to the
    same requirement twice, so TC4 (TC1 with a timeout) needs TC1's action gone. Demo tooling only."""
    files = write_seed_files(vault_dir)
    with db.transaction() as conn:
        removed = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        for table in ("action_attempts", "action_checks", "action_approvals", "actions"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names, FK-safe order
        for consent in CONSENTS:
            conn.execute("UPDATE consents SET revoked = 0, revoked_at = NULL WHERE id = ?", (consent["id"],))
        for doc in DOCUMENTS:
            fields = {k: doc.fields.get(k) for k in ("doc_type", "patient_name", "doc_date", "invoice_no",
                                                     "hospital", "amount_inr", "legible")}
            repo.update_vault_fields(conn, doc.id, source=ExtractionSource.SEED_LABEL.value, **fields)
    logger.info("scenario state reset", extra={"actions_removed": removed, "seed_files_written": files})
    return {"actions_removed": removed, "seed_files_written": files}


def reset_demo_data(db: Database, vault_dir: Path) -> dict[str, Any]:
    """Delete every row and restore the seed, in one transaction. Seed files are rewritten;
    other files in the vault folder are left on disk."""
    version = migrate(db)
    files = write_seed_files(vault_dir)
    with db.transaction() as conn:
        for table in ("scenario_results", "graph_steps", "action_attempts", "action_checks", "action_approvals", "actions", "decisions", "extractions", "evidence", "consent_requests", "runs", "vault_files", "consents",
                      "claims", "people"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names, FK-safe order
        for table in ("writes", "checkpoints"):  # LangGraph checkpoints, if the workflow has created them
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
        inserted = _seed_rows(conn)
    logger.info("demo data reset", extra={"schema_version": version, "seed_inserted": inserted})
    return {"schema_version": version, "seed_inserted": inserted, "seed_files_written": files}
