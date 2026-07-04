"""Form-Nation prototype backend.

Upload a PDF -> detect tier -> extract AcroForm fields -> render pages ->
auto-map a profile JSON onto fields (fuzzy) -> fill and download.
"""

import difflib
import json
import os
import re
import time
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app import llm, ocr, validate, vision

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="form-nation")

# doc_id -> pdf path (prototype keeps state in memory; uploads persist on disk)
DOCS: dict[str, Path] = {}

# data retention: uploaded forms and filled outputs are sensitive — sweep them
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))


def sweep_uploads():
    cutoff = time.time() - RETENTION_HOURS * 3600
    for p in UPLOAD_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


sweep_uploads()


def detect_tier(doc: fitz.Document) -> int:
    # page.widgets() is a generator (always truthy) — must actually iterate
    if any(True for page in doc for _ in page.widgets()):
        return 1
    if any(page.get_text().strip() for page in doc):
        return 2
    return 3


def nearby_text(words: list, rect: fitz.Rect) -> str:
    """Printed label text around a widget: same row to the left, or just above.
    Gives the LLM context when field names are junk like 'T1' or 'fill_3'."""
    zone = fitz.Rect(rect.x0 - 180, rect.y0 - 22, rect.x1, rect.y1)
    hits = [w for w in words if fitz.Rect(w[:4]).intersects(zone)]
    hits.sort(key=lambda w: (round(w[1]), w[0]))
    return " ".join(w[4] for w in hits)[:200]


def extract_fields(doc: fitz.Document) -> list[dict]:
    fields = []
    for pno, page in enumerate(doc):
        words = page.get_text("words")
        for w in page.widgets() or []:
            ftype = w.field_type_string
            field = {
                "id": w.xref,
                "name": w.field_name or f"field_{w.xref}",
                "type": ftype,
                "page": pno,
                "rect": list(w.rect),  # PDF points, origin top-left
                "value": w.field_value if ftype != "Signature" else None,
                "readonly": bool(w.field_flags & 1) or ftype == "Signature",
                "context": nearby_text(words, w.rect),
            }
            fields.append(field)
    return fields


def extract_candidates(doc: fitz.Document) -> list[dict]:
    """Tier 2/3: CV-detected answer areas presented as pseudo-fields."""
    fields = []
    for pno, page in enumerate(doc):
        for c in vision.detect_answer_candidates(page):
            fields.append({
                "id": f"{pno}-{c['id']}",
                "name": c["context"][:70] or f"area {c['id']} (p{pno + 1})",
                "type": "Text",
                "page": pno,
                "rect": list(c["rect"]),
                "value": "",
                "readonly": False,
                "context": c["context"],
                "empty": c["empty"],
            })
    return fields


def sidecar_path(doc_id: str) -> Path:
    return UPLOAD_DIR / f"{doc_id}.json"


def load_payload(doc_id: str) -> dict:
    """Cached doc payload — CV/OCR detection is expensive, run it once."""
    p = sidecar_path(doc_id)
    if p.exists():
        return json.loads(p.read_text())
    doc = get_doc(doc_id)
    payload = doc_payload(doc_id, doc, DOCS[doc_id].name)
    doc.close()
    p.write_text(json.dumps(payload))
    return payload


def doc_payload(doc_id: str, doc: fitz.Document, filename: str) -> dict:
    tier = detect_tier(doc)
    return {
        "doc_id": doc_id,
        "filename": filename,
        "tier": tier,
        "pages": [
            {"width": p.rect.width, "height": p.rect.height} for p in doc
        ],
        "fields": extract_fields(doc) if tier == 1 else extract_candidates(doc),
    }


