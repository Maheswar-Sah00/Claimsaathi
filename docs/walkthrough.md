# ClaimSaathi: a walkthrough

Team Twinkle Stars, Ziro Agentic AI Hackathon 2026. Problem: **Insurance Claim Status Tracker** (Healthcare).

> Everything here is synthetic: the people, policy, claims, hospital, documents and the TPA portal itself.
> No real insurer or patient data is involved.

---

## 1. The problem in one paragraph

When someone is in hospital, a family member usually ends up chasing the insurance claim. They log in to a
TPA or insurer portal, try to read status text like *"Under Process - Pending Final Authorisation"*, work out
which documents the insurer still wants, dig those out of WhatsApp and phone galleries, and upload them,
often more than once because the portal timed out. Mistakes are common: reading "pre-auth approved" as
"claim approved", uploading the wrong person's bill, or checking a relative's claim without their say.

**ClaimSaathi** does that chasing for the family, but never takes the decisions that belong to a person.

## 2. What it does, in plain words

A family member asks: *"Where is my father's claim?"* ClaimSaathi then:

1. **Checks consent** - is this person allowed to see this patient's claim? If not, it asks the patient
   and stops. It does not even open the portal.
2. **Signs in to the portal** - and stops at the OTP screen. A human types the OTP. The agent never reads
   or fills it.
3. **Reads the claim page** - status, last-updated time, pre-authorisation, documents requested and
   documents already uploaded. Every page it reads is saved as evidence (screenshot + page source + hash).
4. **Translates the status honestly** - using a fixed rule table, not an AI guess. "Pre-auth approved" is
   shown separately and is never reported as "claim approved". Unknown wording is reported as *uncertain*.
5. **Matches missing documents to the family's files** - checking the patient's name, document type, date
   (inside the hospital stay), readability and whether it is already uploaded. Documents the hospital is
   supposed to send on cashless claims are flagged as *the hospital's job*, with a drafted note.
6. **Asks before uploading** - shows an approval card: which file, which request, which patient, a preview
   and the file's fingerprint (SHA-256). Nothing is sent until the person presses *Approve*.
7. **Uploads once, then checks** - after uploading, it re-reads the portal's document list. It only says
   "done" when the same file is actually listed there. If the portal timed out, it checks first instead of
   uploading again.
8. **Replies in plain language** - the real status, what each requested document's state is, and exactly
   one next step, with links to the evidence.

## 3. The pieces

There are two separate web apps that talk to each other only through a browser, like a real user would.

| Piece | Port | What it is |
| --- | --- | --- |
| **Mock TPA portal** (`portal/`) | 8001 | A stand-in insurer portal: login + OTP, claim pages, document uploads, and a clearly labelled demo admin page to change statuses and inject faults (timeouts, HTTP 500). |
| **ClaimSaathi agent** (`agent/` + `web/`) | 8000 | The family-facing app: sign-in, dashboard, claim view, approval cards, consent screens, document vault, evidence viewer and test results. |

Under the hood the agent is a **LangGraph** workflow driving **Playwright** (headless Chromium) against the
portal, with everything recorded in **SQLite**. Gemini is optional: if a key is set it can help read the page
and rephrase the reply, but its output is checked against the page and it never decides anything.

## 4. The demo family

Policy `POL-HDF-778812` is a family floater covering three people:

| Person | Role in the demo |
| --- | --- |
| **Ramesh** (62) | Father, the patient. Has given Arjun consent for his claims. |
| **Lakshmi** (58) | Mother, also a patient. Has **not** given Arjun consent. |
| **Arjun** (31) | Son, the one asking. Signs in to ClaimSaathi. |

Three claims sit on the portal:

