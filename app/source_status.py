"""Mark distributed leads "In Progress" in the source sheet.

The team keeps a "Lead Status" column on the Website Leads and Meta Leads tabs:
a lead that went to an AE gets "In Progress". Once a lead is accepted and in the
Pipedrive sheet (i.e. distributed), its row in its own source tab is marked.

A lead's row is the one it was read from: same email (else phone), closest to
the lead's date, over every lead tab (a merged lead credited to Meta can have
been read from a Website or WA row); with several, the first one. A cell that already holds
anything is never overwritten. Tabs without a "Lead Status" column (the
WhatsApp chatbot tab) are skipped. Each lead is handled once; a lead whose row
can't be found is retried a few times, then left alone.
"""
from __future__ import annotations

import datetime as dt
import json

from gspread.utils import rowcol_to_a1

from . import config, db, sheets
from .gtm import _digits, _hkey, _parse_date, _swapped

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
    # "ok" = handled by the row choice below. An older "done" gets one more look,
    # so leads whose row was looked for on the wrong tab are marked now.
    pending = [l for l in db.list_leads("accepted")
               if l.get("pushed") and l.get("origin") == "sheet" and state.get(str(l["id"])) != "ok"]
    if not pending:
        return out
    sh = sheets._client().open_by_key(config.GOOGLE_SOURCE_SHEET_ID)
    titles = [t.strip() for t in (config.GOOGLE_SOURCE_TAB or "").split(",") if t.strip()] \
        or [ws.title for ws in sh.worksheets()]
    tabs = []
    for title in titles:
        ch = channel_of(title)
        if ch == title:
            continue                                   # not a lead tab, e.g. "[Import] SQL"
        ws = sh.worksheet(title)
        values = ws.get_all_values()
        header = [_hkey(h) for h in (values[0] if values else [])]
        col = lambda names, _h=header: next((_h.index(n) for n in names if n in _h), None)
        tabs.append({"ws": ws, "values": values, "channel": ch, "sc": col(("lead status",)),
                     "dc": col(("date of lead", "date od lead", "timestamp")),
                     "ec": col(("email",)), "pc": col(("number",)), "cells": []})

    for l in pending:
        ch = l.get("channel") or ""
        # The row it was read from: the closest date over every lead tab (ties:
        # its channel's tab). A merged lead is credited to the higher channel but
        # keeps the row it was read from, which can be on another tab.
        hit = None
        for t in tabs:
            if t["ec"] is None and t["pc"] is None:
                continue
            g = _find_row_gap(t["values"], l, t["dc"], t["ec"], t["pc"])
            if g and (hit is None or (g[0], t["channel"] != ch) < (hit[2], hit[0]["channel"] != ch)):
                hit = (t, g[1], g[0])
        if hit is None:
            tries = (state.get(str(l["id"])) if isinstance(state.get(str(l["id"])), int) else 0) + 1
            state[str(l["id"])] = "ok" if tries >= _TRIES else tries
            out["skipped"] += 1
            continue
        t, row, _ = hit
        sc = t["sc"]
        if sc is not None:                             # the WhatsApp tab has no status column
            cur = t["values"][row][sc] if sc < len(t["values"][row]) else ""
            if not cur.strip():
                t["cells"].append({"range": rowcol_to_a1(row + 1, sc + 1), "values": [[STATUS]]})
                t["values"][row] += [""] * (sc + 1 - len(t["values"][row]))
                t["values"][row][sc] = STATUS
        state[str(l["id"])] = "ok"
    for t in tabs:
        if t["cells"]:
            t["ws"].batch_update(t["cells"], value_input_option="USER_ENTERED")
            out["marked"] += len(t["cells"])
    db.meta_set("src_marked", json.dumps(state))
    return out


def _find_row(values: list[list[str]], lead: dict, dc, ec, pc) -> int | None:
    """0-based index of the lead's row: same email or phone, on the lead's date
    (else the closest row within 3 days of it)."""
    best = _find_row_gap(values, lead, dc, ec, pc)
    return best[1] if best else None


def _find_row_gap(values: list[list[str]], lead: dict, dc, ec, pc) -> tuple[int, int] | None:
    """Like _find_row, but (days off the lead's date, index), to compare tabs."""
    email = (lead.get("email") or "").strip().lower()
    phone = _digits(lead.get("phone"))[-9:]
    undated = not (lead.get("lead_date") or "").strip()
    day = _parse_date(lead.get("lead_date") or "") or \
        dt.datetime.utcfromtimestamp(lead.get("created_at") or 0).date()
    best = None
    for i, r in enumerate(values[1:], start=1):
        cell = lambda c: (r[c].strip() if c is not None and c < len(r) else "")
        if not ((email and cell(ec).lower() == email) or
                (len(phone) >= 7 and _digits(cell(pc))[-9:] == phone)):
            continue
        d = _parse_date(cell(dc)) if dc is not None else None
        gap = min((abs((x - day).days) for x in (d, d and _swapped(d)) if x), default=99)
        if undated and not cell(dc):
            gap = 3                                   # a lead read from a row with no date: that row
        if gap <= 3 and (best is None or gap < best[0]):
            best = (gap, i)                           # the first row on the closest day
    return best
