"""A/B experiment: Gemini raw bounding boxes vs grid-overlay prompting.

Treats page 1 of the GEG form as if it were a flat scan (Tier 3), asks Gemini
to locate a set of answer areas two ways, and scores both against the ground
truth from the PDF's real AcroForm field rects.

Usage: .venv/bin/python scripts/vision_experiment.py
"""

import json
import string
import sys
from pathlib import Path

import fitz
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.llm import api_key, generate  # noqa: E402

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "GEG -personal-accident-claim-form.pdf"
OUT = ROOT / "docs"
OUT.mkdir(exist_ok=True)

# fields to locate (page 0), keyed by the printed label we give the model
TARGETS = {
    "Name of Insured": "Name of Insured",
    "NRIC No": "NRIC No",
    "Policy No": "Policy No",
    "Address": "Address",
    "Contact No": "Contact No",
    "Date of Birth": "2 Date of Birth",
    "Email": "4 Email",
    "Present occupation": "5 Present occupation if more than one state all",
    "Name of Claimant": "Name of Claimant",
}

GRID_COLS = 12  # square cells: 612/12 = 51pt -> 15.5 rows on a letter page


def call_gemini(prompt: str, png: bytes) -> list[dict]:
    return generate(prompt, png)


def ground_truth(page: fitz.Page) -> dict[str, fitz.Rect]:
    rects = {w.field_name: fitz.Rect(w.rect) for w in page.widgets() or []}
    return {label: rects[fname] for label, fname in TARGETS.items()}


def render(page: fitz.Page, scale=2.0) -> bytes:
    return page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes("png")


def draw_grid(page: fitz.Page, step: float):
    w, h = page.rect.width, page.rect.height
    red = (0.85, 0.1, 0.1)
    for i in range(GRID_COLS + 1):
        page.draw_line((i * step, 0), (i * step, h), color=red, width=0.5)
        if i < GRID_COLS:
            page.insert_text((i * step + 2, 9), string.ascii_uppercase[i],
                             fontsize=7, color=red)
    r = 1
    y = step
    while y < h:
        page.draw_line((0, y), (w, y), color=red, width=0.5)
        page.insert_text((2, y - 2), str(r), fontsize=7, color=red)
        y += step
        r += 1


def cells_to_rect(cells: list[str], step: float) -> fitz.Rect | None:
    rect = None
    for c in cells:
        c = c.strip().upper()
        if not c or c[0] not in string.ascii_uppercase or not c[1:].isdigit():
            continue
        col, row = string.ascii_uppercase.index(c[0]), int(c[1:]) - 1
        cell = fitz.Rect(col * step, row * step, (col + 1) * step, (row + 1) * step)
        rect = cell if rect is None else rect | cell
    return rect


