# ⚖️ Senior Counsel — Indian Legal Research & Drafting Assistant

© 2026 **Dr Shantanu Samanta**. All rights reserved.

A Streamlit application that acts as a veteran Indian advocate: legal research and
opinions, filing-ready drafts, document analysis, judgment summaries and authority
checks — **with every case citation verified against a primary source before you
see it.**

---

## The one thing that makes this different

Most AI legal tools fabricate citations. They produce a case name, a reporter
volume and a page number that look perfectly correct and do not exist. Lawyers in
several jurisdictions have been sanctioned for filing them.

This app treats the model as a **drafter, never an authority**. Every judicial
citation it produces is independently re-retrieved from a real legal database
before display. If it cannot be found, it is **deleted from the output** and you
are told what was removed and why.

```
model output → extract citations → re-retrieve from Indian Kanoon / primary sources
             → verified?  YES → render with real title, court, date and source link
                          NO  → DELETE from text + log the suppression
             → check every quotation verbatim against the source judgment
```

Quotations get the same treatment: a quoted passage that does not appear in the
cited judgment is stripped, even when the case itself is genuine.

You can see this working in the **Verification** tab of every result, and in the
**Raw model output** tab, which shows the unsanitised text so you can judge for
yourself what was removed.

---

## Features

| Tab | What it does |
|---|---|
| 🔍 **Research & Opinion** | Retrieves real judgments first, then reasons. Structured opinion: question, short answer, facts, law, authorities, analysis, strategy, candid risks. |
| ✍️ **Drafting** | 39 document types — notices, plaints, written statements, writs, SLPs, bail, quashing, arbitration, IBC, contracts. Unknowns marked `[TO BE INSERTED: …]`, never invented. |
| 📑 **Document Analysis** | Upload notices, FIRs, contracts, orders, awards. Scanned documents read by the vision model. Extracts particulars, computes deadlines, flags adverse clauses and defects. |
| ⚖️ **Judgment Summary** | Practitioner's headnote: ratio vs obiter, quotable paragraphs with numbers, precedents considered, current status. |
| 🔗 **Authority Check** | "Is this still good law?" Reports `UNVERIFIED` rather than guessing. |
| 🕸️ **Citation Map** | Precedent graph built **only** from authorities verified in that session. |

Other things it gets right:

- **BNS/IPC transition.** Establishes the date of the offence, then applies the
  Bharatiya Nyaya Sanhita 2023 / BNSS / BSA for offences on or after 1 July 2024,
  and IPC / CrPC / Evidence Act before it — and says which and why.
- **Limitation, jurisdiction, maintainability** are addressed before the merits.
- **Bench strength and per incuriam** reasoning when weighing authority.
- Exports to **DOCX** (Times New Roman, 1.5 spacing, justified, court margins),
  **PDF** and **Markdown** — each with a *Verification Appendix* recording what was
  checked and what was suppressed.

---

## Quick start

```bash
git clone <your-repo> && cd legal_assistant
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit secrets.toml — see below

streamlit run app.py
```

### Set the access password (required)

The app refuses to start without one.

