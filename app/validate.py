"""Deterministic value validators — the cheap self-check layer.

Infers each field's expected type from its name/label text and checks the
proposed value against it. A type mismatch is a strong signal that a value
was mapped into the wrong box. Everything here is a warning, never a blocker:
the human decides.
"""

import re
from datetime import datetime

NRIC_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
NRIC_LETTERS = {
    "S": "JZIHGFEDCBA", "T": "GFEDCBAJZIH",
    "F": "XWUTRQPNMLK", "G": "RQPNMLKXWUT",
}

TYPE_KEYWORDS = {
    "nric": ["nric", "fin"],
    "email": ["email", "e-mail"],
    "phone": ["contact no", "phone", "mobile", "telephone", "hp"],
    "date": ["date", "dob", "d/m/y", "dmy"],
    "amount": ["amount", "earnings", "salary", "income", "$", "sum"],
}

DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%Y-%m-%d")


def nric_valid(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"[STFG]\d{7}[A-Z]", v):
        return False
    table = NRIC_LETTERS[v[0]]
    total = sum(int(d) * w for d, w in zip(v[1:8], NRIC_WEIGHTS))
    if v[0] in "TG":
        total += 4
    return table[total % 11] == v[8]


def infer_type(field_name: str, context: str = "") -> str | None:
    text = f"{field_name} {context}".lower()
    for ftype, words in TYPE_KEYWORDS.items():
        if any(w in text for w in words):
            return ftype
    return None


def check_value(ftype: str, value: str) -> str | None:
    """Returns a warning message, or None if the value looks fine."""
    v = value.strip()
    if not v:
        return None
    if ftype == "nric":
        if not nric_valid(v):
            return "does not look like a valid NRIC/FIN (checksum failed)"
    elif ftype == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            return "does not look like an email address"
    elif ftype == "phone":
        digits = re.sub(r"[^\d]", "", v.removeprefix("+65"))
        if not (7 <= len(digits) <= 12):
            return "does not look like a phone number"
    elif ftype == "date":
        if not any(_parses(v, f) for f in DATE_FORMATS):
            return "not a recognised date (try DD/MM/YYYY)"
    elif ftype == "amount":
        if not re.fullmatch(r"[$S]*\s*[\d,]+(\.\d+)?", v):
            return "does not look like an amount"
    return None


def _parses(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def check_all(fields: list[dict], values: dict[str, object]) -> list[dict]:
    """Validate proposed values against each field's inferred type."""
    by_id = {str(f["id"]): f for f in fields}
    issues = []
    for fid, value in values.items():
        field = by_id.get(str(fid))
        if field is None or not isinstance(value, str):
            continue
        ftype = infer_type(field["name"], field.get("context", ""))
        if ftype is None:
            continue
        msg = check_value(ftype, value)
        if msg:
            issues.append({"field_id": str(fid), "field_name": field["name"],
                           "type": ftype, "message": msg})
    return issues
