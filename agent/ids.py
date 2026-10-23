"""Identifier formats accepted in URLs. Anything else is rejected (422) before any handler runs.

Generated ids are `PREFIX-` + 12 lowercase hex characters (`repository.new_id`); seed rows use
readable ids such as `CON-SEED-01` and `VF-SEED-01`. Use the annotated types for path parameters:

    def view(run_id: RunId): ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

RUN_ID = r"^RUN-[0-9a-f]{12}$"
ACTION_ID = r"^ACT-[0-9a-f]{12}$"
EVIDENCE_ID = r"^EV-[0-9a-f]{12}$"
CONSENT_REQUEST_ID = r"^CRQ-[0-9a-f]{12}$"
CONSENT_ID = r"^CON-[A-Za-z0-9-]{1,40}$"
VAULT_FILE_ID = r"^VF-[A-Za-z0-9-]{1,40}$"
CLAIM_ID = r"^CLM-\d{4}-\d{4}$"
POLICY_NO = r"^POL-[A-Z0-9][A-Z0-9-]{2,40}$"

RunId = Annotated[str, Path(pattern=RUN_ID, description="run id, e.g. RUN-3f9c1a2b7d4e")]
ActionId = Annotated[str, Path(pattern=ACTION_ID, description="upload action id, e.g. ACT-3f9c1a2b7d4e")]
EvidenceId = Annotated[str, Path(pattern=EVIDENCE_ID, description="evidence id, e.g. EV-3f9c1a2b7d4e")]
ConsentRequestId = Annotated[str, Path(pattern=CONSENT_REQUEST_ID, description="consent request id")]
ConsentId = Annotated[str, Path(pattern=CONSENT_ID, description="consent id, e.g. CON-SEED-01")]
VaultFileId = Annotated[str, Path(pattern=VAULT_FILE_ID, description="vault file id, e.g. VF-SEED-01")]