@app.post("/api/upload")
async def upload(file: UploadFile):
    sweep_uploads()
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF")
    doc_id = uuid.uuid4().hex[:12]
    path = UPLOAD_DIR / f"{doc_id}.pdf"
    path.write_bytes(await file.read())
    try:
        doc = fitz.open(path)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not open PDF: {e}")
    if detect_tier(doc) == 3:  # scans: deskew once, work on the clean copy
        normalized = ocr.normalize_scan(doc)
        doc.close()
        normalized.save(path)
        doc = fitz.open(path)
    DOCS[doc_id] = path
    result = doc_payload(doc_id, doc, file.filename)
    doc.close()
    sidecar_path(doc_id).write_text(json.dumps(result))
    return result


@app.get("/api/doc/{doc_id}")
def doc_info(doc_id: str):
    get_doc(doc_id).close()  # 404 if unknown
    return load_payload(doc_id)


def get_doc(doc_id: str) -> fitz.Document:
    path = DOCS.get(doc_id)
    if path is None and doc_id.isalnum():  # survive server restarts
        candidate = UPLOAD_DIR / f"{doc_id}.pdf"
        if candidate.exists():
            DOCS[doc_id] = path = candidate
    if path is None or not path.exists():
        raise HTTPException(404, "Unknown document — upload again")
    return fitz.open(path)


@app.get("/api/render/{doc_id}/{page_no}")
def render_page(doc_id: str, page_no: int, scale: float = 2.0):
    doc = get_doc(doc_id)
    if not 0 <= page_no < len(doc):
        raise HTTPException(404, "No such page")
    pix = doc[page_no].get_pixmap(matrix=fitz.Matrix(scale, scale))
    png = pix.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png")


# ---- profile auto-mapping (M1: fuzzy matching; M2 will swap in an LLM) ----

SYNONYMS = {
    "name": ["name of insured", "full name", "name of claimant", "insured"],
    "nric": ["nric no", "nric", "id number", "fin"],
    "policy_no": ["policy no", "policy number", "certificate no"],
    "address": ["address"],
    "phone": ["contact no", "contact number", "telephone", "mobile", "hp"],
    "email": ["email", "e-mail"],
    "date_of_birth": ["date of birth", "dob", "birth date"],
    "occupation": ["occupation", "present occupation"],
    "employer": ["employer", "name of employer", "company"],
    "bank": ["bank", "name of bank"],
    "account_no": ["account no", "account number", "bank account"],
}


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def score_match(profile_key: str, field_name: str) -> float:
    f = normalize(re.sub(r"^\d+\s*", "", field_name))  # drop leading numbering
    candidates = [normalize(profile_key)]
    candidates += [normalize(s) for s in SYNONYMS.get(profile_key.lower(), [])]
    best = 0.0
    for c in candidates:
        ratio = difflib.SequenceMatcher(None, c, f).ratio()
        tokens_c, tokens_f = set(c.split()), set(f.split())
        overlap = len(tokens_c & tokens_f) / len(tokens_c) if tokens_c else 0
        best = max(best, ratio, overlap * 0.9)
    return best


class MapRequest(BaseModel):
    profile: dict[str, str]


def fuzzy_map(fields: list[dict], profile: dict[str, str]) -> dict:
    suggestions = {}
    for field in fields:
        best_key, best_score = None, 0.0
        for key in profile:
            s = score_match(key, field["name"])
            if s > best_score:
                best_key, best_score = key, s
        if best_key and best_score >= 0.55:
            suggestions[str(field["id"])] = {
                "value": profile[best_key],
                "source_key": best_key,
                "confidence": round(best_score, 2),
            }
    return suggestions


