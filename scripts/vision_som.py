"""Method C benchmark: set-of-marks (CV boxes + Gemini as chooser).

Same targets and scoring as scripts/vision_experiment.py so all three
methods are directly comparable.

Usage: .venv/bin/python scripts/vision_som.py
"""

import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.vision import detect_answer_candidates, draw_markers  # noqa: E402
from vision_experiment import (  # noqa: E402
    OUT, PDF, TARGETS, call_gemini, ground_truth, render, score,
)


def main():
    doc = fitz.open(PDF)
    page = doc[0]
    truths = ground_truth(page)
    labels = list(TARGETS)

    candidates = detect_answer_candidates(page)
    print(f"CV detected {len(candidates)} candidate answer areas")

    # draw markers on a copy and ask Gemini to choose marker ids only
    mdoc = fitz.open(PDF)
    draw_markers(mdoc[0], candidates)
    marked_png = render(mdoc[0])
    (OUT / "vision-C-markers-input.png").write_bytes(marked_png)
    mdoc.close()

    listing = "\n".join(
        f"{c['id']}: near text \"{c['context']}\"" for c in candidates)
    prompt = (
        "This insurance claim form has red numbered markers drawn on every "
        "candidate answer area (the number badge sits at the top-left of its "
        "box). Marker list with the printed text found inside each area:\n"
        f"{listing}\n\n"
        "For each field label below, pick the marker whose box is where that "
        "field's VALUE should be written. Use null if no marker fits.\n"
        f"Labels: {json.dumps(labels)}\n"
        'Return a JSON array of {"label": str, "marker_id": int | null}.'
    )
    raw = call_gemini(prompt, marked_png)

    by_id = {c["id"]: c["rect"] for c in candidates}
    preds = {}
    for item in raw:
        if item.get("label") in truths and item.get("marker_id") in by_id:
            preds[item["label"]] = by_id[item["marker_id"]]

    print(f"{'field':<22} {'C iou':>6} {'C err':>6} {'C hit':>5}")
    rows = {}
    for lbl in labels:
        s = score(preds.get(lbl), truths[lbl])
        rows[lbl] = s
        print(f"{lbl:<22} {s['iou']:>6} {str(s['center_err']):>6} "
              f"{str(s['hit']):>5}")
    ious = [s["iou"] for s in rows.values()]
    errs = [s["center_err"] for s in rows.values() if s["center_err"] is not None]
    hits = sum(s["hit"] for s in rows.values())
    print(f"C_set_of_marks: mean IoU {sum(ious)/len(ious):.3f} | "
          f"mean center err {sum(errs)/len(errs) if errs else 0:.1f}pt | "
          f"hits {hits}/{len(labels)}")

    # annotate result: truth green, chosen candidate red
    vdoc = fitz.open(PDF)
    vpage = vdoc[0]
    for lbl, t in truths.items():
        vpage.draw_rect(t, color=(0, 0.6, 0.2), width=1.2)
        if lbl in preds:
            vpage.draw_rect(preds[lbl], color=(0.9, 0.1, 0.1), width=1.2)
    vpage.get_pixmap(matrix=fitz.Matrix(2, 2),
                     clip=fitz.Rect(30, 130, 582, 320)).save(
        OUT / "vision-C-set-of-marks.png")
    vdoc.close()

    results = json.loads((OUT / "vision-results.json").read_text()) \
        if (OUT / "vision-results.json").exists() else {}
    results["C_set_of_marks"] = rows
    (OUT / "vision-results.json").write_text(json.dumps(results, indent=2))
    print("annotated image: docs/vision-C-set-of-marks.png")


if __name__ == "__main__":
    main()