| Claim | Patient | Type | What the portal says | Why it is in the demo |
| --- | --- | --- | --- | --- |
| `CLM-2026-0142` | Ramesh | Cashless | Query Raised: needs pharmacy invoice + indoor case papers | Upload flow (TC1, TC4) |
| `CLM-2026-0150` | Lakshmi | Reimbursement | Under Review | Consent block (TC2) |
| `CLM-2026-0163` | Ramesh | Cashless | Under Process - Pending Final Authorisation, pre-auth APPROVED Rs 50,000 | The "approved" trap (TC3) |

Arjun's document vault holds six files, deliberately including traps: the right pharmacy invoice, his
mother's invoice (wrong patient), an invoice from August (outside the hospital stay), a discharge summary,
a blurry photo of an invoice, and indoor case papers.

## 5. Walking through a run (TC1)

This is the main story: Arjun asks about his father's claim and the missing invoice gets sent.

**Step 1 - Sign in.** Arjun opens `http://127.0.0.1:8000/auth/login`, picks his name, and types the demo
OTP shown in the simulated SMS panel on the page.

**Step 2 - Ask.** On the dashboard he keeps policy `POL-HDF-778812`, enters claim `CLM-2026-0142`, picks
*Status, and send missing documents*, and presses **Check claim**.

**Step 3 - Consent check.** Behind the scenes the agent confirms Ramesh has granted Arjun `read_status` and
`upload_docs` on this claim. The result is stored as a decision.

**Step 4 - Portal OTP (pause).** The agent opens the portal, logs in, and stops. The claim page shows the
portal's own OTP screen. Arjun types the 6-digit code from the portal's simulated SMS panel. The run then
continues by itself.

**Step 5 - Read and understand.** The agent reads the claim page. Raw status *"Query Raised"* becomes the
canonical status `QUERY_RAISED`. Two requests are pending:

- `REQ-0142-01` pharmacy invoice
- `REQ-0142-02` indoor case papers

**Step 6 - Match documents.** For each request it goes through the vault:

- Pharmacy invoice -> `pharmacy_invoice_ramesh.pdf` is **MATCHED** (right patient, dated inside 14-18 Oct,
  readable, not yet uploaded). Lakshmi's invoice and the August invoice are rejected with reasons.
- Indoor case papers -> **HOSPITAL_SHOULD_PROVIDE**. On cashless claims the hospital sends these to the TPA
  (IRDAI Master Circular, 29 May 2024, Ch. I para 17(c)), so the agent drafts a note to the hospital's TPA
  desk instead of uploading the family's copy.

**Step 7 - Approval card (pause).** Arjun sees a card: patient Ramesh, claim `CLM-2026-0142`, request
`REQ-0142-01`, the file name, a preview, its SHA-256, and every check that passed. He presses
**Approve and upload**. (If the card changed in between, the approval is refused.)

**Step 8 - Upload and verify.** The agent re-checks consent and the file one last time, uploads through the
portal's own form, then re-reads the uploaded-documents list. The same SHA-256 is listed under
`REQ-0142-01`, so the action becomes **VERIFIED**.

**Step 9 - The answer.** The claim page shows:

- Status card: `QUERY_RAISED`, the raw portal text, and the portal's last-updated time in IST.
- Documents table: invoice *received and verified*; case papers *hospital should provide*, with the note.
- One next step: send the drafted note to the hospital.
- An **Agent trace** tab listing every step with its decision, timing and evidence screenshots.

## 6. The other official test cases

**TC2 - Mother's claim, no consent.** Arjun asks about `CLM-2026-0150`. The consent gate returns
`NEEDS_CONSENT` and the run stops **before the portal is touched** (no login, no OTP). A consent request goes
to Lakshmi. She can sign in as herself, approve fewer permissions, choose 1/7/30 days, or deny, and can revoke
later from `/consent`.

**TC3 - "Approved" that isn't.** For `CLM-2026-0163` the portal shows pre-authorisation APPROVED Rs 50,000,
while the claim itself is *Pending Final Authorisation*. The agent reports `UNDER_REVIEW` and shows the
pre-auth in a separate card. The status rules only ever look at the claim status text, and a reply guard
blocks words like "approved", "settled" or "paid" unless the real status allows them.

