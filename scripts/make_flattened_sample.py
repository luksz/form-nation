"""Create a Tier-2 test sample: the GEG form with its AcroForm baked away.

The result looks identical but has no interactive fields — only printed
boxes and a text layer — which is what many downloaded/emailed forms are.
"""

from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "GEG -personal-accident-claim-form.pdf"
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)

doc = fitz.open(SRC)
doc.bake(annots=True, widgets=True)
dest = OUT / "GEG-flattened.pdf"
doc.save(dest)

check = fitz.open(dest)
widgets = sum(len(list(p.widgets() or [])) for p in check)
text = sum(len(p.get_text()) for p in check)
print(f"saved {dest} — widgets: {widgets}, text chars: {text}")
