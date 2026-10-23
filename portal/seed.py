"""Synthetic seed data: one family floater policy, three members, three claims.

Every name, number and hospital here is invented for the hackathon demo.
`build_claims()` returns fresh objects each call, so a reset never shares state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from shared.synthetic_pdf import synthetic_pdf  # noqa: F401  (re-exported for callers)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Demo credentials (synthetic). Real portals would never ship these in code.
DEMO_USERS: dict[str, str] = {"arjun": "demo123"}


@dataclass(frozen=True)
class Member:
    """A person covered by the policy."""

    member_id: str
    name: str
    age: int
    relation: str


@dataclass(frozen=True)
class Policy:
    """The family floater policy."""

    policy_no: str
    insurer: str
    tpa: str
    holder: str
    sum_insured: int
    valid_from: str
    valid_to: str
    members: tuple[Member, ...]


@dataclass
class PreAuth:
    """Pre-authorisation for a cashless claim. Separate from the claim's own status."""

    reference: str
    status: str
    amount: int
    decided_at: datetime


@dataclass
class Requirement:
    """A document the TPA has asked for."""

    req_id: str
    description: str
    doc_type: str
    due_date: str
    state: str = "Pending"
    received_at: datetime | None = None


@dataclass
class UploadedDoc:
    """A document stored against a claim."""

    doc_id: str
    filename: str
    size: int
    sha256: str
    content_type: str
    uploaded_at: datetime
    stored_name: str
    requirement_id: str | None = None
    source: str = "portal upload"


@dataclass
class Claim:
    """A claim and everything the claim page shows."""

    claim_id: str
    policy_no: str
    patient_id: str
    route: str
    hospital: str
    admission_date: str
    discharge_date: str | None
    amount_claimed: int
    raw_status: str
    status_updated_at: datetime
    preauth: PreAuth | None
    requirements: list[Requirement] = field(default_factory=list)
    uploads: list[UploadedDoc] = field(default_factory=list)


POLICY = Policy(
    policy_no="POL-HDF-778812",
    insurer="Synthetic General Insurance Co. (demo)",
    tpa="DemoTPA Services (demo)",
    holder="Arjun",
    sum_insured=500_000,
    valid_from="2026-04-01",
    valid_to="2027-03-31",
    members=(
        Member("M-01", "Ramesh", 62, "Father"),
        Member("M-02", "Lakshmi", 58, "Mother"),
        Member("M-03", "Arjun", 31, "Self"),
    ),
)

MEMBERS_BY_ID: dict[str, Member] = {m.member_id: m for m in POLICY.members}

# Documents already on file at seed time: (claim_id, filename, requirement_id, lines of synthetic text).
SEED_DOCUMENTS: list[tuple[str, str, str | None, list[str]]] = [
    ("CLM-2026-0142", "preauth_request_form.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Pre-authorisation request", "Patient: Ramesh", "Claim: CLM-2026-0142"]),
    ("CLM-2026-0142", "final_hospital_bill.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Final hospital bill", "Patient: Ramesh", "Sunrise Multispeciality Hospital (demo)"]),
    ("CLM-2026-0150", "claim_form.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Reimbursement claim form", "Patient: Lakshmi", "Claim: CLM-2026-0150"]),
    ("CLM-2026-0150", "hospital_bill.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Hospital bill", "Patient: Lakshmi", "Lakeview Hospital (demo)"]),
    ("CLM-2026-0150", "discharge_summary.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Discharge summary", "Patient: Lakshmi", "Admitted 2026-10-02, discharged 2026-10-05"]),
    ("CLM-2026-0163", "preauth_request_form.pdf", None,
     ["SYNTHETIC DEMO DOCUMENT", "Pre-authorisation request", "Patient: Ramesh", "Claim: CLM-2026-0163"]),
]


def build_claims() -> dict[str, Claim]:
    """Fresh claim objects (without uploads; the store attaches seed documents)."""
    claims = [
        Claim(
            claim_id="CLM-2026-0142",
            policy_no=POLICY.policy_no,
            patient_id="M-01",
            route="Cashless",
            hospital="Sunrise Multispeciality Hospital (demo)",
            admission_date="2026-10-14",
            discharge_date="2026-10-18",
            amount_claimed=118_400,
            raw_status="Query Raised",
            status_updated_at=datetime(2026, 10, 21, 14, 5, tzinfo=IST),
            preauth=PreAuth("PA-2026-0142", "Approved", 120_000, datetime(2026, 10, 14, 11, 40, tzinfo=IST)),
            requirements=[
                Requirement("REQ-0142-01", "Pharmacy invoice for discharge medicines, showing patient name and date",
                            "pharmacy_invoice", "2026-11-05"),
                Requirement("REQ-0142-02", "Indoor case papers for the admission", "indoor_case_papers", "2026-11-05"),
            ],
        ),
        Claim(
            claim_id="CLM-2026-0150",
            policy_no=POLICY.policy_no,
            patient_id="M-02",
            route="Reimbursement",
            hospital="Lakeview Hospital (demo)",
            admission_date="2026-10-02",
            discharge_date="2026-10-05",
            amount_claimed=64_250,
            raw_status="Under Review",
            status_updated_at=datetime(2026, 10, 20, 10, 30, tzinfo=IST),
            preauth=None,
        ),
        Claim(
            claim_id="CLM-2026-0163",
            policy_no=POLICY.policy_no,
            patient_id="M-01",
            route="Cashless",
            hospital="Sunrise Multispeciality Hospital (demo)",
            admission_date="2026-10-20",
            discharge_date=None,
            amount_claimed=50_000,
            raw_status="Under Process - Pending Final Authorisation",
            status_updated_at=datetime(2026, 10, 22, 9, 15, tzinfo=IST),
            preauth=PreAuth("PA-2026-0163", "APPROVED", 50_000, datetime(2026, 10, 20, 16, 50, tzinfo=IST)),
        ),
    ]
    return {c.claim_id: c for c in claims}