def score(pred: fitz.Rect | None, truth: fitz.Rect) -> dict:
    if pred is None or pred.is_empty:
        return {"iou": 0.0, "center_err": None, "hit": False}
    inter = fitz.Rect(pred) & truth
    ia = max(0, inter.width) * max(0, inter.height) if not inter.is_empty else 0
    union = pred.get_area() + truth.get_area() - ia
    cx, cy = (pred.x0 + pred.x1) / 2, (pred.y0 + pred.y1) / 2
    tx, ty = (truth.x0 + truth.x1) / 2, (truth.y0 + truth.y1) / 2
    return {
        "iou": round(ia / union, 3) if union else 0.0,
        "center_err": round(((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5, 1),
        "hit": truth.contains(fitz.Point(cx, cy)),
    }


def annotate(pdf_page_no: int, preds: dict[str, fitz.Rect | None],
             truths: dict[str, fitz.Rect], outfile: Path, grid_step=None):
    doc = fitz.open(PDF)
    page = doc[pdf_page_no]
    if grid_step:
        draw_grid(page, grid_step)
    for label, t in truths.items():
        page.draw_rect(t, color=(0, 0.6, 0.2), width=1.2)      # truth: green
        p = preds.get(label)
        if p:
            page.draw_rect(p, color=(0.9, 0.1, 0.1), width=1.2)  # pred: red
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(30, 130, 582, 320))
    pix.save(outfile)
    doc.close()


def main():
    if not api_key():
        sys.exit("GEMINI_API_KEY not set")
    labels = list(TARGETS)
    doc = fitz.open(PDF)
    page = doc[0]
    truths = ground_truth(page)
    W, H = page.rect.width, page.rect.height
    step = W / GRID_COLS

    # ---- Method A: raw bounding boxes on the plain image ----
    plain_png = render(page)
    prompt_a = (
        "This is a scanned insurance claim form. For each field label below, "
        "find the EMPTY ANSWER AREA (the box or space where the value gets "
        "written, not the printed label itself) and return its bounding box.\n"
        f"Labels: {json.dumps(labels)}\n"
        'Return a JSON array of {"label": str, "box_2d": [ymin, xmin, ymax, '
        "xmax]} with coordinates normalized to 0-1000."
    )
    raw_a = call_gemini(prompt_a, plain_png)
    preds_a = {}
    for item in raw_a:
        if item.get("label") in truths and len(item.get("box_2d", [])) == 4:
            y0, x0, y1, x1 = item["box_2d"]
            preds_a[item["label"]] = fitz.Rect(
                x0 / 1000 * W, y0 / 1000 * H, x1 / 1000 * W, y1 / 1000 * H)

    # ---- Method B: grid overlay, model names cells only ----
    gdoc = fitz.open(PDF)
    gpage = gdoc[0]
    draw_grid(gpage, step)
    grid_png = render(gpage)
    gdoc.close()
    rows = int(H / step) + 1
    prompt_b = (
        "This scanned insurance claim form has a red reference grid drawn on "
        f"it: columns A-{string.ascii_uppercase[GRID_COLS-1]} left to right, "
        f"rows 1-{rows} top to bottom (cell A1 is the top-left square; red "
        "letters/numbers mark each column/row). For each field label below, "
        "list ALL grid cells that the EMPTY ANSWER AREA occupies (the space "
        "where the value gets written, not the printed label).\n"
        f"Labels: {json.dumps(labels)}\n"
        'Return a JSON array of {"label": str, "cells": ["C4", "D4", ...]}.'
    )
    raw_b = call_gemini(prompt_b, grid_png)
    preds_b = {}
    for item in raw_b:
        if item.get("label") in truths:
            preds_b[item["label"]] = cells_to_rect(item.get("cells", []), step)

    # ---- score & report ----
    results = {}
    for method, preds in (("A_raw_bbox", preds_a), ("B_grid", preds_b)):
        rows_ = {lbl: score(preds.get(lbl), truths[lbl]) for lbl in labels}
        results[method] = rows_
    print(f"{'field':<22} {'A iou':>6} {'A err':>6} {'A hit':>5} "
          f"{'B iou':>6} {'B err':>6} {'B hit':>5}")
    for lbl in labels:
        a, b = results["A_raw_bbox"][lbl], results["B_grid"][lbl]
        print(f"{lbl:<22} {a['iou']:>6} {str(a['center_err']):>6} "
              f"{str(a['hit']):>5} {b['iou']:>6} {str(b['center_err']):>6} "
              f"{str(b['hit']):>5}")
    for m in results:
        vals = results[m].values()
        ious = [v["iou"] for v in vals]
        errs = [v["center_err"] for v in vals if v["center_err"] is not None]
        hits = sum(v["hit"] for v in vals)
        print(f"{m}: mean IoU {sum(ious)/len(ious):.3f} | "
              f"mean center err {sum(errs)/len(errs):.1f}pt | "
              f"hits {hits}/{len(labels)}")

    annotate(0, preds_a, truths, OUT / "vision-A-raw-bbox.png")
    annotate(0, preds_b, truths, OUT / "vision-B-grid.png", grid_step=step)
    (OUT / "vision-results.json").write_text(json.dumps(
        {m: {k: v for k, v in r.items()} for m, r in results.items()}, indent=2))
    print("annotated images: docs/vision-A-raw-bbox.png, docs/vision-B-grid.png")


if __name__ == "__main__":
    main()
