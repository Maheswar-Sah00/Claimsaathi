"""Family-facing reply: a deterministic template from trusted facts, an optional model
rephrase or translation, and a guard that the rephrase must pass.

The guard (deterministic) blocks any text that
- claims approval, sanction, settlement or payment the canonical claim status does not
  support (negated mentions such as "has not been approved" are fine, and a pre-authorisation
  sentence may say APPROVED only if it also says it is not the final decision);
- introduces a number that is not in the trusted facts;
- drops the claim id.
A blocked rephrase is replaced by the template. Invalid extraction or an UNCERTAIN status
always produces the uncertainty template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agent import repository as repo
from agent.db import Database
from agent.extract import ExtractionOutcome
from agent.llm import LlmClient
from agent.models import CanonicalStatus
from agent.status import StatusDecision

IST = timezone(timedelta(hours=5, minutes=30))

STATUS_SENTENCES: dict[CanonicalStatus, str] = {
    CanonicalStatus.QUERY_RAISED: "the insurer has asked for more documents before it can continue",
    CanonicalStatus.UNDER_REVIEW: "it is still being reviewed and no final decision has been made yet",
    CanonicalStatus.PARTIALLY_APPROVED: "it has been partially approved, so only part of the amount was accepted",
    CanonicalStatus.APPROVED: "it has been approved; payment has not been confirmed yet",
    CanonicalStatus.SETTLED: "it has been settled and paid",
    CanonicalStatus.REJECTED: ("it has been rejected; you can ask the insurer for the reason and the policy clause, "
                               "and escalate to its grievance cell or the Insurance Ombudsman"),
    CanonicalStatus.CLOSED: "it has been closed",
}

# Words that assert a positive outcome, by language. Lower-case; Devanagari/Telugu as-is.
_POSITIVE_WORDS = {
    "approved": "approve", "approval": "approve", "sanctioned": "approve", "sanction": "approve",
    "settled": "settle", "settlement": "settle", "paid": "settle", "payment released": "settle",
    "स्वीकृत": "approve", "मंजूर": "approve", "मंज़ूर": "approve", "अनुमोदित": "approve", "भुगतान": "settle",
    "ఆమోదించ": "approve", "మంజూరు": "approve", "చెల్లించ": "settle",
}
_PERMITTED = {
    CanonicalStatus.APPROVED: {"approve"},
    CanonicalStatus.SETTLED: {"approve", "settle"},
}
_NEGATORS = {"not", "no", "never", "yet", "awaiting", "pending", "without"}
_PREAUTH = re.compile(r"pre[\s-]?auth", re.I)
_CLAIM_POSITIVE = re.compile(r"\bclaim\b[^.;]{0,60}\b(approved|sanctioned|settled|paid)\b", re.I)


@dataclass(frozen=True)
class ReplyFacts:
    """Only these facts may reach the family. Everything comes from validated extraction."""

    claim_id: str
    patient_name: str | None
    canonical: CanonicalStatus
    raw_status: str | None
    status_stamp: str | None
    extraction_valid: bool
    preauth_status: str | None = None
    preauth_amount: int | None = None
    missing_items: list[str] = field(default_factory=list)
    next_step: str | None = None
    uncertainty_reason: str | None = None


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    violations: list[str]


@dataclass(frozen=True)
class Reply:
    text: str
    template: str
    used_model: bool
    guard: GuardResult
    decision_id: str | None


def _stamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(IST).strftime("%d-%b-%Y %H:%M IST")
    except ValueError:
        return None


def _rupees(amount: int) -> str:
    digits = str(amount)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "Rs " + (",".join(groups + [tail]) if groups else tail)


def render_template(facts: ReplyFacts) -> str:
    """The trusted reply. Short sentences a worried relative can act on."""
    stamp = _stamp(facts.status_stamp)
    shown = f'The portal shows "{facts.raw_status}"' + (f", updated {stamp}." if stamp else ".") if facts.raw_status else ""
    if not facts.extraction_valid or facts.canonical is CanonicalStatus.UNCERTAIN:
        why = facts.uncertainty_reason or "the status on the portal is not one I can interpret safely"
        lines = [f"I could not confirm the status of claim {facts.claim_id}: {why.rstrip('.')}.",
                 shown,
                 "I have not assumed anything about approval or payment.",
                 "Next step: check this claim with the TPA helpline, or ask me to read the portal again later."]
        return " ".join(line for line in lines if line)
    who = f" for {facts.patient_name}" if facts.patient_name else ""
    lines = [f"Claim {facts.claim_id}{who}: {STATUS_SENTENCES[facts.canonical]}.", shown]
    if facts.preauth_status:
        amount = f" for {_rupees(facts.preauth_amount)}" if facts.preauth_amount is not None else ""
        lines.append(f"Pre-authorisation was marked {facts.preauth_status}{amount}; that is the hospital's initial "
                     "cashless go-ahead, not the final decision.")
    if facts.missing_items:
        lines.append("Still needed: " + "; ".join(facts.missing_items) + ".")
    elif facts.canonical is CanonicalStatus.QUERY_RAISED:
        lines.append("No open document requests were listed on the page.")
    if facts.next_step:
        lines.append(f"Next step: {facts.next_step.rstrip('.')}.")
    return " ".join(line for line in lines if line)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?।])\s+", text) if s.strip()]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", text)}


def guard(text: str, facts: ReplyFacts, *, template: str | None = None) -> GuardResult:
    """Check a candidate reply against the trusted facts. Deterministic; no model involved."""
    violations: list[str] = []
    permitted = _PERMITTED.get(facts.canonical, set()) if facts.extraction_valid else set()
    for sentence in _sentences(text):
        lower = sentence.lower()
        for word, family in _POSITIVE_WORDS.items():
            for m in re.finditer(re.escape(word) if not word.isascii() else rf"\b{re.escape(word)}\b", lower):
                before = lower[: m.start()].split()[-4:]
                if _NEGATORS.intersection(before):
                    continue  # "has not been approved", "not yet settled"
                if facts.canonical is CanonicalStatus.PARTIALLY_APPROVED and word == "approved" \
                        and before and before[-1] == "partially":
                    continue
                if family in permitted:
                    continue
                if facts.preauth_status and _PREAUTH.search(sentence) and "not the final" in lower \
                        and not _CLAIM_POSITIVE.search(sentence):
                    continue  # the pre-authorisation trap explained honestly
                violations.append(f"says '{word}' but the claim status is {facts.canonical.value}: {sentence!r}")
    if facts.claim_id not in text:
        violations.append("does not mention the claim id")
    allowed_numbers = _numbers(template if template is not None else render_template(facts))
    extra = sorted(_numbers(text) - allowed_numbers)
    if extra:
        violations.append(f"introduces numbers not in the facts: {', '.join(extra)}")
    return GuardResult(not violations, violations)


REPHRASE_PROMPT = """Rewrite the message below for a family member in {language}.
Keep every fact, name, id, number and date exactly as given. Do not add facts, advice or reassurance.
Do not say the claim is approved, sanctioned, settled or paid unless the message already says so.
Return only the rewritten message.

