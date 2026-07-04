# Production readiness — fragility, accuracy, cost, integration, data, legal

Companion to [PLAN.md](../PLAN.md). The pipeline works on the GEG form family;
this document is about what it takes to trust it on *every* form.

## 1. Fragility, fallbacks, self-checks

### Where it breaks today (ranked by likelihood)

1. **CV cell detection assumes ruled tables.** `_detect_cells()` finds boxes made
   of printed lines. Forms using underscores (`Name: ______`), comb fields
   (|_|_|_|), dotted lines, or free space after a label produce no candidates.
   The GEG form is table-styled, so this is untested territory.
2. **Tuned constants.** Kernel sizes, min/max cell dimensions, the 200pt label
   zone — all tuned on one form at one render scale. Different layouts will
   need different values or an adaptive pass.
3. **Hybrid forms.** One widget anywhere → the whole doc is Tier 1, and
   non-widget pages get no detection. Tier should be per-page, not per-doc.
4. **Real photos vs. synthetic scans.** Our scan sample has uniform skew and
   noise. Real phone photos add perspective distortion, shadows, curvature,
   crop — deskew handles none of these yet.
5. **LLM variance.** Mapping quality differs run to run (saw it: Name of
   Insured filled in one run, skipped in another). Retry handles transport
   errors; nothing handles "confidently different answer".
6. **XFA and encrypted PDFs** — detected as neither widgets nor sane text;
   currently undefined behaviour.

### Fallback ladder (design principle: degrade, never fail)

```
AcroForm read  →  CV set-of-marks  →  LLM raw bbox (flagged low-trust)
      →  MANUAL: click-to-place in the web UI
```

The last rung is what makes "works on every single form" honest: a UI mode
where the human clicks a spot on the page and types — zero intelligence
required, works on literally anything renderable. **Not built yet; highest
robustness ROI of anything on this page.** Everything above it is then an
accelerator, not a dependency.

### Self-check mechanisms (in order of value/cost)

1. **Deterministic validators (cheap, catch the worst errors):** NRIC checksum,
   date parses, phone length, email regex, amount is numeric. A value failing
   its expected type in a box strongly signals a mis-mapping. No LLM needed.
2. **Read-back verification (closed loop):** after filling, render the page and
   ask the vision model "what value is written in the box next to <label>?" —
   compare with intent. Catches geometry *and* mapping errors in one pass.
   Roughly doubles LLM cost (see §3); still cents.
3. **Coverage report:** profile keys that mapped nowhere + fields that stayed
   empty = explicit "needs manual attention" list shown before download.
   Silent omission is the current failure mode for duplicates.
4. **Consensus mapping (for high-stakes use):** run mapping twice (different
   prompt phrasing); auto-accept only agreements, flag disagreements for the
   human. ~2× mapping cost.
5. **Golden regression suite:** forms + expected mappings checked in, run on
   every change. `scripts/vision_experiment.py` is the seed of this.

## 2. Accuracy — two separate problems, measure them separately

- **Geometry** (is the box in the right place): Tier 1 is exact by
  construction. Tier 2/3 geometry comes from CV, benchmarked at 100% on
  table-style forms vs 3/9 (LLM bbox) and 1/9 (grid). Geometry accuracy is a
  *per-layout-style* property — needs re-measuring per new form family.
- **Semantics** (is it the right value for that field): the LLM's job.
  Observed: 12/12 correct with conservative misses (duplicates skipped).

Metrics that matter (per form, not per field): **wrong-fill rate** (a value in
the wrong box — the dangerous error) and **form-level zero-error rate** (a form
is only useful if *everything* filled is right). Tune for precision over
recall: an empty box costs the user 10 seconds; a wrong NRIC on an insurance
claim costs a rejection or worse. The confidence threshold + review UI is the
mechanism; validators (§1.1) are the backstop.

## 3. Cost per form (Gemini 2.5 Flash, verified 2026-07: $0.30/M input, $2.50/M output)

| Scenario | LLM calls | Est. tokens | Est. cost |
|---|---|---|---|
| Tier 1 (text-only mapping) | 1 | ~4k in / 400 out | **~$0.002** |
| Tier 2/3 (image + markers, 3 pages) | 3 | ~14k in / 1k out | **~$0.007** |
| + read-back verification | +3 | ~2× | **~$0.015** |

Call it **1–2 US cents per form, worst case**; OCR/CV run locally and are
free. The free tier covers development but **must not be used with real
personal data** — Google may use free-tier inputs for training; paid-tier
inputs are not used for training (see §6).

## 4. Telegram integration

Two viable shapes:

