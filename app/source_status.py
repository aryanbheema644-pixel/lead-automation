"""Mark distributed leads "In Progress" in the source sheet.

The team keeps a "Lead Status" column on the Website Leads and Meta Leads tabs:
a lead that went to an AE gets "In Progress". Once a lead is accepted and in the
Pipedrive sheet (i.e. distributed), its row in its own source tab is marked.

A lead's row is found by email (else phone) on the lead's date; with several
(the same person twice that day) the first one. A cell that already holds
anything is never overwritten. Tabs without a "Lead Status" column (the
WhatsApp chatbot tab) are skipped. Each lead is handled once; a lead whose row
can't be found is retried a few times, then left alone.
"""
from __future__ import annotations

import datetime as dt
import json

from gspread.utils import rowcol_to_a1

from . import config, db, sheets
from .gtm import _digits, _hkey, _parse_date

STATUS = "In Progress"
_TRIES = 6                       # syncs to keep looking for a row that isn't there yet


def _state() -> dict:
    try:
        return json.loads(db.meta_get("src_marked", "{}") or "{}")
    except ValueError:
        return {}


def mark_distributed(channel_of) -> dict:
    """Mark every newly distributed lead's source row. Returns counts for the UI."""
    out = {"marked": 0, "skipped": 0}
    if not config.GOOGLE_SOURCE_SHEET_ID:
        return out
    state = _state()
    pending = [l for l in db.list_leads("accepted")
               if l.get("pushed") and l.get("origin") == "sheet" and state.get(str(l["id"])) != "done"]
    if not pending:
        return out
    sh = sheets._client().open_by_key(config.GOOGLE_SOURCE_SHEET_ID)
    tabs = [t.strip() for t in (config.GOOGLE_SOURCE_TAB or "").split(",") if t.strip()] \
        or [ws.title for ws in sh.worksheets()]
    by_channel = {}
    for title in tabs:
        by_channel.setdefault(channel_of(title), title)

    for channel in {l.get("channel") or "" for l in pending}:
        leads = [l for l in pending if (l.get("channel") or "") == channel]
        title = by_channel.get(channel)
        if not title:
            for l in leads:
                state[str(l["id"])] = "done"          # not from a tab we read
            continue
        ws = sh.worksheet(title)
        values = ws.get_all_values()
        header = [_hkey(h) for h in (values[0] if values else [])]
        if "lead status" not in header:
            for l in leads:
                state[str(l["id"])] = "done"          # e.g. the WhatsApp tab: nothing to mark
            continue
        sc = header.index("lead status")
        dc = next((header.index(n) for n in ("date of lead", "date od lead", "timestamp") if n in header), None)
        ec = header.index("email") if "email" in header else None
        pc = header.index("number") if "number" in header else None
        cells = []
        for l in leads:
            row = _find_row(values, l, dc, ec, pc)
            if row is None:
                tries = state.get(str(l["id"]), 0) + 1
                state[str(l["id"])] = "done" if tries >= _TRIES else tries
                out["skipped"] += 1
                continue
            cur = values[row][sc] if sc < len(values[row]) else ""
            if not cur.strip():
                cells.append({"range": rowcol_to_a1(row + 1, sc + 1), "values": [[STATUS]]})
            state[str(l["id"])] = "done"
        if cells:
            ws.batch_update(cells, value_input_option="USER_ENTERED")
            out["marked"] += len(cells)
    db.meta_set("src_marked", json.dumps(state))
    return out


def _find_row(values: list[list[str]], lead: dict, dc, ec, pc) -> int | None:
    """0-based index of the lead's row: same email or phone, on the lead's date
    (else the closest row within 3 days of it)."""
    email = (lead.get("email") or "").strip().lower()
    phone = _digits(lead.get("phone"))[-9:]
    day = _parse_date(lead.get("lead_date") or "") or \
        dt.datetime.utcfromtimestamp(lead.get("created_at") or 0).date()
    best = None
    for i, r in enumerate(values[1:], start=1):
        cell = lambda c: (r[c].strip() if c is not None and c < len(r) else "")
        if not ((email and cell(ec).lower() == email) or
                (len(phone) >= 7 and _digits(cell(pc))[-9:] == phone)):
            continue
        d = _parse_date(cell(dc)) if dc is not None else None
        gap = abs((d - day).days) if d else 99
        if gap <= 3 and (best is None or gap < best[0]):
            best = (gap, i)                           # the first row on the closest day
    return best[1] if best else None
