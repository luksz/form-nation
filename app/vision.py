"""Set-of-marks field detection for Tier 2/3 (flattened PDFs and scans).

Classical CV finds candidate answer areas with pixel precision; the LLM is
only ever asked "which marker number is field X?" — geometry never comes
from the model.

Pipeline: render page -> detect table cells via line morphology (OpenCV)
-> subtract printed-label area inside each cell (text layer or OCR words)
-> numbered candidate rects, ready to draw as markers.
All rects are in PDF points.
"""

import cv2
import fitz
import numpy as np

SCALE = 2.0  # render resolution for CV


def _detect_cells(img_gray: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Find enclosed table cells from ruling lines. Returns px rects."""
    binv = cv2.adaptiveThreshold(
        img_gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25))
    h_lines = cv2.dilate(cv2.erode(binv, h_kernel), h_kernel, iterations=2)
    v_lines = cv2.dilate(cv2.erode(binv, v_kernel), v_kernel, iterations=2)
    grid = cv2.bitwise_or(h_lines, v_lines)
    # cells = holes in the line mask -> invert and take connected components
    contours, hierarchy = cv2.findContours(
        grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    cells = []
    if hierarchy is None:
        return cells
    for cnt, hier in zip(contours, hierarchy[0]):
        if hier[3] == -1:  # keep only inner contours (holes = cell interiors)
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        if w < 70 or h < 16 or h > 400 or w > img_gray.shape[1] * 0.97:
            continue
        cells.append((x, y, x + w, y + h))
    return cells


def _detect_checkboxes(img_gray: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Find small empty squares (checkboxes). Returns px rects."""
    binv = cv2.adaptiveThreshold(
        img_gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)
    contours, _ = cv2.findContours(binv, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)
    H, W = img_gray.shape
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if not (16 <= w <= 44 and 16 <= h <= 44):
            continue
        if abs(w - h) > 0.25 * max(w, h):        # must be square-ish
            continue
        if cv2.contourArea(cnt) < 0.7 * w * h:   # solid rectangular outline
            continue
        inset = max(3, w // 5)
        inner = binv[y + inset:y + h - inset, x + inset:x + w - inset]
        if inner.size == 0 or inner.mean() > 30:  # interior must be empty
            continue
        # surroundings must be light — rejects glyphs in dark headers
        pad = 4
        outer = img_gray[max(0, y - pad):min(H, y + h + pad),
                         max(0, x - pad):min(W, x + w + pad)]
        if outer.mean() < 150:
            continue
        boxes.append((x, y, x + w, y + h))
    # dedupe near-identical (inner+outer contour of the same square)
    unique = []
    for b in boxes:
        if not any(abs(b[0] - u[0]) < 6 and abs(b[1] - u[1]) < 6
                   for u in unique):
            unique.append(b)
    return unique


def _subtract_label(cell: fitz.Rect, words: list) -> fitz.Rect:
    """Empty answer area = cell minus the printed text inside it."""
    inside = [fitz.Rect(w[:4]) for w in words
              if fitz.Rect(w[:4]).intersects(cell)]
    if not inside:
        return cell
    text_bbox = inside[0]
    for r in inside[1:]:
        text_bbox |= r
    below = fitz.Rect(cell.x0, text_bbox.y1 + 1, cell.x1, cell.y1)
    right = fitz.Rect(text_bbox.x1 + 3, cell.y0, cell.x1, cell.y1)
    # prefer whichever leftover region is larger and usable
    best = max((below, right), key=lambda r: max(0, r.get_area()))
    if best.height >= 8 and best.width >= 30:
        return best
    return cell


def get_words(page: fitz.Page) -> list[tuple]:
    """Text-layer words when available (Tier 2), OCR otherwise (Tier 3)."""
    words = page.get_text("words")
    if words:
        return words
    from app import ocr
    return ocr.ocr_words(page)


def detect_answer_candidates(page: fitz.Page) -> list[dict]:
    """Returns [{'id': int, 'rect': fitz.Rect, 'context': str}] in PDF points."""
    pix = page.get_pixmap(matrix=fitz.Matrix(SCALE, SCALE))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    cells_px = _detect_cells(gray)

    words = get_words(page)
    candidates = []
    for x0, y0, x1, y1 in cells_px:
        cell = fitz.Rect(x0 / SCALE, y0 / SCALE, x1 / SCALE, y1 / SCALE)
        answer = _subtract_label(cell, words)
        # label text: words inside the cell PLUS words just left of / above it,
        # so empty answer boxes still carry the label they belong to
        zone = fitz.Rect(cell.x0 - 200, cell.y0 - 20, cell.x1, cell.y1)
        near = [w for w in words if fitz.Rect(w[:4]).intersects(zone)]
        near.sort(key=lambda w: (round(w[1]), w[0]))
        shrunk = fitz.Rect(answer) + (2, 2, -2, -2)
        is_empty = not any(fitz.Rect(w[:4]).intersects(shrunk) for w in words)
        candidates.append({"rect": answer, "empty": is_empty,
                           "context": " ".join(w[4] for w in near)[:150]})

    for x0, y0, x1, y1 in _detect_checkboxes(gray):
        box = fitz.Rect(x0 / SCALE, y0 / SCALE, x1 / SCALE, y1 / SCALE)
        # checkbox labels sit beside or just above the square
        zone = fitz.Rect(box.x0 - 70, box.y0 - 16, box.x1 + 70, box.y1 + 4)
        near = [w for w in words if fitz.Rect(w[:4]).intersects(zone)]
        near.sort(key=lambda w: (round(w[1]), w[0]))
        candidates.append({"rect": box, "empty": True, "kind": "checkbox",
                           "context": " ".join(w[4] for w in near)[:100]})

    # dedupe near-identical rects (double contours), keep reading order;
    # only compare same-kind candidates — a checkbox inside a table cell
    # is not a duplicate of that cell
    candidates.sort(key=lambda c: (round(c["rect"].y0 / 8), c["rect"].x0))
    unique = []
    for c in candidates:
        if any((c["rect"] & u["rect"]).get_area() >
               0.8 * min(c["rect"].get_area(), u["rect"].get_area())
               for u in unique
               if u.get("kind") == c.get("kind")
               and not (c["rect"] & u["rect"]).is_empty):
            continue
        unique.append(c)
    for i, c in enumerate(unique, start=1):
        c["id"] = i
    return unique


def draw_markers(page: fitz.Page, candidates: list[dict]):
    """Draw numbered set-of-marks badges onto a (copy of a) page."""
    red = (0.85, 0.1, 0.1)
    for c in candidates:
        r = c["rect"]
        page.draw_rect(r, color=red, width=0.8)
        badge = fitz.Rect(r.x0, r.y0, r.x0 + 7 + 5 * len(str(c["id"])), r.y0 + 10)
        page.draw_rect(badge, color=red, fill=red)
        page.insert_text((badge.x0 + 2, badge.y1 - 2), str(c["id"]),
                         fontsize=7, color=(1, 1, 1))
