"""SQLite persistence: saved clients, form history, audit trail.

- clients: the FA's saved client profiles (the point is reuse — this is
  deliberately persisted personal data; see docs/production-notes.md §5)
- forms: metadata history of processed forms (no field values stored)
- audit: what was mapped where, by which engine, at what confidence
  (keys and confidences only — never the values themselves)
"""

import json
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "form-nation.db"
KEY_PATH = DATA_DIR / ".secret.key"


def _fernet() -> Fernet:
    DATA_DIR.mkdir(exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
        KEY_PATH.chmod(0o600)
    return Fernet(KEY_PATH.read_bytes())


def _encrypt(profile: dict) -> str:
    return _fernet().encrypt(json.dumps(profile).encode()).decode()


def _decrypt(blob: str) -> dict:
    if blob.startswith("{"):        # legacy plaintext row
        return json.loads(blob)
    try:
        return json.loads(_fernet().decrypt(blob.encode()))
    except InvalidToken:
        return {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    profile_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS forms (
    doc_id TEXT PRIMARY KEY,
    filename TEXT,
    tier INTEGER,
    pages INTEGER,
    fields INTEGER,
    status TEXT DEFAULT 'uploaded',
    source TEXT DEFAULT 'web',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    filename TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT,
    created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---- clients ----

def list_clients() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, name, profile_json FROM clients ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"],
             "profile": _decrypt(r["profile_json"])} for r in rows]


def get_client(client_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT id, name, profile_json FROM clients WHERE id=?",
                      (client_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"],
            "profile": _decrypt(r["profile_json"])}


def save_client(name: str, profile: dict) -> int:
    now = time.time()
    with connect() as c:
        c.execute(
            "INSERT INTO clients (name, profile_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET profile_json=excluded.profile_json,"
            " updated_at=excluded.updated_at",
            (name.strip(), _encrypt(profile), now, now))
        row = c.execute("SELECT id FROM clients WHERE name=?",
                        (name.strip(),)).fetchone()
    return row["id"]


def delete_client(client_id: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM clients WHERE id=?", (client_id,))
    return cur.rowcount > 0


# ---- claim types (registered blank-form templates) ----

def list_types() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, name, path, filename FROM claim_types"
            " ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_type(type_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT id, name, path, filename FROM claim_types"
                      " WHERE id=?", (type_id,)).fetchone()
    return dict(r) if r else None


def save_type(name: str, path: str, filename: str) -> int:
    now = time.time()
    with connect() as c:
        c.execute(
            "INSERT INTO claim_types (name, path, filename, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET path=excluded.path,"
            " filename=excluded.filename",
            (name.strip(), path, filename, now))
        row = c.execute("SELECT id FROM claim_types WHERE name=?",
                        (name.strip(),)).fetchone()
    return row["id"]


def delete_type(type_id: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM claim_types WHERE id=?", (type_id,))
    return cur.rowcount > 0


# ---- forms & audit ----

def record_form(doc_id: str, filename: str, tier: int, pages: int,
                fields: int, source: str = "web"):
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO forms"
            " (doc_id, filename, tier, pages, fields, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, filename, tier, pages, fields, source, time.time()))


def set_form_status(doc_id: str, status: str):
    with connect() as c:
        c.execute("UPDATE forms SET status=? WHERE doc_id=?", (status, doc_id))


def log_event(doc_id: str, event: str, detail: dict | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO audit (doc_id, event, detail_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (doc_id, event, json.dumps(detail or {}), time.time()))


def form_history(limit: int = 50) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM forms ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
