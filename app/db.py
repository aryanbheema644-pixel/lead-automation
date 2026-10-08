"""Tiny SQLite persistence layer. JSON blobs are stored as TEXT."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import DB_PATH

_lock = threading.Lock()

# Columns stored as JSON text.
_JSON_FIELDS = ("extracted", "queries", "candidates", "chosen", "screening", "pushed_row",
                "gtm_counted", "merged_from")


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
    "channel": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "linkedin": "TEXT",
    "li_optout": "INTEGER DEFAULT 0",
    "other_link": "TEXT",          # a non-LinkedIn profile link the lead provided
    "link_raw": "TEXT",            # the profile-link cell exactly as the lead typed it
    "origin": "TEXT",              # 'sheet' (pulled from the source sheet) | 'upload' (CSV)
    "gtm_counted": "TEXT",         # JSON: where this lead was +1'd in the GTM report
    "dup_of": "INTEGER",           # open "possible duplicate of lead #N" flag (Duplicates page)
    "dup_reason": "TEXT",          # why it was flagged / merged (same name, same email, …)
    "merged_into": "INTEGER",      # status 'duplicate': merged into lead #N
    "merged_from": "TEXT",         # JSON list of lead ids merged into this one
    "visa": "TEXT",
    "lead_date": "TEXT",
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
    "screening": "TEXT",
    "screened": "INTEGER DEFAULT 0",
    "owner": "TEXT",
    "pushed": "INTEGER DEFAULT 0",
    "pushed_row": "TEXT",          # JSON: the dest-sheet cell values we last wrote
    "sheet_row": "INTEGER",        # dest-sheet row number we last wrote to (a hint)
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
        # Small key/value table for round-robin counters etc.
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # Dedup store: keys of source rows we've already ingested (or baselined).
        conn.execute("CREATE TABLE IF NOT EXISTS seen_leads (k TEXT PRIMARY KEY)")


def seen_keys() -> set:
    """All keys of source rows already ingested (or baselined). Keys are per
    channel, so the same person arriving on another channel still comes in —
    and is then merged as a duplicate rather than silently dropped."""
    with _conn() as conn:
        return {r["k"] for r in conn.execute("SELECT k FROM seen_leads")}


def mark_seen(keys: list[str]) -> None:
    keys = [k for k in keys if k]
    if not keys:
        return
    with _lock, _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_leads (k) VALUES (?)", [(k,) for k in keys])


def count_seen() -> int:
    with _conn() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM seen_leads").fetchone()["c"])


def meta_get(k: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default


def meta_set(k: str, v: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def requeue_interrupted() -> int:
    """Leads left mid-pipeline by a restart/deploy go back to the queue."""
    with _lock, _conn() as conn:
        return conn.execute("UPDATE leads SET status = 'queued', stage = NULL "
                            "WHERE status = 'processing'").rowcount


def next_rr(name: str, n: int) -> int:
    """Return the next round-robin index for `name` (0..n-1) and advance it."""
    if n <= 0:
        return 0
    key = f"rr_{name}"
    with _lock, _conn() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        cur = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(cur + 1)))
        return cur % n


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for jf in _JSON_FIELDS:
        if d.get(jf):
            try:
                d[jf] = json.loads(d[jf])
            except (json.JSONDecodeError, TypeError):
                d[jf] = None
    return d


def insert_lead(name: str, company: str, email: str, phone: str, message: str,
                linkedin: str = "", li_optout: int = 0, other_link: str = "",
                link_raw: str = "", visa: str = "", lead_date: str = "", channel: str = "",
                origin: str = "") -> int:
    now = time.time()
    with _lock, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO leads (name, company, channel, email, phone, linkedin,
                                  li_optout, other_link, link_raw, visa, lead_date,
                                  message, origin, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (name, company, channel, email, phone, linkedin, li_optout, other_link,
             link_raw, visa, lead_date, message, origin, now, now),
        )
        return int(cur.lastrowid)


def analytics() -> dict:
    """Aggregate counts for the dashboard charts: by channel, status, fit tier,
    and by day (last 14). Cheap — the leads table only holds processed leads."""
    import datetime as _dt
    channels: dict = {}
    statuses: dict = {}
    tiers: dict = {}
    owners: dict = {}
    by_day: dict = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT channel, status, screening, owner, created_at FROM leads").fetchall()
    for r in rows:
        ch = r["channel"] or "Upload"
        channels[ch] = channels.get(ch, 0) + 1
        st = r["status"] or "?"
        statuses[st] = statuses.get(st, 0) + 1
        if r["owner"]:
            owners[r["owner"]] = owners.get(r["owner"], 0) + 1
        if r["screening"]:
            try:
                tier = (json.loads(r["screening"]) or {}).get("tier")
            except (json.JSONDecodeError, TypeError):
                tier = None
            if tier:
                tiers[tier] = tiers.get(tier, 0) + 1
        if r["created_at"]:
            d = _dt.datetime.utcfromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
            by_day[d] = by_day.get(d, 0) + 1
    return {"channels": channels, "statuses": statuses, "tiers": tiers,
            "owners": owners, "by_day": sorted(by_day.items())[-14:]}


def update_lead(lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    for jf in _JSON_FIELDS:
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


def delete_lead(lead_id: int) -> bool:
    """Delete one lead. Its dedup key stays in seen_leads, so a sheet pull
    won't re-ingest it."""
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,)).rowcount > 0


def clear_all() -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM leads")
