"""PII redaction before LLM calls.

High-sensitivity identifiers (NRIC/FIN, email, phone, account numbers) match
strict patterns, so they are replaced with placeholders locally before any
text is sent to the LLM, and substituted back into the LLM's output locally.
The LLM only ever sees "<NRIC_1>".

Names and addresses cannot be pattern-redacted (extracting them requires the
LLM to read them) — that residual exposure is why the paid API tier
(no training on inputs) is still required for real client data.
"""

import os
import re

PATTERNS = [
    ("NRIC", re.compile(r"\b[STFGstfg]\d{7}[A-Za-z]\b")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("ACCOUNT", re.compile(r"\b\d{3}-\d{5,9}(?:-\d{1,4})?\b")),
    ("PHONE", re.compile(r"(?<![\d-])(?:\+65[ -]?)?[3689]\d{3}[ -]?\d{4}(?![\d-])")),
]


def enabled() -> bool:
    return os.environ.get("REDACT_BEFORE_LLM", "true").lower() != "false"


class Redactor:
    """One instance per LLM call so placeholder numbering stays consistent
    across the message, the known profile, and the response."""

    def __init__(self):
        self.mapping: dict[str, str] = {}   # placeholder -> original
        self._seen: dict[str, str] = {}     # original -> placeholder
        self._counts: dict[str, int] = {}

    def redact(self, text: str) -> str:
        if not enabled() or not text:
            return text
        out = text
        for label, pattern in PATTERNS:
            def sub(m, label=label):
                original = m.group(0)
                if original not in self._seen:
                    self._counts[label] = self._counts.get(label, 0) + 1
                    ph = f"<{label}_{self._counts[label]}>"
                    self._seen[original] = ph
                    self.mapping[ph] = original
                return self._seen[original]
            out = pattern.sub(sub, out)
        return out

    def restore(self, text: str) -> str:
        for ph, original in self.mapping.items():
            text = text.replace(ph, original)
        return text

    def restore_values(self, d: dict) -> dict:
        return {k: self.restore(v) if isinstance(v, str) else v
                for k, v in d.items()}
