# ClaimSaathi pitch script with screen directions

Team Twinkle Stars · Ziro Agentic AI Hackathon 2026 · Insurance Claim Status Tracker

**Length:** about 6 minutes in full. A 3-minute cut is at the end.
**Format:** each scene has **SCREEN** (what is visible), **DO** (what you click) and **SAY** (what you speak).
Lines in *italics* are optional if you are short on time.

---

## Before you record (checklist)

1. Stop any running agent and portal.
2. Reset the agent data: `.\scripts\reset_demo.ps1`
3. Start the portal: `.\scripts\run_portal.ps1` (this also resets its data).
4. Start the agent: `.\scripts\run_agent.ps1`
5. Open `http://127.0.0.1:8000/health/ready` and confirm `"status":"ready"`.
6. Browser: one window, zoom 110%, bookmarks bar hidden, no other tabs. Sign out of ClaimSaathi.
7. Open these tabs in this order:
   - Tab 1: `http://127.0.0.1:8000/` (ClaimSaathi home)
   - Tab 2: `http://127.0.0.1:8001/admin` (portal demo controls, for TC4 only)
8. Optional: an opening slide with the title and team name.

> **Order matters.** TC1 uploads the invoice, and an upload cannot be repeated for the same file and request.
> So do TC4 last, after a reset (Scene 7), or show it from the Test runs page (Scene 7, option B).

---

## Scene 1 · The problem (0:00 – 0:35)

**SCREEN:** Title slide, or ClaimSaathi home page (Tab 1), scrolled to the top.

**SAY:**
> "When a parent is in hospital, a son or daughter usually ends up handling the insurance claim.
> They log in to the TPA portal, the insurer's claims website, and see lines like *'Query Raised'* or
> *'Pending Final Authorisation'*. What does that mean? Which documents are missing? Is this the right bill?
> Did my upload even go through?
>
> People make the same mistakes again and again. They read *'pre-authorisation approved'* as 'the claim is approved'.
> They upload their mother's bill to their father's claim. The portal times out, so they upload again and create
> duplicates. Or they chase the hospital's own records, which the hospital is supposed to send."

---

## Scene 2 · The solution in one line (0:35 – 1:05)

**SCREEN:** ClaimSaathi home page. Slowly scroll to the three cards: *Consent*, *Status*, *Approval*.

**SAY:**
> "ClaimSaathi is a family-side agent. You ask *'where is my father's claim?'*, and it logs in to the TPA portal
> for you, reads the real status, works out what is missing, finds the right file, and uploads it. But only after you approve.
> Then it checks the portal again to prove the upload landed.
>
> Three rules: the patient consents first, a person types every OTP, and a person approves every upload.
> The AI never makes those decisions."

*Optional:* "Everything you'll see is synthetic: a mock TPA portal we built, and an invented family."

---

## Scene 3 · Meet the family (1:05 – 1:25)

**SCREEN:** Click **How it works** in the top bar, then **Next →** once to the *Meet the family* step.

**SAY:**
> "Our demo family shares one floater policy. Ramesh, the father, was admitted from 14 to 18 October on a cashless
> claim. Lakshmi, the mother, has her own claim. Arjun, the son, is our user. Ramesh has given Arjun permission;
> Lakshmi hasn't yet."

---

## Scene 4 · TC1: the main flow (1:25 – 3:10)

### 4a · Sign in

**SCREEN:** Click **Sign in** (top right).
**DO:** Choose **Arjun** → submit → type the code shown in the *Demo SMS* panel → **Verify**.

**SAY:**
> "I'm Arjun. I sign in with an OTP sent to my phone, simulated here."

### 4b · Ask

**SCREEN:** Dashboard, *Check a claim* card.
**DO:** Keep policy `POL-HDF-778812`. Type claim `CLM-2026-0142`. Choose **Status, and send missing documents**.
Click **Check claim**.

**SAY:**
> "I ask about my father's claim, and I let it send missing documents too. Notice the note:
> each upload will wait for my approval."

### 4c · Consent and portal OTP

