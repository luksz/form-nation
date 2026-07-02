# form-nation

Auto form-filling agent for FA forms. See [PLAN.md](PLAN.md) for the full design,
tiered pipeline, and roadmap.

## Run the prototype (M0/M1)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and drop in a PDF form
(e.g. the GEG personal accident claim form in this repo).

Sample inputs to try: the GEG PDF (Tier 1, fillable), `samples/GEG-flattened.pdf`
(Tier 2, no form fields), `samples/GEG-scan.pdf` (Tier 3, skewed noisy scan).

What it does today:

- Detects the form tier (fillable AcroForm / flattened / scan); scans are
  deskewed and OCR'd (RapidOCR) automatically
- Extracts every field with its exact position (AcroForm data on Tier 1,
  OpenCV cell detection on Tiers 2-3), renders pages with bounding-box and
  grid overlays
- Auto-fills text fields from a profile JSON, with confidence badges for review.
  With `GEMINI_API_KEY` set (copy `.env.example` to `.env`) it uses LLM semantic
  mapping; without a key it falls back to offline fuzzy matching
- Fills the PDF and downloads it — signature fields are never auto-filled
