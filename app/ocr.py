"""Tier 3 support: OCR word extraction and scan normalization (deskew).

ocr_words() returns fitz-style word tuples so the set-of-marks pipeline in
vision.py works identically whether the words came from a PDF text layer
(Tier 2) or from OCR (Tier 3).
"""

import cv2
import fitz
import numpy as np

OCR_SCALE = 2.0
MIN_CONFIDENCE = 0.5

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _page_image(page: fitz.Page, scale: float) -> np.ndarray:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)


def ocr_words(page: fitz.Page) -> list[tuple]:
    """OCR the page; returns (x0, y0, x1, y1, text, 0, 0, 0) in PDF points.

    Each OCR line becomes one 'word' tuple — coarser than a text layer but
    sufficient for label context and emptiness checks.
    """
    img = _page_image(page, OCR_SCALE)
    result, _ = _get_engine()(img)
    words = []
    for box, text, score in result or []:
        if float(score) < MIN_CONFIDENCE or not text.strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        words.append((min(xs) / OCR_SCALE, min(ys) / OCR_SCALE,
                      max(xs) / OCR_SCALE, max(ys) / OCR_SCALE,
                      text.strip(), 0, 0, 0))
    return words


def _skew_angle(img: np.ndarray) -> float:
    """Estimate page skew in degrees from the long horizontal ruling lines."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    binv = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1))
    h_lines = cv2.dilate(cv2.erode(binv, h_kernel), h_kernel)
    segments = cv2.HoughLinesP(h_lines, 1, np.pi / 360, threshold=100,
                               minLineLength=img.shape[1] // 3, maxLineGap=8)
    if segments is None:
        return 0.0
    angles = []
    for seg in np.asarray(segments).reshape(-1, 4):
        x1, y1, x2, y2 = (float(v) for v in seg)
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) < 15:
            angles.append(a)
    return float(np.median(angles)) if angles else 0.0


def normalize_scan(doc: fitz.Document) -> fitz.Document:
    """Deskew every page; returns a new image-backed PDF at original sizes."""
    out = fitz.open()
    for page in doc:
        img = _page_image(page, OCR_SCALE)
        angle = _skew_angle(img)
        if abs(angle) > 0.15:
            h, w = img.shape[:2]
            m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            img = cv2.warpAffine(img, m, (w, h),
                                 borderValue=(255, 255, 255))
        ok, png = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        new_page = out.new_page(width=page.rect.width,
                                height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=png.tobytes())
    return out