**SCREEN:** Claim page with the progress steps at the top and the card *The portal needs an OTP* showing the
portal's own OTP screen.
**DO:** Pause one second on the progress steps, then read the 6-digit code from the SMS panel in the screenshot,
type it, and click **Continue**.

**SAY:**
> "Before touching the portal, it checked that Ramesh has consented. It has. Then it opened the TPA portal in its
> own browser and logged in. The portal wants an OTP, and the agent stops. It is never allowed to read or
> fill an OTP. I type it."

### 4d · What it found, and the approval card

**SCREEN:** *Approve this upload?* card: file name, preview, patient, requirement, SHA-256, *Why this file*.
**DO:** Point the cursor at, in order: patient **Ramesh**, requirement **REQ-0142-01**, *Why this file*, the SHA-256.

**SAY:**
> "It read the claim page. The status is *Query Raised*: the insurer wants two documents, a pharmacy invoice and
> the indoor case papers.
>
> For the invoice, it checked every file in my documents: right patient, right document type, dated inside the
> hospital stay, readable, not already uploaded. My mother's invoice was rejected as the wrong patient. An August invoice
> was rejected as outside the stay. This one passed.
>
> Here's the card: whose claim, which request, which file, a preview and its fingerprint. Nothing has been sent yet."

**DO:** Click **Approve and upload**.

### 4e · Verified, and the answer

**SCREEN:** Claim page after the run: status card, *Next step*, *Documents the insurer asked for* table,
*In plain words*.
**DO:** Scroll slowly: status card → Next step → documents table → In plain words.

**SAY:**
> "It uploaded through the portal's own form, then re-read the portal's document list. It only says
> **verified** when the exact same file appears under the same request. Not just because the upload button
> said OK.
>
> Now the indoor case papers. On a cashless claim, the hospital sends those to the TPA. That's in IRDAI's 2024
> master circular. So ClaimSaathi doesn't upload my copy. It tells me the hospital should provide them and drafts
> the note for me to send. That's my one next step."

---

## Scene 5 · TC3: the "approved" trap (3:10 – 3:40)

**SCREEN:** Click **Dashboard**.
**DO:** Claim `CLM-2026-0163`, choose **Where is the claim?**, click **Check claim**. If it asks for the portal OTP again,
type it.
**DO:** Point at the status card, then the separate pre-authorisation card.

**SAY:**
> "My father's other claim. The portal shows *pre-authorisation approved, fifty thousand rupees*. Most people
> would celebrate. But pre-auth is only the estimate given at admission. The claim itself says *Pending Final
> Authorisation*.
>
> ClaimSaathi says **Under review**, and shows the pre-auth separately. Status comes from a fixed rule table, not
> an AI guess, and a guard blocks words like 'approved' or 'paid' unless the real status allows them."

---

## Scene 6 · TC2: someone else's claim (3:40 – 4:30)

**SCREEN:** **Dashboard.**
**DO:** Claim `CLM-2026-0150`, **Where is the claim?**, **Check claim**.

**SCREEN:** *Consent needed* card.

**SAY:**
> "Now my mother's claim. She hasn't given me permission, so it stops right here. It never opened the portal.
> No login, no OTP, nothing read. Instead, it sent her a consent request."

**DO:** Account menu (your initial, top right) → **Sign out**. Sign in as **Lakshmi** with the demo OTP.
Click **Consent** → under *Requests waiting for your decision*, click **Review**.

**SCREEN:** Consent request page: who is asking, which claim, which actions, duration (7 days by default).

**SAY:**
> "This is Lakshmi's view. She sees exactly who is asking, for which claim and what for. She can allow less,
> choose how long, decline, and revoke any time later."

**DO:** Click **Allow**.

*Optional:* Sign out, sign back in as Arjun, re-check `CLM-2026-0150`: it now runs and shows *Under Review*.

---

## Scene 7 · TC4: the upload times out (4:30 – 5:15)

Pick **one** option.

### Option A · Live (needs a reset first, about 1 extra minute)

**DO (off camera or sped up):** run `.\scripts\reset_demo.ps1`; on Tab 2 (`/admin`) click **Reset all seeded data**.
Then on `/admin` → *Upload fault mode* → choose **timeout_after_store**, tick **Apply to the next upload only**,
click **Set fault mode**.