**TC4 - Upload timeout.** On the portal's `/admin` page set fault `timeout_after_store`. The portal saves the
file but never answers in time. The agent marks the upload **UNKNOWN** (not failed, not done), re-reads the
document list, finds the file, and marks it **VERIFIED**: one file stored, no second upload. If the re-read
had shown the file missing, it would ask Arjun whether to retry; it never retries on its own.

## 7. Edge cases we also cover

| Case | What happens |
| --- | --- |
| E1 Wrong-patient invoice | Only Lakshmi's invoice is available: marked MISMATCH with reasons, nothing proposed |
| E2 Consent revoked | Ramesh revokes; Arjun's next check is blocked before the portal, new request sent |
| E3 Unknown status text | "Escalated to medical panel" -> `UNCERTAIN`; the reply says it can't confirm |
| E4 Wrong OTP | Refused, asked again (bounded tries), right code continues |
| E5 Portal down | `FETCH_FAILED`; the reply never pretends "no change" |
| E6 Hospital-held docs | Indoor case papers flagged for the hospital, never uploaded from the vault |

All ten cases (TC1-TC4, E1-E6) pass against the live mock portal in headless Chromium. Results, checks and
evidence links are in [test-evidence.md](test-evidence.md) and on the agent's `/test-runs` page.

## 8. Why it is safe (the design rules)

- **The AI never decides.** Consent, status, document matching and upload state are fixed rules. A model
  may only read fields (checked against the page) and rephrase the reply (checked by a guard).
- **A human types every OTP** and **approves every upload.** The code checks that the OTP element is never
  read by the agent.
- **Consent first, every time.** It is checked at the start and again right before any read or upload. The
  database itself refuses portal evidence in a run without an allow decision.
- **Proof before "done".** Only a fresh portal read showing the same file hash can mark an upload VERIFIED.
  There is no API that sets that state directly.
- **No duplicate uploads.** Each upload has an idempotency key (claim + request + file hash). Crashes,
  repeat clicks and timeouts all re-read instead of re-sending.
- **Everything is on record.** Screenshots, page HTML with SHA-256, every decision and every workflow step
  are stored and can be opened from the claim page.
- **Resumable.** Runs are checkpointed in SQLite; after a restart a stopped run shows *Continue* and picks up
  from the last completed step.

## 9. How the workflow flows

```text
intake -> consent gate -> portal login -> [OTP: human] -> read claim -> extract + normalise status
       -> match documents -> [approve: human] -> upload -> verify + reply
```

Any step can exit early with an honest answer: no consent, portal down, nothing pending, status-only
question, or no safe file to upload.

## 10. Running it yourself

```powershell
cd B:\Projects\Ziro_digital\claimsaathi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env

.\scripts\reset_demo.ps1      # optional: clean demo data
.\scripts\run_portal.ps1      # terminal 1 -> http://127.0.0.1:8001
.\scripts\run_agent.ps1       # terminal 2 -> http://127.0.0.1:8000
```

Then follow section 5. Logins:

| Who | Where | How |
| --- | --- | --- |
| Arjun, Ramesh, Lakshmi | agent `/auth/login` | demo OTP shown on the page |
| `arjun` / `demo123` | portal `/login` (the agent uses this) | OTP in the portal's simulated SMS panel |

Tests:

```powershell
pytest                 # unit + integration suite
pytest -m scenario     # TC1-TC4 and E1-E6 end to end; rewrites docs/test-evidence.md
```

## 11. What it does not do (yet)

- It runs locally against a mock portal; there is no real insurer integration and nothing is deployed.
- OTPs appear on simulated SMS panels; no SMS is sent.
- One portal browser session per agent process.
- No background status watcher or chat-style view yet.

For full technical detail (schema, API, state machine, configuration), see the [README](../README.md).
