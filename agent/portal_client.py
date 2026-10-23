"""Portal adapter: a safety-owning `PortalClient` over a replaceable browser `PortalDriver`.

Split of responsibilities
- `PortalDriver` (contract below) only drives a browser: log in, submit a human-typed OTP,
  fetch a claim page, submit an upload. `PlaywrightTpaDriver` implements it for the
  synthetic DemoTPA portal. An authorised replacement portal needs only a new driver.
- `PortalClient` owns every safety rule and is not meant to be replaced:
  * reads need a fresh ALLOW `read_status` consent decision in the same run;
  * uploads need an action that is EXECUTING (approved by a human, consent re-checked) and a
    file resolved from `vault_files` under VAULT_DIR whose bytes match the approved hash;
  * every read or upload attempt, successful or not, is saved as evidence (screenshot, HTML,
    SHA-256) under `EVIDENCE_DIR/<run_id>/` with its metadata in SQLite.

The OTP is never read from the page. The driver stops at the OTP screen and returns a
screenshot for a person to read; only `submit_otp(code)` with that person's input continues.
Nothing here bypasses CAPTCHA or access controls: they surface as typed, blocking errors.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from agent import repository as repo
from agent.db import Database, to_utc_iso
from agent.models import ActionState, ConsentAction, ConsentResult

logger = logging.getLogger("agent.portal")

_CLAIM_ID = re.compile(r"^CLM-\d{4}-\d{4}$")
_REQ_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_RUN_ID = re.compile(r"^RUN-[0-9a-f]{12}$")
_OTP = re.compile(r"^\d{6}$")
MAX_VISIBLE_TEXT = 60_000  # characters of page text kept with the evidence for extraction
_CAPTCHA_SELECTOR = (
    "iframe[src*='captcha' i], [id*='captcha' i], [class*='captcha' i], [name*='captcha' i]"
)


def probe_portal(base_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Return (reachable, detail) for the portal's /health endpoint. Never raises."""
    try:
        resp = httpx.get(f"{base_url}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"unreachable: {type(exc).__name__}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    return True, "ok"


# ---------------- data ----------------


class LoginState(str, Enum):
    """Where the portal session stands."""

    LOGGED_OUT = "logged_out"
    OTP_REQUIRED = "otp_required"
    AUTHENTICATED = "authenticated"


@dataclass(frozen=True)
class PageArtifacts:
    """What the browser saw: final URL, HTML, visible text and (if possible) a screenshot."""

    url: str
    html: str
    screenshot: bytes | None
    text: str = ""


@dataclass(frozen=True)
class LoginStatus:
    """Result of a login step. `screenshot` shows the OTP screen for a human to read."""

    state: LoginState
    message: str
    screenshot: bytes | None = None


@dataclass(frozen=True)
class Requirement:
    req_id: str
    description: str
    doc_type: str | None
    due_date: str | None
    state: str


@dataclass(frozen=True)
class UploadedDocument:
    doc_id: str
    filename: str
    size_bytes: int
    sha256: str
    requirement_id: str | None
    uploaded_at: str


@dataclass(frozen=True)
class PreAuth:
    reference: str
    status: str
    amount_inr: int
    decided_at: str


@dataclass(frozen=True)
class ClaimSnapshot:
    """Everything read from a claim page, as shown by the portal."""

    claim_id: str
    policy_no: str
    patient_name: str
    patient_member_id: str | None
    route: str
    hospital: str
    admission_date: str
    discharge_date: str | None
    amount_claimed_inr: int | None
    raw_status: str
    status_updated_at: str
    status_updated_at_utc: str
    preauth: PreAuth | None
    requirements: tuple[Requirement, ...]
    uploaded: tuple[UploadedDocument, ...]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaimPage:
    snapshot: ClaimSnapshot
    artifacts: PageArtifacts


@dataclass(frozen=True)
class UploadPage:
    """What the portal said after an upload submit.

    outcome: accepted (confirmation shows a hash) | rejected (portal error page) |
    timeout (no response in time; the file may or may not be stored) | unconfirmed.
    """

    outcome: str
    doc_id: str | None = None
    sha256: str | None = None
    error_code: str | None = None
    message: str | None = None
    artifacts: PageArtifacts | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class ReadResult:
    snapshot: ClaimSnapshot
    evidence_id: str


@dataclass(frozen=True)
class DocumentListResult:
    claim_id: str
    raw_status: str
    status_updated_at: str
    requirements: tuple[Requirement, ...]
    uploaded: tuple[UploadedDocument, ...]
    evidence_id: str
    snapshot: ClaimSnapshot | None = None  # the whole page read, for re-checking eligibility

    def has_file(self, sha256: str, requirement_id: str | None = None) -> bool:
        return any(d.sha256 == sha256 and (requirement_id is None or d.requirement_id == requirement_id)
                   for d in self.uploaded)


@dataclass(frozen=True)
class UploadResult:
    """`accepted` only when the portal's confirmation shows the exact hash we sent.
    Even then the executor must verify by re-reading the document list."""

    outcome: str
    action_id: str
    confirmed_sha256: str | None
    doc_id: str | None
    error_code: str | None
    message: str | None
    evidence_id: str
    http_status: int | None = None


# ---------------- errors ----------------


class PortalError(Exception):
    """Base class. `blocked` errors need a person (OTP, re-login, CAPTCHA) before continuing."""

    code = "portal_error"
    http_status = 502
    blocked = False

    def __init__(self, message: str, *, artifacts: PageArtifacts | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.artifacts = artifacts
        self.url = url or (artifacts.url if artifacts else None)
        self.evidence_id: str | None = None  # set once the failure is recorded as evidence


class PortalUnavailable(PortalError):
    code, http_status = "portal_unavailable", 503


class LoginFailed(PortalError):
    code, http_status = "login_failed", 401


class OtpRequired(PortalError):
    code, http_status, blocked = "otp_required", 409, True


class OtpRejected(PortalError):
    code, http_status = "otp_rejected", 401


class SessionExpired(PortalError):
    code, http_status, blocked = "session_expired", 409, True


class CaptchaDetected(PortalError):
    code, http_status, blocked = "captcha_detected", 409, True


class LayoutChanged(PortalError):
    code = "layout_changed"


class ClaimNotFound(PortalError):
    code, http_status = "claim_not_found", 404


class ClaimMismatch(PortalError):
    code = "claim_mismatch"


class RequirementNotOnPage(PortalError):
    code, http_status = "requirement_not_found", 404


class NotApproved(PortalError):
    """A safety precondition (consent or human approval) is missing. Nothing was sent."""

    code, http_status = "not_approved", 403


class UnsafeFile(PortalError):
    code, http_status = "unsafe_file", 400


# ---------------- driver contract ----------------


class PortalDriver(Protocol):
    """Browser mechanics for one portal. No safety logic, no database access."""

    @property
    def state(self) -> LoginState: ...

    def login(self, username: str, password: str) -> LoginStatus: ...

    def submit_otp(self, otp: str) -> LoginStatus: ...

    def fetch_claim(self, claim_id: str) -> ClaimPage: ...

    def submit_upload(self, claim_id: str, requirement_id: str, filename: str, data: bytes,
                      content_type: str, timeout_ms: int) -> UploadPage: ...

    def close(self) -> None: ...


# ---------------- Playwright driver for the DemoTPA portal ----------------

_EXTRACT_JS = """
() => {
  const q = s => document.querySelector(s);
  const t = s => { const e = q(s); return e ? e.textContent.trim() : null; };
  const a = (s, n) => { const e = q(s); return e ? e.getAttribute(n) : null; };
  const cell = (row, s) => { const e = row.querySelector(s); return e ? e.textContent.trim() : null; };
  return {
    claim_id: t('#claim-id'), policy_no: t('#policy-number'), patient_name: t('#patient-name'),
    patient_member_id: a('#patient-name', 'data-member-id'), route: t('#claim-route'),
    hospital: t('#hospital-name'), admission_date: t('#admission-date'), discharge_date: t('#discharge-date'),
    amount_claimed: a('#amount-claimed', 'data-amount'), raw_status: a('#claim-status', 'data-raw-status'),
    status_updated_at: a('#status-updated-at', 'datetime'),
    has_preauth: !!q('#preauth-status'), no_preauth: !!q('#preauth-none'),
    preauth: q('#preauth-status') ? {
      reference: t('#preauth-reference'), status: t('#preauth-status'),
      amount: a('#preauth-amount', 'data-amount'), decided_at: a('#preauth-decided-at', 'datetime')
    } : null,
    has_requested: !!q('#requested-docs'), no_requested: !!q('#requested-docs-none'),
    requirements: [...document.querySelectorAll('#requested-docs tr.requirement')].map(r => ({
      req_id: r.dataset.reqId || null, state: r.dataset.state || null, doc_type: r.dataset.docType || null,
      description: cell(r, '.req-description'), due_date: cell(r, '.req-due-date')
    })),
    has_uploaded: !!q('#uploaded-docs'),
    uploaded: [...document.querySelectorAll('#uploaded-docs tr.uploaded-doc')].map(r => {
      const size = r.querySelector('.doc-size'); const time = r.querySelector('.doc-uploaded-at time');
      return {
        doc_id: r.dataset.docId || null, sha256: r.dataset.sha256 || null,
        requirement_id: r.dataset.requirementId || null, filename: cell(r, '.doc-filename'),
        size: size ? size.dataset.bytes : null, uploaded_at: time ? time.getAttribute('datetime') : null
      };
    })
  };
}
"""


def _parse_snapshot(raw: dict[str, Any], url: str) -> ClaimSnapshot:
    """Turn the raw DOM read into a snapshot, refusing anything incomplete."""
    required = ("claim_id", "policy_no", "patient_name", "route", "hospital", "admission_date", "raw_status",
                "status_updated_at")
    missing = [k for k in required if not raw.get(k)]
    if missing or not (raw["has_preauth"] or raw["no_preauth"]) or not raw["has_uploaded"] \
            or not (raw["has_requested"] or raw["no_requested"]):
        raise LayoutChanged(f"claim page is missing expected fields: {missing or 'sections'}", url=url)
    try:
        preauth = None
        if raw["preauth"]:
            p = raw["preauth"]
            preauth = PreAuth(p["reference"], p["status"], int(p["amount"]), p["decided_at"])
        requirements = tuple(
            Requirement(r["req_id"], r["description"], r["doc_type"], r["due_date"], r["state"])
            for r in raw["requirements"]
        )
        uploaded = tuple(
            UploadedDocument(d["doc_id"], d["filename"], int(d["size"]), d["sha256"], d["requirement_id"],
                             d["uploaded_at"])
            for d in raw["uploaded"]
        )
        if any(not r.req_id or not r.state for r in requirements) or any(
            not d.sha256 or not d.filename for d in uploaded
        ):
            raise ValueError("incomplete row")
        stamp_utc = to_utc_iso(datetime.fromisoformat(raw["status_updated_at"]))
        amount = int(raw["amount_claimed"]) if raw.get("amount_claimed") else None
    except (KeyError, TypeError, ValueError) as exc:
        raise LayoutChanged(f"claim page values could not be parsed: {exc}", url=url) from exc
    discharge = raw.get("discharge_date")
    return ClaimSnapshot(
        claim_id=raw["claim_id"], policy_no=raw["policy_no"], patient_name=raw["patient_name"],
        patient_member_id=raw.get("patient_member_id"), route=raw["route"], hospital=raw["hospital"],
        admission_date=raw["admission_date"],
        discharge_date=None if not discharge or discharge.lower().startswith("not ") else discharge,
        amount_claimed_inr=amount, raw_status=raw["raw_status"], status_updated_at=raw["status_updated_at"],
        status_updated_at_utc=stamp_utc, preauth=preauth, requirements=requirements, uploaded=uploaded,
    )


class PlaywrightTpaDriver:
    """Drives the synthetic DemoTPA portal with Playwright (sync API, one thread).

    `browser="local"` launches Chromium; `browser="browserbase"` connects to a Browserbase
    cloud browser over CDP using the configured API key and project id.
    """

    def __init__(self, base_url: str, *, headless: bool = True, timeout_ms: int = 15000,
                 browser: str = "local", browserbase_api_key: str | None = None,
                 browserbase_project_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.browser_kind = browser
        self._bb_key, self._bb_project = browserbase_api_key, browserbase_project_id
        self._pw = self._browser = self._context = self._page = None
        self._state = LoginState.LOGGED_OUT

    # ---- lifecycle ----

    @property
    def state(self) -> LoginState:
        return self._state

    def _ensure_page(self):  # noqa: ANN202 - Playwright Page
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        if self.browser_kind == "browserbase":
            if not (self._bb_key and self._bb_project):
                raise PortalUnavailable("BROWSER=browserbase needs BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID")
            from browserbase import Browserbase

            session = Browserbase(api_key=self._bb_key).sessions.create(project_id=self._bb_project)
            self._browser = self._pw.chromium.connect_over_cdp(session.connect_url)
            self._context = self._browser.contexts[0]
        else:
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context(viewport={"width": 1200, "height": 900})
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self._page

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
        self._state = LoginState.LOGGED_OUT

    def expire_session(self) -> None:
        """Drop the portal cookies (used to test and recover from session expiry)."""
        if self._context is not None:
            self._context.clear_cookies()

    # ---- helpers ----

    def _artifacts(self) -> PageArtifacts:
        page = self._page
        try:
            shot = page.screenshot(full_page=True)
        except Exception:  # noqa: BLE001 - a screenshot is best effort
            shot = None
        try:
            text = page.inner_text("body")
        except Exception:  # noqa: BLE001 - text is best effort
            text = ""
        return PageArtifacts(url=page.url, html=page.content(), screenshot=shot, text=text)

    def _path(self) -> str:
        return urlparse(self._page.url).path

    def _goto(self, path: str):  # noqa: ANN202 - Playwright Response
        from playwright.sync_api import Error as PlaywrightError

        page = self._ensure_page()
        url = f"{self.base_url}{path}"
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise PortalUnavailable(f"portal did not respond: {exc.message.splitlines()[0]}", url=url) from exc
        if response is not None and response.status >= 500:
            raise PortalUnavailable(f"portal returned HTTP {response.status}", artifacts=self._artifacts())
        self._check_captcha()
        return response

    def _check_captcha(self) -> None:
        if self._page.locator(_CAPTCHA_SELECTOR).count():
            raise CaptchaDetected("the portal is showing a CAPTCHA; a person must handle it", artifacts=self._artifacts())

    def _check_session(self) -> None:
        path = self._path()
        if path.startswith("/login"):
            self._state = LoginState.LOGGED_OUT
            raise SessionExpired("the portal session has expired; sign in again", artifacts=self._artifacts())
        if path.startswith("/otp"):
            self._state = LoginState.OTP_REQUIRED
            raise OtpRequired("the portal is asking for an OTP", artifacts=self._artifacts())

    def _submit_and_wait(self, click: Callable[[], None], timeout_ms: int) -> int | None:
        """Click and wait for the resulting page; returns its HTTP status when known."""
        from playwright.sync_api import Error as PlaywrightError

        try:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms) as navigation:
                click()
        except PlaywrightError as exc:
            if "Timeout" in type(exc).__name__ or "timeout" in exc.message.lower():
                raise
            raise PortalUnavailable(f"portal did not respond: {exc.message.splitlines()[0]}",
                                    url=self._page.url) from exc
        response = navigation.value
        return response.status if response is not None else None

    # ---- contract ----

    def login(self, username: str, password: str) -> LoginStatus:
        self._goto("/login")
        page = self._page
        if not page.locator("#login-form").count():
            raise LayoutChanged("login form not found", artifacts=self._artifacts())
        page.fill("#username", username)
        page.fill("#password", password)
        self._submit_and_wait(lambda: page.click("#login-submit"), self.timeout_ms)
        self._check_captcha()
        if page.locator("#otp-form").count():
            self._state = LoginState.OTP_REQUIRED
            return LoginStatus(LoginState.OTP_REQUIRED,
                               "The portal sent an OTP. A person must read it and type it in.",
                               page.screenshot(full_page=True))
        if page.locator("#login-error").count():
            self._state = LoginState.LOGGED_OUT
            raise LoginFailed(page.inner_text("#login-error"), artifacts=self._artifacts())
        raise LayoutChanged("unexpected page after login", artifacts=self._artifacts())

    def submit_otp(self, otp: str) -> LoginStatus:
        if self._state is not LoginState.OTP_REQUIRED or self._page is None:
            raise SessionExpired("no OTP is pending; start the login again")
        page = self._page
        if not page.locator("#otp-form").count():
            self._state = LoginState.LOGGED_OUT
            raise SessionExpired("the OTP screen is no longer shown; start the login again",
                                 artifacts=self._artifacts())
        page.fill("#otp", otp)
        self._submit_and_wait(lambda: page.click("#otp-submit"), self.timeout_ms)
        path = self._path()
        if path.startswith("/claims"):
            self._state = LoginState.AUTHENTICATED
            return LoginStatus(LoginState.AUTHENTICATED, "Signed in to the portal.")
        if page.locator("#otp-error").count():
            raise OtpRejected(page.inner_text("#otp-error"), artifacts=self._artifacts())
        if path.startswith("/login"):
            self._state = LoginState.LOGGED_OUT
            raise SessionExpired("the OTP expired; start the login again", artifacts=self._artifacts())
        raise LayoutChanged("unexpected page after OTP", artifacts=self._artifacts())

    def fetch_claim(self, claim_id: str) -> ClaimPage:
        if not _CLAIM_ID.match(claim_id):
            raise ClaimNotFound(f"invalid claim id {claim_id!r}")
        if self._state is not LoginState.AUTHENTICATED:
            raise SessionExpired("not signed in to the portal")
        response = self._goto(f"/claims/{claim_id}")
        self._check_session()
        page = self._page
        if response is not None and response.status == 404:
            raise ClaimNotFound(f"the portal has no claim {claim_id}", artifacts=self._artifacts())
        try:
            page.wait_for_selector("#claim-id", timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001 - Playwright TimeoutError
            raise LayoutChanged("claim page did not show #claim-id", artifacts=self._artifacts()) from exc
        artifacts = self._artifacts()
        snapshot = _parse_snapshot(page.evaluate(_EXTRACT_JS), page.url)
        return ClaimPage(snapshot, artifacts)

    def submit_upload(self, claim_id: str, requirement_id: str, filename: str, data: bytes,
                      content_type: str, timeout_ms: int) -> UploadPage:
        from playwright.sync_api import Error as PlaywrightError

        if not _REQ_ID.match(requirement_id):
            raise RequirementNotOnPage(f"invalid requirement id {requirement_id!r}")
        self.fetch_claim(claim_id)
        page = self._page
        form = page.locator(f"form.upload-form[data-req-id='{requirement_id}']")
        if form.count() != 1:
            raise RequirementNotOnPage(f"{claim_id} has no upload form for {requirement_id}",
                                       artifacts=self._artifacts())
        form.locator("input[type=file]").set_input_files({"name": filename, "mimeType": content_type, "buffer": data})
        try:
            status = self._submit_and_wait(lambda: form.locator("button[type=submit]").click(), timeout_ms)
        except PlaywrightError:
            # No response in time. The portal may still have stored the file: never assume either way.
            return UploadPage("timeout", message=f"no response within {timeout_ms} ms")
        self._check_session()
        result = page.locator("#upload-result")
        if result.count():
            return UploadPage("accepted", doc_id=result.get_attribute("data-doc-id"),
                              sha256=result.get_attribute("data-sha256"), artifacts=self._artifacts(),
                              http_status=status)
        if page.locator("#error-code").count():
            return UploadPage("rejected", error_code=page.inner_text("#error-code").strip(),
                              message=page.inner_text("#error-message").strip(), artifacts=self._artifacts(),
                              http_status=status)
        return UploadPage("unconfirmed", message="no confirmation or error on the page", artifacts=self._artifacts(),
                          http_status=status)


# ---------------- the safety-owning client ----------------


class PortalClient:
    """The only way the agent talks to a portal. See the module docstring for the rules."""

    def __init__(self, db: Database, driver: PortalDriver, *, evidence_dir: Path, vault_dir: Path,
                 username: str, password: str, upload_timeout_ms: int = 10000) -> None:
        self.db = db
        self.driver = driver
        self.evidence_dir = Path(evidence_dir)
        self.vault_dir = Path(vault_dir)
        self._username, self._password = username, password
        self.upload_timeout_ms = upload_timeout_ms

    @property
    def state(self) -> LoginState:
        return self.driver.state

    def close(self) -> None:
        self.driver.close()

    # ---- login ----

    def login(self) -> LoginStatus:
        """Open the portal and sign in up to the OTP screen. Returns OTP_REQUIRED with a screenshot."""
        status = self.driver.login(self._username, self._password)
        logger.info("portal login", extra={"state": status.state.value})
        return status

    def submit_otp(self, otp: str) -> LoginStatus:
        """Continue the same session with the OTP a person typed."""
        if not _OTP.match(otp.strip()):
            raise OtpRejected("an OTP is 6 digits")
        status = self.driver.submit_otp(otp.strip())
        logger.info("portal otp submitted", extra={"state": status.state.value})
        return status

    # ---- reads ----

    def read_claim(self, claim_id: str, *, run_id: str) -> ReadResult:
        """Read a claim page. Requires a fresh ALLOW read_status decision in `run_id`."""
        page = self._fetch(claim_id, run_id, "portal_page")
        evidence_id = self._record(run_id, "portal_page", claim_id, page.artifacts,
                                   {"ok": True, "claim": page.snapshot.to_json()})
        return ReadResult(page.snapshot, evidence_id)

    def list_documents(self, claim_id: str, *, run_id: str) -> DocumentListResult:
        """Read the requested and uploaded documents plus the status stamp."""
        page = self._fetch(claim_id, run_id, "document_list")
        s = page.snapshot
        extracted = {"ok": True, "claim_id": s.claim_id, "raw_status": s.raw_status,
                     "status_updated_at": s.status_updated_at,
                     "requirements": [asdict(r) for r in s.requirements],
                     "uploaded": [asdict(d) for d in s.uploaded]}
        evidence_id = self._record(run_id, "document_list", claim_id, page.artifacts, extracted)
        return DocumentListResult(s.claim_id, s.raw_status, s.status_updated_at, s.requirements, s.uploaded,
                                  evidence_id, snapshot=s)

    def _fetch(self, claim_id: str, run_id: str, kind: str) -> ClaimPage:
        expected = self._require_consent(run_id, claim_id, ConsentAction.READ_STATUS)
        self._require_signed_in()
        try:
            page = self.driver.fetch_claim(claim_id)
            self._validate(page.snapshot, expected, page.artifacts)
        except PortalError as exc:
            self._record_failure(run_id, kind, claim_id, exc)
            raise
        return page

    def _validate(self, snapshot: ClaimSnapshot, expected: repo.ClaimRecord, artifacts: PageArtifacts) -> None:
        """Accept a page only if it is the claim, policy and patient the registry says it is."""
        with self.db.read() as conn:
            patient = repo.get_person(conn, expected.patient_id)
        problems = []
        if snapshot.claim_id != expected.claim_id:
            problems.append(f"page shows claim {snapshot.claim_id}")
        if snapshot.policy_no != expected.policy_no:
            problems.append(f"page shows policy {snapshot.policy_no}")
        if patient.portal_member_id and snapshot.patient_member_id != patient.portal_member_id:
            problems.append(f"page shows member {snapshot.patient_member_id}")
        if problems:
            raise ClaimMismatch(f"expected {expected.claim_id} on {expected.policy_no} for {patient.name}: "
                                + "; ".join(problems), artifacts=artifacts)

    # ---- upload ----

    def upload(self, *, action_id: str) -> UploadResult:
        """Upload the approved file for an EXECUTING action through the portal's own form.

        The caller is the action executor, after `consent.guarded_start_upload`. Nothing is
        sent unless the action, its approval, consent and the file bytes all check out.
        """
        action, run, vault_file, data = self._approved_upload(action_id)
        self._require_signed_in()
        try:
            page = self.driver.submit_upload(action.claim_id, action.requirement_id, vault_file.original_name, data,
                                             vault_file.content_type, self.upload_timeout_ms)
        except PortalError as exc:
            self._record_failure(run.id, "upload_response", action.claim_id, exc, action_id=action.id)
            raise
        outcome = page.outcome
        if outcome == "accepted" and page.sha256 != action.file_sha256:
            outcome = "unconfirmed"  # a confirmation for some other bytes is not our upload
        extracted = {"ok": outcome == "accepted", "outcome": outcome, "action_id": action.id,
                     "requirement_id": action.requirement_id, "sent_sha256": action.file_sha256,
                     "confirmed_sha256": page.sha256, "doc_id": page.doc_id, "error_code": page.error_code,
                     "message": page.message, "http_status": page.http_status}
        evidence_id = self._record(run.id, "upload_response", action.claim_id, page.artifacts, extracted,
                                   url=f"{getattr(self.driver, 'base_url', '')}/claims/{action.claim_id}/documents")
        logger.info("portal upload", extra={"action_id": action.id, "outcome": outcome,
                                            "http_status": page.http_status})
        return UploadResult(outcome, action.id, page.sha256 if outcome == "accepted" else None, page.doc_id,
                            page.error_code, page.message, evidence_id, page.http_status)

    def _approved_upload(self, action_id: str) -> tuple[repo.Action, repo.Run, repo.VaultFile, bytes]:
        with self.db.read() as conn:
            action = repo.get_action(conn, action_id)
            run = repo.get_run(conn, action.run_id)
        if action.state != ActionState.EXECUTING.value or not action.approved_by or not action.approved_at:
            raise NotApproved(f"action {action_id} is {action.state}; uploads run only after human approval "
                              "and consent re-check (EXECUTING)")
        self._require_consent(run.id, action.claim_id, ConsentAction.UPLOAD_DOCS)
        if not action.vault_file_id:
            raise UnsafeFile("the action has no registered vault file")
        with self.db.read() as conn:
            vault_file = repo.get_vault_file(conn, action.vault_file_id)
        if vault_file.owner_id != run.requester_id:
            raise UnsafeFile("the vault file does not belong to the requester")
        path = self._vault_path(vault_file.storage_path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not (digest == vault_file.sha256 == action.file_sha256):
            raise UnsafeFile("file bytes do not match the approved SHA-256; nothing was uploaded")
        return action, run, vault_file, data

    def _vault_path(self, storage_path: str) -> Path:
        base = self.vault_dir.resolve()
        target = (base / repo.safe_relative_path(storage_path)).resolve()
        if base not in target.parents or not target.is_file():
            raise UnsafeFile("vault file is missing or outside the vault folder")
        return target

    # ---- safety preconditions ----

    def _require_signed_in(self) -> None:
        if self.driver.state is LoginState.OTP_REQUIRED:
            raise OtpRequired("waiting for a person to enter the portal OTP")
        if self.driver.state is not LoginState.AUTHENTICATED:
            raise SessionExpired("not signed in to the portal; call login() and submit the OTP")

    def _require_consent(self, run_id: str, claim_id: str, action: ConsentAction) -> repo.ClaimRecord:
        """The latest consent decision for this run, claim and action must be ALLOW."""
        with self.db.read() as conn:
            run = repo.get_run(conn, run_id)
            if run.claim_id != claim_id:
                raise NotApproved(f"run {run_id} is for {run.claim_id}, not {claim_id}")
            row = conn.execute(
                "SELECT result FROM decisions WHERE run_id = ? AND kind = 'consent' AND subject_ref = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (run_id, f"{claim_id}:{action.value}"),
            ).fetchone()
            if row is None or row["result"] != ConsentResult.ALLOW.value:
                raise NotApproved(f"no current ALLOW consent for {action.value} on {claim_id} in run {run_id}; "
                                  "use consent.guarded_portal_read / guarded_start_upload")
            try:
                return repo.get_claim_record(conn, claim_id)
            except repo.NotFound as exc:
                raise NotApproved(f"{claim_id} is not in the trusted claim registry") from exc

    # ---- evidence ----

    def _record(self, run_id: str, kind: str, claim_id: str, artifacts: PageArtifacts | None,
                extracted: dict[str, Any], *, url: str | None = None) -> str:
        """Write screenshot + HTML under EVIDENCE_DIR/<run_id>/ and the metadata to SQLite."""
        if not _RUN_ID.match(run_id) or not _CLAIM_ID.match(claim_id):
            raise ValueError("refusing to build an evidence path from an unexpected id")
        html_sha = shot_rel = None
        if artifacts is not None:
            stem = f"{to_utc_iso(datetime.now().astimezone()).replace(':', '')}-{kind}-{claim_id}-{secrets.token_hex(3)}"
            run_dir = self.evidence_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            html_bytes = artifacts.html.encode("utf-8")
            html_sha = hashlib.sha256(html_bytes).hexdigest()
            (run_dir / f"{stem}.html").write_bytes(html_bytes)
            extracted = {**extracted, "html_file": f"{run_id}/{stem}.html"}
            if kind in ("portal_page", "document_list") and artifacts.text:
                extracted["visible_text"] = artifacts.text[:MAX_VISIBLE_TEXT]
                extracted["visible_text_truncated"] = len(artifacts.text) > MAX_VISIBLE_TEXT
            if artifacts.screenshot:
                (run_dir / f"{stem}.png").write_bytes(artifacts.screenshot)
                shot_rel = f"{run_id}/{stem}.png"
        with self.db.transaction() as conn:
            evidence = repo.add_evidence(
                conn, run_id=run_id, kind=kind, url=(artifacts.url if artifacts else None) or url or "about:blank",
                html_sha256=html_sha, screenshot_path=shot_rel, extracted=extracted, extraction_source="rule",
            )
        return evidence.id

    def _record_failure(self, run_id: str, kind: str, claim_id: str, exc: PortalError, *,
                        action_id: str | None = None) -> None:
        """Keep failed attempts visible: error type, message and whatever the browser showed."""
        extracted = {"ok": False, "error": exc.code, "message": exc.message, "blocked": exc.blocked}
        if action_id:
            extracted["action_id"] = action_id
        evidence_id = self._record(run_id, kind, claim_id, exc.artifacts, extracted, url=exc.url)
        exc.evidence_id = evidence_id
        logger.warning("portal step failed", extra={"run_id": run_id, "kind": kind, "error": exc.code,
                                                    "evidence_id": evidence_id})


def build_portal_client(settings: Any, db: Database) -> PortalClient:
    """Construct the configured client (Playwright DemoTPA driver; local or Browserbase browser)."""
    driver = PlaywrightTpaDriver(
        settings.portal_base_url, headless=settings.portal_headless, timeout_ms=settings.portal_timeout_ms,
        browser=settings.browser, browserbase_api_key=settings.browserbase_api_key,
        browserbase_project_id=settings.browserbase_project_id,
    )
    return PortalClient(db, driver, evidence_dir=settings.evidence_dir, vault_dir=settings.vault_dir,
                        username=settings.portal_username, password=settings.portal_password,
                        upload_timeout_ms=settings.portal_upload_timeout_ms)


class PortalSession:
    """Runs one PortalClient on one dedicated thread. Playwright's sync objects are bound to the
    thread that created them, while web requests arrive on many threads."""

    def __init__(self, factory: Callable[[], PortalClient]) -> None:
        self._factory = factory
        self._client: PortalClient | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portal")
        self.owner: str | None = None
        self.otp_screen: bytes | None = None

    def call(self, method: str, *args: Any, timeout: float = 120, **kwargs: Any) -> Any:
        """Run `PortalClient.<method>` on the portal thread and wait for it."""
        def run() -> Any:
            if self._client is None:
                self._client = self._factory()
            return getattr(self._client, method)(*args, **kwargs)
        return self._executor.submit(run).result(timeout=timeout)

    @property
    def state(self) -> LoginState:
        return self._client.state if self._client is not None else LoginState.LOGGED_OUT

    def close(self) -> None:
        if self._client is not None:
            try:
                self.call("close", timeout=30)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)


class SessionPortal:
    """The PortalClient calls the workflow and executor need, each run on the PortalSession's thread.

    `login` keeps the OTP screenshot on the session for the person who started it (served by
    portal_routes); the code itself always comes from that person."""

    def __init__(self, session: PortalSession, upload_timeout_ms: int, *, owner: str | None = None) -> None:
        self.session = session
        self.upload_timeout_ms = upload_timeout_ms
        self.owner = owner

    @property
    def state(self) -> LoginState:
        return self.session.state

    def login(self) -> LoginStatus:
        status = self.session.call("login")
        if self.owner is not None:
            self.session.owner = self.owner
        self.session.otp_screen = status.screenshot
        return status

    def submit_otp(self, otp: str) -> LoginStatus:
        status = self.session.call("submit_otp", otp)
        self.session.otp_screen = None
        return status

    def read_claim(self, claim_id: str, *, run_id: str) -> ReadResult:
        return self.session.call("read_claim", claim_id, run_id=run_id)

    def list_documents(self, claim_id: str, *, run_id: str) -> DocumentListResult:
        return self.session.call("list_documents", claim_id, run_id=run_id)

    def upload(self, *, action_id: str) -> UploadResult:
        return self.session.call("upload", action_id=action_id)