```bash
python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

```toml
APP_PASSWORD_SHA256 = "paste_the_hex_digest_here"
```

Only the digest is stored, so the password itself is never sitting in your secrets
file. For several users:

```toml
[APP_USERS]
shantanu  = "sha256_hex_digest"
associate = "sha256_hex_digest"
```

Protections: constant-time comparison, 5-attempt lockout with 5-minute cooldown,
8-hour session timeout. This gates a private tool — it is not an identity
provider. For client-confidential deployments, put real SSO in front of it.

---

## Configuration

All keys are backend secrets. They are read from `st.secrets` first, then
environment variables, so the identical code runs locally (`.env`) and on
Streamlit Cloud (Secrets UI) with no changes. **No key ever reaches the browser.**

| Secret | Required | Purpose |
|---|---|---|
| `APP_PASSWORD_SHA256` | ✅ | Dashboard access gate |
| `ANTHROPIC_API_KEY` | one of | Claude — **recommended** |
| `GEMINI_API_KEY` | one of | Gemini — fully supported |
| `INDIAN_KANOON_API_TOKEN` | strongly recommended | Citation verification |
| `INDIAN_KANOON_SESSION_BUDGET_INR` | optional | Spend ceiling, default ₹25 |
| `ECOURTS_API_KEY` | optional | Case status, certified copies |

### Claude vs Gemini

Both work; you can switch in the sidebar at runtime. One asymmetry matters:

**Anthropic's `web_search` accepts `allowed_domains`**, so the app can confine
research to Indian courts, Indian Kanoon and India Code at the API level.
**Gemini's grounding cannot be domain-filtered server-side** — the restriction is
expressed in the prompt and enforced afterwards by the verifier, which rejects any
citation not sitting on a primary domain. The verifier is the real guard either
way, but Anthropic gives you defence in depth. Use Claude for opinions and long
drafts; Gemini is a fine cheaper alternative.

---

## Knowledge bases

Pluggable adapters in `legal/knowledge_bases.py`, best authority first:

| Source | Coverage | Access | Cost |
|---|---|---|---|
| **Indian Kanoon API** | 3+ crore judgments, orders, Central Acts. SC, all HCs, district courts, tribunals | Token | Pay-per-call, see below |
| **eCourts India** | 27.5 crore case records, cause lists, **digitally signed certified copies** | API key | ₹200 free credit |
| **Digital SCR** | *Official* Supreme Court Reports | **Free, no key** | Free |
| **India Code** | *Official* Central & State bare acts | **Free, no key** | Free |
| **Local corpus** | Your own judgment PDFs, TF-IDF indexed | Offline | Free |

Digital SCR and India Code work with **no keys at all** — they are the free
baseline.

### Indian Kanoon pricing

Published tariff (INR per request):

| Request | Cost |
|---|---|
| Search | ₹0.50 |
| Original document (court copy) | ₹0.50 |
| Document | ₹0.20 |
| Document fragment | ₹0.05 |
| Document metainfo | ₹0.02 |

**Free credits:** ₹500 on signup, granted automatically. Non-commercial use gets
**₹10,000 every month — but this requires use-case verification by the site
administrator**, so it is discretionary, not automatic. Apply and see; a personal
research tool is a reasonable case, though a publicly deployed app may read as
commercial.

**What that buys.** A typical research query costs roughly **₹2–3** (one
pre-retrieval search, a few verification lookups, cheap fragment checks). So:

- ₹500 signup credit ≈ **165–250 queries**
- ₹10,000/month ≈ **3,000–5,000 queries/month**

For a hobbyist that is effectively free.

**Cost controls built in:**
- Every response is **cached** for the session — repeats cost nothing.
- Quote-checking uses `/docfragment` (₹0.05) rather than `/doc` (₹0.20).
- A **hard session budget** (default ₹25) stops calls deliberately. Indian Kanoon
  is *prepaid*: when the balance hits zero the API silently returns nothing, which
  would quietly degrade verification without telling you. The cap prevents that.
- Live spend and remaining budget shown in the sidebar.

### Does it work without an Indian Kanoon token?

**Yes — degraded, not broken.** This is covered by an automated test
(`test_works_without_indian_kanoon_token`).

Without the token, verification falls back to the LLM's own grounded web search,
restricted to primary domains, plus Digital SCR and India Code. A citation is still
only accepted if it appears on a court or official site. What you lose:

- Targeted `cite:` / `title:` database lookup (much higher hit rate)
- `/docfragment` verbatim quote checking
- Pre-retrieval grounding — the model no longer has real judgments in front of it
  before it writes, which is the single biggest quality lever

The sidebar warns you, and **every result carries a badge** showing whether Indian
Kanoon actually backed *that specific answer*:

| Badge | Meaning |
|---|---|
| ● **Verified** | Every citation confirmed against the database |
| ◐ **Partial** | Some citations confirmed by IK, others by web search |
| ○ **Not needed** | Configured, but this answer asserted no authority |
| ✕ **Unavailable** | No token, or session budget reached — weaker verification |

The Verification tab additionally breaks down provenance per source.

---

## Deploying to Streamlit Community Cloud (free)

**Check before you push:**

```bash
python verify_install.py
```

This catches the failure mode that breaks most deployments — missing folders or
empty files — before Streamlit does.

1. Push to GitHub **with git, not the web upload interface** (see below):

   ```bash
   cd legal_assistant
   git init
   git add -A                       # -A is what carries the subfolders
   git commit -m "Senior Counsel legal assistant"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Confirm `.streamlit/secrets.toml` is not committed** — it is in `.gitignore`,
   but check.
