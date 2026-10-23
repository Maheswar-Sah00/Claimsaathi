"""Deterministic raw-status -> CanonicalStatus mapping (TC3).

`normalise_status` takes the claim's raw status text and nothing else: pre-authorisation
fields cannot influence it by construction. The rules:

1. Text is lower-cased, punctuation like '-', '_', '/' becomes spaces, whitespace collapses.
2. Phrases match on word boundaries ('unpaid' never matches 'paid').
3. A phrase inside a longer matched phrase is ignored ('approved' inside 'partially approved').
4. A positive phrase (approved, settled, paid...) preceded by a negation ('not approved',
   'not yet settled') is not trusted.
5. Exactly one canonical status left -> that status. None, or more than one -> UNCERTAIN.
   An unfamiliar label never becomes an approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent import repository as repo
from agent.db import Database
from agent.models import CanonicalStatus

# Ordered as specified. Order documents intent; correctness comes from the span rules above.
STATUS_RULES: tuple[tuple[CanonicalStatus, tuple[str, ...]], ...] = (
    (CanonicalStatus.QUERY_RAISED, ("query raised", "documents awaited", "deficiency")),
    (CanonicalStatus.UNDER_REVIEW, ("under review", "under process", "pending final")),
    (CanonicalStatus.PARTIALLY_APPROVED, ("partially approved",)),
    (CanonicalStatus.APPROVED, ("approved", "sanctioned")),
    (CanonicalStatus.SETTLED, ("settled", "paid", "payment released")),
    (CanonicalStatus.REJECTED, ("rejected", "repudiated", "denied")),
    (CanonicalStatus.CLOSED, ("closed",)),
)

_POSITIVE = {CanonicalStatus.PARTIALLY_APPROVED, CanonicalStatus.APPROVED, CanonicalStatus.SETTLED}
_NEGATIONS = {"not", "no", "never", "yet", "awaiting", "pending", "without"}


@dataclass(frozen=True)
class StatusResult:
    """The canonical status plus exactly why."""

    canonical: CanonicalStatus
    raw_status: str | None
    matched: tuple[str, ...]
    reason: str


def _clean(raw: str) -> str:
    text = re.sub(r"[-_/|:,.;()\[\]]+", " ", raw.lower())
    return " ".join(text.split())


def normalise_status(raw_status: str | None) -> StatusResult:
    """Map raw claim-status text to a canonical status. See the module docstring for the rules."""
    if raw_status is None or not raw_status.strip():
        return StatusResult(CanonicalStatus.UNCERTAIN, raw_status, (), "no claim status text was found")
    text = _clean(raw_status)

    hits: list[tuple[int, int, CanonicalStatus, str]] = []
    for canonical, phrases in STATUS_RULES:
        for phrase in phrases:
            for m in re.finditer(rf"\b{re.escape(phrase)}\b", text):
                hits.append((m.start(), m.end(), canonical, phrase))
    # Drop hits contained in a longer hit ("approved" inside "partially approved").
    hits = [h for h in hits if not any(o is not h and o[0] <= h[0] and h[1] <= o[1] and (o[1] - o[0]) > (h[1] - h[0])
                                       for o in hits)]

    negated = []
    for start, _end, canonical, phrase in hits:
        before = text[:start].split()[-4:]  # "yet to be approved", "not yet settled"
        if canonical in _POSITIVE and _NEGATIONS.intersection(before):
            negated.append(phrase)
    hits = [h for h in hits if h[3] not in negated]

    found = {h[2] for h in hits}
    matched = tuple(sorted({h[3] for h in hits}))
    if negated and not found:
        return StatusResult(CanonicalStatus.UNCERTAIN, raw_status, tuple(negated),
                            f"'{raw_status}' negates {', '.join(negated)}; not treated as a positive status")
    if not found:
        return StatusResult(CanonicalStatus.UNCERTAIN, raw_status, (),
                            f"'{raw_status}' is not a known status label")
    if len(found) > 1:
        names = ", ".join(sorted(c.value for c in found))
        return StatusResult(CanonicalStatus.UNCERTAIN, raw_status, matched,
                            f"'{raw_status}' mixes conflicting labels ({names})")
    (canonical,) = found
    return StatusResult(canonical, raw_status, matched,
                        f"'{raw_status}' matched {', '.join(repr(p) for p in matched)} -> {canonical.value}")


@dataclass(frozen=True)
class StatusDecision:
    """A recorded status decision with the raw text and portal stamp beside it."""

    canonical: CanonicalStatus
    raw_status: str | None
    status_stamp: str | None
    reason: str
    decision_id: str
    evidence_id: str


def record_status(db: Database, *, run_id: str, claim_id: str, evidence_id: str, extraction_id: str,
                  extraction_valid: bool, raw_status: str | None, status_stamp: str | None,
                  extraction_errors: list[str] | None = None) -> StatusDecision:
    """Decide and store the canonical status for one claim read.

    An invalid extraction always yields UNCERTAIN: a status is never taken from data that
    failed validation. The decision links to the portal evidence it was read from.
    """
    if extraction_valid:
        result = normalise_status(raw_status)
        canonical, reason = result.canonical, result.reason
    else:
        canonical = CanonicalStatus.UNCERTAIN
        reason = "the claim page could not be read reliably: " + "; ".join((extraction_errors or ["invalid"])[:3])
    with db.transaction() as conn:
        decision = repo.record_decision(
            conn, run_id=run_id, kind="status", result=canonical.value, reason=reason, evidence_id=evidence_id,
            subject_ref=claim_id,
            input_refs={"extraction_id": extraction_id, "raw_status": raw_status, "status_stamp": status_stamp},
        )
    return StatusDecision(canonical, raw_status, status_stamp, reason, decision.id, evidence_id)
