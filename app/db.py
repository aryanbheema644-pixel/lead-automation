"""Tiny SQLite persistence layer. JSON blobs are stored as TEXT."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import DB_PATH

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


# Expected columns -> SQL type. Used to create and to migrate older DBs.
_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT",
    "company": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "linkedin": "TEXT",
    "li_optout": "INTEGER DEFAULT 0",
    "message": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'queued'",
    "stage": "TEXT",
    "extracted": "TEXT",
    "queries": "TEXT",
    "candidates": "TEXT",
    "chosen": "TEXT",
    "confidence": "REAL",
    "reasoning": "TEXT",
    "error": "TEXT",
    "pushed": "INTEGER DEFAULT 0",
    "created_at": "REAL",
    "updated_at": "REAL",
}


def init_db() -> None:
    cols_sql = ",\n                ".join(f"{c} {t}" for c, t in _COLUMNS.items())
    with _conn() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS leads (\n                {cols_sql}\n            )")
        # Migrate older databases: add any columns introduced after they were made.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
        for col, decl in _COLUMNS.items():
            if col not in existing:
                # ALTER can't add PRIMARY KEY / NOT NULL-without-default; strip those.
                simple = decl.replace("PRIMARY KEY AUTOINCREMENT", "") \
                             .replace("NOT NULL", "").strip()
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {simple}")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for jf in ("extracted", "queries", "candidates", "chosen"):
        if d.get(jf):
            try:
                d[jf] = json.loads(d[jf])
            except (json.JSONDecodeError, TypeError):
                d[jf] = None
    return d


def insert_lead(name: str, company: str, email: str, phone: str, message: str,
                linkedin: str = "", li_optout: int = 0) -> int:
    now = time.time()
    with _lock, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO leads (name, company, email, phone, linkedin, li_optout,
                                  message, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (name, company, email, phone, linkedin, li_optout, message, now, now),
        )
        return int(cur.lastrowid)


def update_lead(lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    for jf in ("extracted", "queries", "candidates", "chosen"):
        if jf in fields and not isinstance(fields[jf], (str, type(None))):
            fields[jf] = json.dumps(fields[jf], ensure_ascii=False)
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [lead_id]
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", vals)


def get_lead(lead_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_leads(status: Optional[str] = None) -> list[dict]:
    with _conn() as conn:
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


def queued_lead_ids() -> list[int]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM leads WHERE status = 'queued' ORDER BY id ASC"
        ).fetchall()
        return [int(r["id"]) for r in rows]


def stats() -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM leads GROUP BY status"
        ).fetchall()
    out = {"total": 0, "queued": 0, "processing": 0, "accepted": 0,
           "review": 0, "rejected": 0, "error": 0}
    for r in rows:
        out[r["status"]] = r["c"]
        out["total"] += r["c"]
    return out


def clear_all() -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM leads")
