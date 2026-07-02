# Form-Nation — Auto Form-Filling Agent for FA Forms

**Goal:** Upload a Financial Assistance / insurance claim form (PDF), have an agent
figure out where every field is, fill it from the user's profile data, show the result
as a visual overlay for human review, and export the filled PDF.

---

## 1. The key insight: not every form needs OCR

There are three kinds of "PDF form" in the wild, and they need completely different
treatment. Treating them as one problem is how these projects fail. Instead we build
a **tiered pipeline** and route each uploaded document to the cheapest tier that works:

| Tier | Input type | How we find fields | Accuracy | OCR needed? | LLM needed? |
|------|-----------|--------------------|----------|-------------|-------------|
| **1** | Fillable PDF (AcroForm) | Read field definitions directly from the PDF | ~100% (deterministic) | No | Only for *mapping* values to fields |
| **2** | Flattened digital PDF (printed boxes/lines, no interactive fields) | PDF text layer + layout analysis + vision LLM | High | No (text layer exists) | Yes |
| **3** | Scan or phone photo | OCR + vision LLM with grid/box overlay | Medium–High | Yes | Yes |

A tier-detection step runs on upload:

```
upload.pdf
   │
   ├─ has AcroForm widgets? ──────────────► Tier 1 (direct fill)
   ├─ has a text layer but no widgets? ───► Tier 2 (layout analysis)
   └─ image-only pages? ──────────────────► Tier 3 (OCR + vision)
```

**Tier 1 is the jackpot.** A fillable PDF already contains every field's name, type
(text / checkbox / radio / dropdown), exact rectangle, and page number. We read that
with PyMuPDF, no vision model involved, and filling is a deterministic API call.
The only "AI" job left is semantic mapping (profile key `full_name` → field
`Name of Claimant`), which is a cheap text-only LLM call — or even fuzzy string
matching for well-named fields.

## 2. Your grid-overlay idea — assessment

You proposed rendering the form with a grid overlay so the vision LLM can reference
locations more precisely than raw bounding-box prediction. **This is a real, validated
technique** (it's related to "set-of-marks" prompting and coordinate-grounding used in
computer-use agents), and it's the right call for Tiers 2–3. Details:

**Why it works:** Vision LLMs are bad at emitting precise pixel coordinates from a
bare image but good at *reading labels*. If you overlay a labelled grid (A1–H12 style)
or numbered markers on detected boxes, the model only has to say "the NRIC field is at
D4" or "box #17", and deterministic code converts that back to exact coordinates.