3. [share.streamlit.io](https://share.streamlit.io) → *New app* → pick the repo →
   main file `app.py`.
4. *Advanced settings* → **Secrets** → paste the contents of your
   `secrets.toml.example`, filled in.
5. Deploy.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'legal'` (or `llm`, or `ingest`)

The root files reached GitHub but a **subfolder did not**. Almost always caused by
uploading through GitHub's web interface, which does not reliably carry nested
folders, and silently drops zero-byte files.

**Diagnose:**

```bash
python verify_install.py
```

It names the exact missing file. The app also self-diagnoses: instead of a raw
traceback you now get a page listing what is missing and what the server can
actually see.

**Fix — push everything with git:**

```bash
git add -A
git commit -m "Add missing package folders"
git push
```

Then confirm on GitHub that `legal/`, `llm/` and `ingest/` each appear as folders
containing a **non-empty** `__init__.py`. Every `__init__.py` in this project
carries a docstring specifically so it can never be dropped as an empty file.

**Still broken?** On Streamlit Cloud use *Manage app → Reboot*, or clear the cache
— it sometimes serves a stale commit.

### The app says "No access password configured"

Working as intended: it refuses to run unprotected. Set `APP_PASSWORD_SHA256` —
see [Set the access password](#set-the-access-password-required).

### Citations keep getting suppressed

Check the **Raw model output** tab to see what was removed. Common causes:

- No `INDIAN_KANOON_API_TOKEN` — verification is falling back to web search.
- Session budget reached (sidebar shows this) — raise it or reset.
- The judgment is genuinely obscure and could not be retrieved. Suppression is
  deliberately conservative; it prefers a false negative to a fabricated citation.

### `ModuleNotFoundError: No module named 'anthropic'` / `docx` / `reportlab`

`requirements.txt` did not install. Confirm it is at the repo root and matches the
file in this project, then reboot the app.

A note on confidentiality: Community Cloud is public infrastructure. For real
client matters, deploy somewhere you control and take advice on your obligations
under the Advocates Act and the BCI Rules.

---

## Project layout

```
legal_assistant/
├── app.py                      Streamlit UI, 6 tabs
├── auth.py                     Password gate (hashed, throttled)
├── config.py                   Secrets, models, domain whitelist, attribution
├── engine.py                   Orchestration + per-run Indian Kanoon status
├── exporters.py                DOCX / PDF / Markdown + verification appendix
├── legal/
│   ├── citations.py            Citation extraction (structured + free-text sweep)
│   ├── knowledge_bases.py      Indian Kanoon, eCourts, DigiSCR, India Code, local
│   ├── verifier.py             ★ HARD-BLOCK verifier
│   └── prompts.py              Senior-advocate prompts + citation contract
├── llm/
│   ├── base.py                 Provider interface
│   ├── anthropic_client.py     web_search + web_fetch, pause_turn handling
│   └── gemini_client.py        google_search grounding
├── ingest/documents.py         PDF / DOCX / image, vision fallback for scans
└── tests/test_verifier.py      18 tests, all passing
```

Run the tests:

```bash
python tests/test_verifier.py          # or: python -m pytest tests/ -v
```

---

## Limitations — read these

- **It is not a lawyer.** Output is research assistance and drafts. Verify every
  authority against the certified report before filing. The Verification Appendix
  in each export exists to make that check easy, not to replace it.
- **Verification proves a case *exists*, not that it is *good law*.** Use the
  Authority Check tab, and note that it reports `UNVERIFIED` when it cannot
  establish status — treat that as a real answer, not a failure.
- **Suppression is imperfect in both directions.** A genuine but obscure judgment
  may be suppressed because it could not be retrieved. Check the *Raw model output*
  tab when something you expected has vanished.
- **Digital SCR and India Code adapters scrape public HTML**, since neither
  publishes an API. They are best-effort and may break if the sites change; the
  verifier falls through to other sources when they do.
- **Indian Kanoon is prepaid.** Watch the sidebar budget.
- Statutory law changes. The BNS/BNSS/BSA transition especially is still generating
  case law; verify current positions.

---

## Licence & attribution

© 2026 **Dr Shantanu Samanta**. All rights reserved.

Unauthorised reproduction, redistribution or commercial exploitation of this
application or its output is prohibited. Attribution appears on the login screen,
the dashboard header and footer, and in every exported DOCX, PDF and Markdown file
(including the PDF page footer and DOCX running footer).
