"""LLM semantic field mapping (M2) and set-of-marks choosing (M3).

Provider-agnostic contract: the rest of the app calls map_fields() /
map_markers() and gets plain dicts back. Currently backed by Gemini; to swap
in Claude, reimplement generate() only.
"""

import base64
import json
import os
import time

import httpx

from app.redact import Redactor

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
RETRIES = 3


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def generate(prompt: str, image_png: bytes | None = None) -> list | dict:
    """One JSON-mode LLM call, with retry on transient errors.

    Raises RuntimeError with the key redacted from any message.
    """
    parts = []
    if image_png is not None:
        parts.append({"inline_data": {
            "mime_type": "image/png",
            "data": base64.b64encode(image_png).decode()}})
    parts.append({"text": prompt})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0,
                             "response_mime_type": "application/json"},
    }
    last = None
    for attempt in range(RETRIES):
        try:
            resp = httpx.post(GEMINI_URL, params={"key": api_key()},
                              json=body, timeout=120)
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"LLM call failed: HTTP {e.response.status_code}")
        except (httpx.TransportError, KeyError, json.JSONDecodeError) as e:
            last = type(e).__name__
            time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed after {RETRIES} attempts ({last})")


FIELDS_PROMPT = """\
You are mapping a person's profile data onto a PDF form's fields.

The form is a Financial Assistance / insurance claim form. The profile belongs
to THE APPLICANT (the insured person / claimant). Rules:

1. Only map a profile value to a field if that field asks for the APPLICANT's
   own information. Fields about other people (doctor, witness, employer
   contact, referral, agent) or for office use must NOT be filled.
2. Use each field's name AND its nearby label text to decide what it asks for.
3. Only include mappings you are confident about. Omit uncertain fields.
4. Values must be copied verbatim from the profile (no reformatting).
5. Forms often ask for the same information in several places (e.g. "Name of
   Insured" on page 1 AND "Name of Claimant" AND "Name of Patient" later).
   Map the value to EVERY field that asks for it — do not stop at the first.

PROFILE (key: value):
{profile}

FORM FIELDS (id | field name | nearby label text):
{fields}

Return a JSON array of objects: {{"field_id": <int>, "profile_key": <string>,
"confidence": <0.0-1.0>}}. No other keys, no commentary.
"""


def map_fields(fields: list[dict], profile: dict[str, str]) -> dict:
    """Tier 1: fields are AcroForm widgets with real ids.

    Profile values are redacted before sending — the model only returns
    profile KEYS, and real values are substituted locally afterwards.
    """
    red = Redactor()
    field_lines = "\n".join(
        f"{f['id']} | {f['name']} | {f.get('context', '')}" for f in fields
    )
    profile_lines = "\n".join(f"{k}: {red.redact(v)}"
                              for k, v in profile.items())
    mappings = generate(FIELDS_PROMPT.format(profile=profile_lines,
                                             fields=field_lines))
    valid_ids = {f["id"] for f in fields}
    suggestions = {}
    for m in mappings:
        fid, key = m.get("field_id"), m.get("profile_key")
        if fid in valid_ids and key in profile:
            suggestions[str(fid)] = {
                "value": profile[key],
                "source_key": key,
                "confidence": round(float(m.get("confidence", 0.5)), 2),
            }
    return suggestions


MARKERS_PROMPT = """\
This form page image has red numbered markers drawn on every candidate answer
area (each number badge sits at the top-left of its box). The markers, with
the printed label text found in and around each box:

{listing}

A person's profile data must be written into the right boxes. The profile
belongs to THE APPLICANT (the insured / claimant). Do not map anything into
areas meant for other people (doctor, witness, agent) or for office use.
Values get handwritten into blank space: strongly prefer markers tagged
EMPTY. A marker tagged "has printed text" is usually the label itself —
the correct box is typically the EMPTY one to its right or below.

PROFILE (key: value):
{profile}

Forms often ask for the same information in several places (e.g. the
claimant's name on page 1 and again in a later section) — map the value to
EVERY box that asks for it, not just the first.

For each profile value that clearly belongs in one of the marked boxes,
return: {{"marker_id": <int>, "profile_key": <string>,
"confidence": <0.0-1.0>}} as a JSON array. Only confident mappings; a marker
may appear at most once (but one profile_key may map to several markers).
"""


EXTRACT_PROMPT = """\
You are an intake assistant for a financial adviser handling an insurance /
financial-assistance claim. The adviser sends free-form messages about the
client and the claim (notes, forwarded client texts). Extract every concrete
fact into flat snake_case keys with verbatim values.

Preferred canonical keys (use these when applicable, invent sensible
snake_case keys for anything else): name, nric, policy_no, address, phone,
email, date_of_birth, occupation, employer, accident_date, accident_time,
accident_description, injuries, hospital, doctor, bank, account_no.

Rules:
- Extract only facts actually stated. Never guess or fabricate.
- Values verbatim (keep the adviser's formatting for dates/numbers).
- If the message corrects an earlier fact, output the new value.

ALREADY COLLECTED (do not repeat unless corrected):
{known}

FORM FIELDS this claim must eventually fill (may be empty if no form yet):
{form_fields}

NEW MESSAGE FROM THE ADVISER:
{message}

Return JSON: {{"extracted": {{<key>: <value>, ...}},
"missing": [<up to 8 important details still not collected, as short
human-readable labels, prioritising what the form needs>]}}
"""


def extract_details(message: str, known: dict[str, str],
                    form_fields: list[str] | None) -> dict:
    """Free-text intake: extract new facts + list what's still missing.

    NRIC/email/phone/account numbers are redacted to placeholders before
    the LLM sees the message, and restored locally in the extracted values.
    """
    red = Redactor()
    message = red.redact(message)
    known_lines = "\n".join(f"{k}: {red.redact(v)}"
                            for k, v in known.items()) or "(none)"
    fields = "\n".join(form_fields or []) or "(no form uploaded yet)"
    result = generate(EXTRACT_PROMPT.format(
        known=known_lines, form_fields=fields, message=message))
    extracted = result.get("extracted", {}) if isinstance(result, dict) else {}
    missing = result.get("missing", []) if isinstance(result, dict) else []
    clean = {str(k).strip(): red.restore(str(v).strip())
             for k, v in extracted.items() if str(v).strip()}
    return {"extracted": clean, "missing": [str(m) for m in missing][:8]}


def map_markers(image_png: bytes, candidates: list[dict],
                profile: dict[str, str]) -> list[dict]:
    """Tier 2/3: set-of-marks. The LLM only picks marker ids."""
    red = Redactor()
    listing = "\n".join(
        f"{c['id']}: {'EMPTY' if c.get('empty') else 'has printed text'}"
        f" — near \"{c['context']}\"" for c in candidates)
    profile_lines = "\n".join(f"{k}: {red.redact(v)}"
                              for k, v in profile.items())
    mappings = generate(MARKERS_PROMPT.format(listing=listing,
                                              profile=profile_lines),
                        image_png)
    valid = {c["id"] for c in candidates}
    out, used = [], set()
    for m in mappings:
        mid, key = m.get("marker_id"), m.get("profile_key")
        if mid in valid and key in profile and mid not in used:
            used.add(mid)
            out.append({"marker_id": mid, "profile_key": key,
                        "confidence": round(float(m.get("confidence", 0.5)), 2)})
    return out