def automap_markers(doc: fitz.Document, fields: list[dict],
                    profile: dict[str, str]) -> dict:
    """Tier 2/3: set-of-marks per page — CV boxes, LLM picks marker ids."""
    by_page: dict[int, list[dict]] = {}
    for f in fields:
        by_page.setdefault(f["page"], []).append(f)
    suggestions = {}
    for pno, page_fields in sorted(by_page.items()):
        candidates = [
            {"id": i + 1, "rect": fitz.Rect(f["rect"]),
             "context": f.get("context", ""), "empty": f.get("empty", True)}
            for i, f in enumerate(page_fields)
        ]
        copy = fitz.open()
        copy.insert_pdf(doc, from_page=pno, to_page=pno)
        vision.draw_markers(copy[0], candidates)
        png = copy[0].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
        copy.close()
        for m in llm.map_markers(png, candidates, profile):
            field = page_fields[m["marker_id"] - 1]
            suggestions[field["id"]] = {
                "value": profile[m["profile_key"]],
                "source_key": m["profile_key"],
                "confidence": m["confidence"],
            }
    return suggestions


@app.post("/api/automap/{doc_id}")
def automap(doc_id: str, req: MapRequest):
    payload = load_payload(doc_id)
    tier = payload["tier"]
    fields = [f for f in payload["fields"] if f["type"] == "Text"]
    if not llm.api_key():
        return {"engine": "fuzzy (no GEMINI_API_KEY set)",
                "suggestions": fuzzy_map(fields, req.profile)}
    try:
        if tier == 1:
            return {"engine": llm.GEMINI_MODEL,
                    "suggestions": llm.map_fields(fields, req.profile)}
        doc = get_doc(doc_id)
        try:
            return {"engine": f"{llm.GEMINI_MODEL} + set-of-marks",
                    "suggestions": automap_markers(doc, fields, req.profile)}
        finally:
            doc.close()
    except Exception as e:
        return {"engine": f"fuzzy (LLM failed: {e})",
                "suggestions": fuzzy_map(fields, req.profile)}


class ValidateRequest(BaseModel):
    values: dict[str, object]


@app.post("/api/validate/{doc_id}")
def validate_values(doc_id: str, req: ValidateRequest):
    payload = load_payload(doc_id)
    return {"issues": validate.check_all(payload["fields"], req.values)}


# ---- fill & export ----

class FillRequest(BaseModel):
    values: dict[str, object]  # field xref (as str) -> value


def fill_widgets(doc: fitz.Document, values: dict) -> tuple[int, list[str]]:
    filled, skipped = 0, []
    for page in doc:
        for w in page.widgets() or []:
            val = values.get(str(w.xref))
            if val is None:
                continue
            if w.field_type_string == "Signature":
                skipped.append(w.field_name)  # never auto-sign
                continue
            if w.field_type_string == "CheckBox":
                w.field_value = bool(val)
            else:
                w.field_value = str(val)
            w.update()
            filled += 1
    return filled, skipped


def fill_flat(doc: fitz.Document, values: dict, fields: list[dict]) -> int:
    """Tier 2/3: draw text into CV-detected answer areas."""
    rects = {f["id"]: (f["page"], fitz.Rect(f["rect"])) for f in fields}
    filled = 0
    for key, val in values.items():
        if key not in rects or not str(val).strip():
            continue
        pno, rect = rects[key]
        box = fitz.Rect(rect.x0 + 2, rect.y0 + 1, rect.x1 - 2, rect.y1 - 1)
        for size in (10, 9, 8, 7, 6):
            if doc[pno].insert_textbox(box, str(val), fontsize=size,
                                       fontname="helv") >= 0:
                filled += 1
                break
    return filled


@app.post("/api/fill/{doc_id}")
def fill(doc_id: str, req: FillRequest):
    payload = load_payload(doc_id)
    doc = get_doc(doc_id)
    skipped: list[str] = []
    if payload["tier"] == 1:
        filled, skipped = fill_widgets(doc, req.values)
    else:
        filled = fill_flat(doc, req.values, payload["fields"])
    out = UPLOAD_DIR / f"{doc_id}_filled.pdf"
    doc.save(out)
    doc.close()
    return FileResponse(
        out,
        media_type="application/pdf",
        filename="filled.pdf",
        headers={"X-Filled": str(filled), "X-Skipped": ",".join(skipped)},
    )


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