**The stronger variant (what we'll build):** don't make the LLM guess free-space grid
cells at all. Instead:

1. **Detect candidate field regions with classical CV/OCR first** — OCR gives every
   printed word a precise bounding box; line/box detection (OpenCV morphology) finds
   the empty boxes, comb fields, and underscores where answers go.
2. **Number those candidates and draw them on the image** (set-of-marks overlay).
3. **Ask the vision LLM only the semantic question:** "marker #17 sits next to the
   text 'Name of Insured' — which profile field belongs there?" The LLM never emits
   coordinates; it only picks marker IDs. Coordinates stay deterministic.
4. Grid overlay remains as a **fallback** for regions where box detection finds
   nothing (e.g. write-on-the-line answers), and as a human-readable debugging view.

This hybrid is meaningfully more accurate than either raw bounding-box prediction or
grid-only prompting, because the precision-critical part (geometry) never touches the
LLM.

## 3. Architecture

```
┌──────────────┐     ┌─────────────────────────────────────────────┐
│   Frontend    │     │                Backend (FastAPI)             │
│  (HTML/JS)    │     │                                             │
│               │     │  /upload ──► tier detector                   │
│ • upload PDF  │◄───►│      ├─ Tier 1: PyMuPDF widget extraction    │
│ • page viewer │     │      ├─ Tier 2: text layer + layout analysis │
│ • bbox + grid │     │      └─ Tier 3: OCR (PaddleOCR) + CV boxes   │
│   overlays    │     │  /render ──► page → PNG (+optional overlays) │
│ • field panel │     │  /map ─────► profile JSON → field mapping    │
│ • edit values │     │              (fuzzy match, later Claude API) │
│ • download    │     │  /fill ────► write values into PDF, export   │
└──────────────┘     └─────────────────────────────────────────────┘
```

**Stack:** Python 3.11+, FastAPI + Uvicorn, PyMuPDF (rendering, AcroForm read/write),
vanilla HTML/JS frontend (canvas overlays), later: PaddleOCR + OpenCV (Tier 3),
Claude API (semantic mapping + Tier 2/3 vision).

**Human-in-the-loop is a feature, not a fallback.** These are financial/insurance
forms; a wrong NRIC or amount is worse than an empty field. The overlay viewer you
asked for is exactly the review step: every auto-filled value is displayed on top of
the form, colour-coded by confidence, and the human confirms before export.

## 4. Milestones

- **M0 — Prototype viewer (this repo, now):** upload a PDF → detect tier → extract
  AcroForm fields → render pages with bounding-box + grid overlays → edit values in
  a side panel → fill & download the completed PDF. Works fully offline, no LLM.
- **M1 — Profile auto-mapping:** upload/paste a profile JSON; fuzzy-match keys to
  field names; one-click "auto-fill all", confidence colours in the overlay.
- **M2 — LLM semantic mapping:** swap fuzzy matching for an LLM call
  (field names + surrounding label text → profile keys). Handles badly named fields
  (`T1`, `fill_3`) via nearby label text, and knows the difference between "Name of
  Insured" and "Name of Doctor". Provider-agnostic adapter in `app/llm.py` —
  currently Gemini 2.5 Flash (set `GEMINI_API_KEY` in `.env`); swapping to Claude
  is a one-function change.
- **M3 — Tier 2 (flattened PDFs)** ✅ **done:** OpenCV cell detection + text-layer
  label subtraction (`app/vision.py`) produces numbered candidate areas; Gemini
  picks marker ids only (never coordinates); fill draws text at the CV rects.
  Validated on a flattened copy of the GEG form (`samples/GEG-flattened.pdf`).
  Benchmark (scripts/vision_experiment.py + vision_som.py, 9 fields vs AcroForm
  ground truth): raw LLM bboxes 3/9 correct, grid overlay 1/9, set-of-marks 6/9
  before the empty-box hint and all core fields correct after it — confirming §2.
- **M4 — Tier 3 (scans)** ✅ **done:** `app/ocr.py` — RapidOCR (ONNX, no system
  deps) supplies word boxes when there's no text layer; scans are deskewed once
  at upload (Hough-line skew estimate) and all downstream steps work on the
  normalized copy. Validated end-to-end on a synthetic scan
  (`samples/GEG-scan.pdf`: 1.4° skew + noise + JPEG artifacts) — all core
  fields filled correctly. Detection results are cached per document
  (`uploads/<id>.json`) so OCR/CV runs once, not per request.

## 5. Limitations & risks (read this before trusting it)

1. **Signature fields will never be auto-filled.** Legally and ethically off-limits;
   the agent flags them for the human.
2. **XFA forms** (older Adobe LiveCycle, some gov forms) are a different beast from
   AcroForm; PyMuPDF support is partial. We detect and warn rather than mis-fill.
3. **Tier 3 accuracy ceiling:** OCR on poor scans/handwriting misreads; vision LLM
   mapping can be confidently wrong. Mitigation: confidence scores + mandatory human
   review, never silent auto-submit.
4. **LLM coordinate outputs are untrustworthy** — that's the whole reason for the
   marker/grid design. Any future temptation to "just ask the model for the bbox"
   should be resisted.
5. **Checkbox/radio quirks:** AcroForm checkboxes have per-field "on" values
   (`/Yes`, `/On`, `/1`…) that must be read from the field, not assumed.
6. **Privacy:** profiles contain NRIC, income, medical details. Prototype stores
   uploads locally only; anything sent to an LLM API leaves the machine — that must
   be an explicit, visible user choice (and is why M0–M1 are fully offline).
7. **Fonts/appearance streams:** after programmatic fill, some viewers show blank
   fields until appearance regeneration — PyMuPDF handles this but it needs testing
   per form.
8. **Multi-page and overflow:** long answers can exceed a field's rect; we must clip
   or shrink font, not overflow into neighbouring fields.

## 6. What I need from you

- **More real forms** (you've provided the GEG personal accident claim form —
  a few more, ideally one flattened and one scanned, so every tier has a test case).
- **A sample profile** — realistic-but-fake JSON of the data you'd fill in
  (name, NRIC, address, policy number, bank details…).
- **An Anthropic API key** when we reach M2 (not needed for M0–M1).
- **A decision on hosting** eventually: local-only tool vs. deployed web app
  (deployment raises the privacy stakes significantly).
