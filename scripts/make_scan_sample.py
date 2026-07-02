"""Create a Tier-3 test sample: a fake 'scanned' copy of the GEG form.

Renders the flattened form to images, then degrades them like a real scan:
slight rotation (skew), gaussian noise, mild blur. The output PDF has no
text layer at all — pure images.
"""

from pathlib import Path

import cv2
import fitz
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "samples" / "GEG-flattened.pdf"
DEST = ROOT / "samples" / "GEG-scan.pdf"

SKEW_DEG = 1.4
NOISE_SIGMA = 8

rng = np.random.default_rng(42)
doc = fitz.open(SRC)
out = fitz.open()

for page in doc:
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n).copy()

    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), SKEW_DEG, 1.0)
    img = cv2.warpAffine(img, m, (w, h), borderValue=(255, 255, 255))

    img = cv2.GaussianBlur(img, (3, 3), 0)
    noise = rng.normal(0, NOISE_SIGMA, img.shape)
    img = np.clip(img.astype(np.float64) + noise, 0, 255).astype(np.uint8)

    ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, 82])
    new_page = out.new_page(width=page.rect.width, height=page.rect.height)
    new_page.insert_image(new_page.rect, stream=jpg.tobytes())

out.save(DEST)
check = fitz.open(DEST)
print(f"saved {DEST} — text chars: "
      f"{sum(len(p.get_text()) for p in check)} (should be 0)")