MESSAGE:
<<<
{text}
>>>"""


def compose_reply(db: Database, facts: ReplyFacts, *, run_id: str, evidence_id: str | None,
                  llm: LlmClient | None = None, language: str = "English") -> Reply:
    """Build the reply, let the model rephrase only if configured, guard it, and record the guard decision."""
    template = render_template(facts)
    template_check = guard(template, facts, template=template)
    if not template_check.passed:  # a bug in the template must never reach a family
        raise RuntimeError(f"reply template failed its own guard: {template_check.violations}")
    text, used_model, result = template, False, template_check
    if llm is not None:
        try:
            candidate = llm.generate_text(REPHRASE_PROMPT.format(language=language, text=template)).strip()
        except Exception as exc:  # noqa: BLE001 - fall back to the template
            candidate, result = "", GuardResult(False, [f"rephrase failed: {type(exc).__name__}"])
        if candidate:
            result = guard(candidate, facts, template=template)
            if result.passed:
                text, used_model = candidate, True
    reason = "reply passed the guard" if result.passed else "rephrase blocked, template used: " + "; ".join(
        result.violations)[:500]
    with db.transaction() as conn:
        decision = repo.record_decision(
            conn, run_id=run_id, kind="reply_guard", result="PASSED" if result.passed else "BLOCKED",
            reason=reason, subject_ref=facts.claim_id,
            evidence_id=evidence_id, evidence_state=None if evidence_id else "missing",
            input_refs={"canonical": facts.canonical.value, "used_model": used_model, "language": language},
        )
    return Reply(text, template, used_model, result, decision.id)


def facts_from(outcome: ExtractionOutcome, status: StatusDecision, *, missing_items: list[str] | None = None,
               next_step: str | None = None) -> ReplyFacts:
    """Assemble reply facts from an ExtractionOutcome and a StatusDecision."""
    data = outcome.data
    return ReplyFacts(
        claim_id=outcome.claim_id,
        patient_name=data.patient_name if data else None,
        canonical=status.canonical,
        raw_status=status.raw_status,
        status_stamp=status.status_stamp,
        extraction_valid=outcome.valid,
        preauth_status=data.preauth_status if data else None,
        preauth_amount=data.preauth_amount if data else None,
        missing_items=missing_items or [],
        next_step=next_step,
        uncertainty_reason=None if outcome.valid else "the claim page could not be read reliably",
    )
