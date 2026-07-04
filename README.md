# FormNation

Claim-form auto-filling assistant for financial advisers. Forward a client's
claim form (or just *describe the claim in chat*), and FormNation extracts the
details, fills the form, checks its own work, and returns a reviewed PDF —
via a Telegram bot or a web UI.

Design and roadmap: [PLAN.md](PLAN.md) ·
Fragility/cost/compliance analysis: [docs/production-notes.md](docs/production-notes.md) ·
Data flows & privacy: [docs/data-handling.md](docs/data-handling.md)

## How it works

Every form is routed to the cheapest tier that works:

| Tier | Input | Field detection |
|---|---|---|
| 1 | Fillable PDF (AcroForm) | read directly from the PDF — exact |
| 2 | Flattened PDF | OpenCV cell + checkbox detection, text layer for labels |
| 3 | Scan / photo | + RapidOCR and automatic deskew |

The LLM (Gemini 2.5 Flash) never produces geometry — it only answers
semantic multiple-choice ("which numbered box is the NRIC?"), a design
validated by benchmark (`scripts/vision_experiment.py`: raw LLM bounding
boxes 3/9 correct, grid prompting 1/9, set-of-marks ≈ all).

Safety layers, in order: PII redaction before every LLM call (NRIC/email/
phone/account → placeholders, restored locally) → deterministic validators
(NRIC checksum, date/email/phone formats) → **read-back verification** (the
filled page is re-read by the vision model and compared with intent) →
mandatory human review. Signature fields are never auto-filled.

## Quick start

```bash
git clone https://github.com/luksz/form-nation.git && cd form-nation
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # add GEMINI_API_KEY (+ TELEGRAM_BOT_TOKEN for the bot)
./run.sh                    # starts web UI + bot together
```

- **Web UI:** http://127.0.0.1:8477 — upload, review with overlays and
  confidence badges, click-to-place fallback, download.
- **Dashboard:** http://127.0.0.1:8477/dashboard — claims history, saved
  clients, registered claim types.
- **Telegram bot:** create one with @BotFather, put the token in `.env`.
  Then: `/newtype Personal Accident` + send the blank form once. Daily use:
  `/newclaim` → tap the type → message the claim details in plain language →
  the bot tracks what's captured and what's missing → 📝 Fill → review the
  preview + read-back check → ✅ Approve. Clients are saved automatically on
  approval; delivered PDFs auto-delete from the chat after
  `DELETE_DELIVERED_MINUTES`.

Sample inputs in the repo: the GEG personal accident form (Tier 1),
`samples/GEG-flattened.pdf` (Tier 2), `samples/GEG-scan.pdf` (Tier 3).

## Data handling (short version)

Everything runs on your machine; the only external parties are Telegram
(chat transport — not E2E encrypted) and Google Gemini (mapping/extraction —
after local PII redaction). Uploads and filled PDFs auto-delete after
`RETENTION_HOURS`; client records are Fernet-encrypted at rest and deletable
via `/delclient` or the web UI; the audit log stores decisions, never values.
**Use a paid-tier Gemini key before processing real client data** (free-tier
inputs may be used for training). Full detail: [docs/data-handling.md](docs/data-handling.md).

## Repo map

```
app/main.py         FastAPI backend: tiers, mapping, fill, verify, clients
app/vision.py       OpenCV cell + checkbox detection, set-of-marks markers
app/ocr.py          RapidOCR words + deskew (Tier 3)
app/llm.py          Gemini adapter: mapping, intake extraction, read-back
app/redact.py       PII placeholder redaction for LLM calls
app/validate.py     NRIC checksum & format validators
app/db.py           SQLite: clients (encrypted), claim types, history, audit
app/telegram_bot.py Conversational intake bot
static/             Web UI + dashboard
scripts/            Benchmarks and sample generators
```

FormNation is an independent prototype. It is not affiliated with the
Singapore Government, form.gov.sg, or any insurer. AI-suggested values must
always be reviewed by a human before use.