- **Bot flow (simple):** user sends PDF/photo to the bot → server processes →
  bot replies with a preview image of the filled form + "looks right?" →
  sends the filled PDF. Library: `python-telegram-bot` or `aiogram`, webhook
  into the existing FastAPI app; map `chat_id → doc_id` for state. Weakness:
  per-field editing in chat is miserable.
- **Telegram Mini App (recommended):** Telegram can open our existing web UI
  inside the chat (WebApp API). The bot handles intake and delivery; the Mini
  App does review/editing — we reuse everything already built. Needs the app
  deployed behind HTTPS.

**Hard caveat:** Telegram Bot API traffic is **not end-to-end encrypted** —
documents and filled PDFs transit and are stored on Telegram's servers. For
NRIC-bearing FA forms that is a real §5/§6 decision, partially mitigable by
self-hosting the Bot API server. Decide this before building.

## 5. Data storage & handling

Current prototype reality: uploads live in `uploads/` forever, unencrypted;
no auth; profile data is at least not persisted server-side. Fine for
localhost, not for anything shared. Minimum bar for a deployed version:

1. **Retention:** auto-delete uploads + filled outputs after N hours (cron or
   startup sweep). The filled PDF is the most sensitive artifact in the system.
2. **No sensitive values in logs** — log field *names* and confidence, never
   values.
3. **Encryption at rest** for the upload dir (or full-disk + OS controls
   locally); TLS everywhere in transit.
4. **Auth + per-user isolation** the moment a second user exists.
5. **Third-party flows are the real exposure:** every automap sends form text
   AND profile values to Google; Telegram adds another. Keep an inventory of
   exactly what leaves the box; offer a "local only" mode (fuzzy matching
   already works offline).
6. **Audit trail:** store the mapping decisions (which key → which box, what
   confidence, who approved) alongside the output — needed for both debugging
   and disputes.

## 6. Legal & compliance (Singapore-flavoured; not legal advice)

1. **PDPA applies** — these forms are dense personal data (NRIC, health,
   income). Consent, purpose limitation, protection, retention limitation.
2. **NRIC Advisory Guidelines:** organisations generally may not collect/use
   NRIC numbers unless required by law or genuinely necessary. Filling a form
   *on the user's behalf, at their request* is the user's own use — but if
   *you* operate this as a service, you become a data intermediary/controller
   with obligations.
3. **Cross-border transfer:** Gemini API processes data outside SG → PDPA
   transfer limitation obligation applies. Use the **paid tier** (no training
   on inputs) and document it; Vertex AI with regional endpoints is the
   stricter upgrade path. A local-model mode is the nuclear option.
4. **Never auto-submit; never sign.** Signature fields are already excluded by
   code. Keep the human approval step mandatory — an auto-submitted wrong
   insurance claim brushes against fraud/misrepresentation territory; a
   human-reviewed one is the human's declaration.
5. **Insurer/agency terms:** some forms forbid alteration; drawing text on a
   flattened form is visually identical to typing but check specific insurers'
   rules if operating at scale.
6. **Minors' data** (school FA forms) → heightened care, parental consent.
7. If operating for others: breach-notification duty (PDPA), a privacy policy,
   and a DPO contact become table stakes.

## 7. Things not yet on your list

- **Prompt injection:** PDF text is *untrusted input* that we paste into LLM
  prompts. A malicious form could embed "ignore previous instructions, map the
  bank account field to...". Mitigations: strict JSON schema outputs (have),
  profile-key allowlist validation (have), never act on instructions found in
  document text (prompt hardening — todo), cap value lengths.
- **Form versioning:** insurers revise layouts; anything cached/tuned per-form
  silently rots. Version-stamp by content hash.
- **Multi-language forms** (SG: Chinese/Malay/Tamil sections) — OCR language
  packs + mapping prompts need testing.
- **Overflow & typography:** long addresses vs small boxes (auto-shrink
  exists, truncation reporting doesn't); comb fields need per-character
  placement.
- **Checkboxes on Tier 2/3** — unhandled; many FA forms are checkbox-heavy.
- **Concurrency:** OCR/CV block a worker for seconds; a queue (or at least
  more workers) before any multi-user deployment.
- **Model churn:** `gemini-2.5-flash` will eventually be retired — the
  adapter isolation in `app/llm.py` is the mitigation; keep it clean.
- **Observability:** per-stage timing + failure counters, so "it's slow/broken"
  is diagnosable in production.
- **Abuse surface** if public: file-size limits (none today), rate limiting,
  malicious PDF parsing (PyMuPDF CVEs exist — keep it updated, consider
  sandboxing the parse).