**SCREEN:** Show the `/admin` page with *Current: timeout_after_store*.

**SAY:**
> "Now the nastiest case. I'm telling the mock portal to save the file, but never answer in time."

**DO:** Sign in as Arjun, repeat Scene 4b–4d quickly (claim `CLM-2026-0142`, upload, OTP, **Approve and upload**).
Wait about 10 seconds.

**SCREEN:** Claim page shows the upload went *unknown*, then *verified*.

**SAY:**
> "The upload timed out. A person would upload again and create a duplicate. ClaimSaathi marks it **unknown**: not
> failed, not done. It re-reads the document list first, finds the file is already there, and marks it
> **verified**. One file, no second upload. If the file had been missing, it would ask me before retrying.
> It never retries by itself."

### Option B · From the recorded test (no reset, about 30 seconds)

**DO:** Account menu → **Test runs** → scroll to **TC4** → open its run.
**SCREEN:** The TC4 run's **Agent trace** tab.

**SAY:** Same lines as Option A, pointing at the steps *UNKNOWN → VERIFIED* in the trace.

---

## Scene 8 · Proof: agent trace and test runs (5:15 – 5:40)

**SCREEN:** Open your TC1 run (Dashboard → *Your claim checks* → the `CLM-2026-0142` run) → **Agent trace** tab.
**DO:** Click one evidence screenshot thumbnail, then go back. Then account menu → **Test runs**.

**SAY:**
> "Every step is on record: what the agent decided, why, and a screenshot of every portal page it read, with a
> hash so it can't be quietly changed.
>
> And here are all four official test cases, plus six edge cases: wrong patient, consent revoked, unknown
> status, wrong OTP, portal down and hospital-held documents. All run end to end against the portal in a real browser.
> All passing."

---

## Scene 9 · How it's built, and close (5:40 – 6:10)

**SCREEN:** **How it works** → **Next →** to the last step, *You're done* (the recap list). Or an architecture slide.

**SAY:**
> "Under the hood: a LangGraph workflow drives a real browser through Playwright, with every step checkpointed in
> SQLite. If it crashes, the run resumes where it stopped. Consent, status, document matching and upload state are all
> deterministic rules. An AI model is optional and only helps read and phrase. It never decides.
>
> Next, we'd add a background watcher that checks every day and pings the family when the status changes.
>
> ClaimSaathi: the real status, the right document, uploaded once, and proof it landed. With the family in
> control at every step. Thank you."

---

## Who says what (two presenters)

| Part | Maheswar | Ravivarma |
|---|---|---|
| Scenes 1–3 (problem, solution, family) | Speaks | Drives the screen |
| Scenes 4–7 (live demo) | Drives the screen | Speaks |
| Scenes 8–9 (proof, architecture, close) | Speaks | Drives the screen |

---

## 3-minute cut

Keep: Scene 1 (first paragraph only) → Scene 2 → Scene 4 (all) → Scene 5 → Scene 6 (stop at the *Consent needed*
card, skip Lakshmi's view) → Scene 8 (Test runs page only) → Scene 9 (last paragraph only).

---

## If a judge asks…

| Question | Answer |
|---|---|
| Is ClaimSaathi the TPA? | No. It's the family's assistant. It uses the TPA's own portal with the family's login, just as they would. |
| Why not just do it manually? | You can. ClaimSaathi stops the common mistakes: misreading pre-auth, wrong-patient files, duplicate uploads, chasing hospital-held documents. And it keeps proof. |
| Where do the documents come from? | In the demo they're pre-loaded with their details filled in. A real version would let you upload or forward files and read the details automatically. |
| What does the AI do? | Optionally reads the page and phrases the reply, both checked against the page. Consent, status, matching and uploads are fixed rules. |
| Can Lakshmi do it herself? | Yes. Consent only adds a helper. She can check her own claim and revoke Arjun's access any time. |
| Does it work on a real insurer? | The portal adapter is separate from the safety rules. A real portal needs a new adapter, the code that knows that site's pages. The rules stay the same. Today it's a mock, by design. |
| Does it check automatically? | Not yet. A background watcher is the next phase. |
