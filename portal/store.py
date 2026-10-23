"""In-memory portal state plus on-disk upload storage.

State lives in one process (run uvicorn with a single worker). `reset()` restores
the seed exactly, including the seed documents on disk.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from portal.seed import IST, SEED_DOCUMENTS, Claim, UploadedDoc, build_claims, synthetic_pdf
from shared.errors import AppError

FAULT_MODES = ("none", "timeout_before_store", "timeout_after_store", "http_500")
OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5

# Allowed upload types, identified by magic bytes (the browser's content type is not trusted).
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
)
_ALLOWED_EXTENSIONS = {"pdf": "pdf", "png": "png", "jpg": "jpg", "jpeg": "jpg"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def now_ist() -> datetime:
    """Current time in IST (portal timestamps are shown in IST)."""
    return datetime.now(IST).replace(microsecond=0)


def safe_display_name(raw: str | None) -> str:
    """Reduce a browser-supplied filename to a harmless display name. Never used as a path."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", name).strip(" .")
    return name[:100] or "upload"


def sniff_type(data: bytes, filename: str) -> tuple[str, str]:
    """Return (content_type, extension) if extension and magic bytes agree, else raise AppError."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise AppError("unsupported_file_type", "Only PDF, PNG and JPG files are accepted.", 415)
    for magic, content_type, canonical_ext in _SIGNATURES:
        if data.startswith(magic):
            if canonical_ext != _ALLOWED_EXTENSIONS[ext]:
                break
            return content_type, canonical_ext
    raise AppError("file_content_mismatch", "File content does not match its extension.", 415)


@dataclass
class OtpChallenge:
    """A pending OTP. Kept server-side so the code never travels in the session cookie."""

    username: str
    code: str
    expires_at: datetime
    attempts: int = 0


@dataclass
class UploadLogEntry:
    """Audit line for every upload attempt, accepted or not."""

    at: datetime
    claim_id: str
    requirement_id: str
    filename: str
    sha256: str
    outcome: str
    fault: str


@dataclass
class PortalStore:
    """All mutable portal state."""

    upload_dir: Path
    claims: dict[str, Claim] = field(default_factory=dict)
    fault_mode: str = "none"
    fault_once: bool = True
    upload_log: list[UploadLogEntry] = field(default_factory=list)
    otp_challenges: dict[str, OtpChallenge] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------- lifecycle ----------

    def reset(self) -> None:
        """Restore seed claims and documents, clear uploads, faults and the upload log."""
        with self.lock:
            if self.upload_dir.exists():
                shutil.rmtree(self.upload_dir)
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            self.claims = build_claims()
            self.fault_mode, self.fault_once = "none", True
            self.upload_log = []
            for i, (claim_id, filename, req_id, lines) in enumerate(SEED_DOCUMENTS, start=1):
                data = synthetic_pdf(lines)
                claim = self.claims[claim_id]
                doc = self._write(claim_id, data, "pdf")
                claim.uploads.append(
                    UploadedDoc(
                        doc_id=f"DOC-SEED-{i:02d}",
                        filename=filename,
                        size=len(data),
                        sha256=doc[0],
                        content_type="application/pdf",
                        uploaded_at=claim.status_updated_at - timedelta(days=1),
                        stored_name=doc[1],
                        requirement_id=req_id,
                        source="seed",
                    )
                )

    # ---------- claims ----------

    def get_claim(self, claim_id: str) -> Claim:
        """Return a claim or raise a 404 AppError."""
        claim = self.claims.get(claim_id)
        if claim is None:
            raise AppError("claim_not_found", f"No claim {claim_id} on this policy.", 404)
        return claim

    def set_status(self, claim_id: str, raw_status: str) -> Claim:
        """Admin: change a claim's raw status text and stamp the time."""
        text = " ".join(raw_status.split())
        if not 2 <= len(text) <= 80:
            raise AppError("invalid_status", "Status text must be 2 to 80 characters.", 400)
        with self.lock:
            claim = self.get_claim(claim_id)
            claim.raw_status = text
            claim.status_updated_at = now_ist()
            return claim

    # ---------- faults ----------

    def set_fault(self, mode: str, once: bool) -> None:
        """Admin: select the upload fault mode."""
        if mode not in FAULT_MODES:
            raise AppError("invalid_fault_mode", f"Fault mode must be one of {', '.join(FAULT_MODES)}.", 400)
        with self.lock:
            self.fault_mode, self.fault_once = mode, once

    def take_fault(self) -> str:
        """Return the active fault for this upload; a one-shot fault switches itself off."""
        with self.lock:
            mode = self.fault_mode
            if mode != "none" and self.fault_once:
                self.fault_mode = "none"
            return mode

    # ---------- uploads ----------

    def validate_upload(self, claim_id: str, req_id: str, filename: str, data: bytes, max_bytes: int) -> tuple[str, str, str]:
        """Check requirement, size, type and duplicates. Returns (sha256, content_type, ext)."""
        claim = self.get_claim(claim_id)
        if not any(r.req_id == req_id for r in claim.requirements):
            raise AppError("unknown_requirement", f"{req_id} is not a document request on {claim_id}.", 400)
        if not data:
            raise AppError("empty_file", "The file is empty.", 400)
        if len(data) > max_bytes:
            raise AppError("file_too_large", f"Files must be at most {max_bytes // (1024 * 1024)} MB.", 413)
        content_type, ext = sniff_type(data, filename)
        sha = hashlib.sha256(data).hexdigest()
        if any(u.sha256 == sha and u.requirement_id == req_id for u in claim.uploads):
            raise AppError(
                "duplicate_upload",
                f"This exact file is already uploaded for {req_id} on {claim_id}.",
                409,
                {"sha256": sha},
            )
        return sha, content_type, ext

    def store_upload(self, claim_id: str, req_id: str, filename: str, data: bytes, sha: str,
                     content_type: str, ext: str) -> UploadedDoc:
        """Write the file under a server-generated name and attach it to the claim."""
        with self.lock:
            claim = self.get_claim(claim_id)
            if any(u.sha256 == sha and u.requirement_id == req_id for u in claim.uploads):
                raise AppError("duplicate_upload", "This exact file is already uploaded.", 409, {"sha256": sha})
            _, stored_name = self._write(claim_id, data, ext)
            stamp = now_ist()
            doc = UploadedDoc(
                doc_id=f"DOC-{secrets.token_hex(4).upper()}",
                filename=filename,
                size=len(data),
                sha256=sha,
                content_type=content_type,
                uploaded_at=stamp,
                stored_name=stored_name,
                requirement_id=req_id,
            )
            claim.uploads.append(doc)
            for req in claim.requirements:
                if req.req_id == req_id:
                    req.state, req.received_at = "Received", stamp
            return doc

    def log_upload(self, claim_id: str, req_id: str, filename: str, sha: str, outcome: str, fault: str) -> None:
        """Append an audit line."""
        with self.lock:
            self.upload_log.append(UploadLogEntry(now_ist(), claim_id, req_id, filename, sha, outcome, fault))

    def _write(self, claim_id: str, data: bytes, ext: str) -> tuple[str, str]:
        """Store bytes at <upload_dir>/<claim_id>/<sha>.<ext>. The path never comes from the client."""
        sha = hashlib.sha256(data).hexdigest()
        base = self.upload_dir.resolve()
        target = (base / claim_id / f"{sha}.{ext}").resolve()
        if base not in target.parents:
            raise AppError("bad_path", "Refusing to write outside the upload folder.", 400)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return sha, target.name

    # ---------- OTP ----------

    def new_otp(self, username: str) -> str:
        """Create an OTP challenge and return its id."""
        challenge_id = secrets.token_urlsafe(16)
        with self.lock:
            self.otp_challenges = {k: v for k, v in self.otp_challenges.items() if v.expires_at > now_ist()}
            self.otp_challenges[challenge_id] = OtpChallenge(
                username=username,
                code=f"{secrets.randbelow(1_000_000):06d}",
                expires_at=now_ist() + OTP_TTL,
            )
        return challenge_id

    def get_otp(self, challenge_id: str | None) -> OtpChallenge | None:
        """Return a live challenge, or None if missing or expired."""
        challenge = self.otp_challenges.get(challenge_id or "")
        if challenge is None or challenge.expires_at <= now_ist():
            return None
        return challenge

    def verify_otp(self, challenge_id: str | None, code: str) -> str:
        """Check a typed code. Returns the username on success; raises AppError otherwise."""
        with self.lock:
            challenge = self.get_otp(challenge_id)
            if challenge is None:
                raise AppError("otp_expired", "The OTP has expired. Please sign in again.", 401)
            challenge.attempts += 1
            if challenge.attempts > OTP_MAX_ATTEMPTS:
                self.otp_challenges.pop(challenge_id or "", None)
                raise AppError("otp_locked", "Too many wrong attempts. Please sign in again.", 401)
            if not secrets.compare_digest(challenge.code, code.strip()):
                left = OTP_MAX_ATTEMPTS - challenge.attempts
                raise AppError("otp_invalid", f"Incorrect OTP. {left} attempt(s) left.", 401)
            self.otp_challenges.pop(challenge_id or "", None)
            return challenge.username
