"""Write the cleaned values of the Pipedrive sheet back into the source sheet.

Whatever LeadSearch puts in the Pipedrive sheet for a lead (the verified
LinkedIn link, the name, the visa, company, designation, the AE it went to)
also replaces the old value on that lead's row in its own source tab, so the
lead sheet holds the clean data too.

- Only leads already in the Pipedrive sheet, and only columns the tab has
  (matched by header, e.g. "Linkedin Profile URL" / "Linkedin Id").
- Email and phone are never written: they're how rows are found and deduped.
- A cell is written the first time when it differs from the clean value, and
  later only when the clean value changes, so a hand edit made after that
  stays until LeadSearch's value for that field changes again.
- A name is only replaced by a fuller version of itself (see _better_name),
  and only on rows that have an email or phone.
- Every write is logged with the old value (meta "src_wb_log") to undo it.
"""
from __future__ import annotations

import json
import re
import time

from gspread.utils import rowcol_to_a1

from . import config, db, sheets
from .gtm import _hkey
from .source_status import _find_row

# Pipedrive-sheet field -> header(s) it goes under in a source tab.
_COLUMNS = {
    "name": ("name",),
    "linkedin profile url": ("linkedin profile url", "linkedin id", "linkedin profile"),
    "visa intrested in": ("visa intrested in", "visa interested in", "visa type"),
    "company": ("company name",),
    "designation": ("designation",),
    "owner name": ("distributed to",),
}
_SKIP_VALUES = {"visa intrested in": {"AE decide"}}


def _state() -> dict:
    try:
        return json.loads(db.meta_get("src_wb", "{}") or "{}")
    except ValueError:
        return {}


_TITLES = {"dr", "mr", "mrs", "ms", "md", "phd", "prof", "jr", "sr"}


def _name_toks(name: str) -> list[str]:
    toks = re.sub(r"[^\w\s]", " ", name.lower()).split()
    return [t for t in toks if t not in _TITLES]


def _clean_name(name: str) -> str:
    """A LinkedIn name without its extras: 'Joel Schuurman, CPA' -> 'Joel Schuurman',
    'Yuxin Yang — Sensors…' -> 'Yuxin Yang', 'KK (Mechatronics)' -> 'KK';
    ALL CAPS -> Title Case."""
    name = re.split(r"\s[—–|-]\s|,|\s\(", name)[0].strip()
    return name.title() if name.isupper() else name


def _better_name(new: str, old: str) -> bool:
    """Only a fuller version of the same name replaces it: every word of the old
    name is in the new one, and the new one has more ('Luis Salinas, MD' ->
    'Luis Eduardo Salinas Ruiz'). Never 'Kenneth Munachi' -> 'Kenneth', never an
    initial ('Thomas Z.'), never just a title added ('Dr.Ranjith Gopalan')."""
    n, o = _name_toks(new), _name_toks(old)
    if not o:
        return bool(n)
    if any(len(t) == 1 for t in n):
        return False
    return set(o) <= set(n) and len(n) > len(o)


def sync(channel_of, fields_of, live: bool = True) -> dict:
    """Write back every pushed lead's changed values. live=False only plans.
    Returns {"written": n, "rows": n, "plan": [...], "not_found": n}."""
    out = {"written": 0, "rows": 0, "not_found": 0, "plan": []}
    if not config.GOOGLE_SOURCE_SHEET_ID:
        return out
    state = _state()
    leads = [l for l in db.list_leads() if l.get("pushed") and l.get("origin") == "sheet"
             and l.get("status") == "accepted"]
    if not leads:
        return out
    sh = sheets._client().open_by_key(config.GOOGLE_SOURCE_SHEET_ID)
    tabs = [t.strip() for t in (config.GOOGLE_SOURCE_TAB or "").split(",") if t.strip()] \
        or [ws.title for ws in sh.worksheets()]
    by_channel = {}
    for title in tabs:
        by_channel.setdefault(channel_of(title), title)
    log = []
    for channel in {l.get("channel") or "" for l in leads}:
        title = by_channel.get(channel)
        if not title:
            continue
        ws = sh.worksheet(title)
        values = ws.get_all_values()
        if not values:
            continue
        header = [_hkey(h) for h in values[0]]
        col = lambda names: next((header.index(n) for n in names if n in header), None)
        dc = col(("date of lead", "date od lead", "timestamp"))
        ec, pc = col(("email",)), col(("number",))
        targets = {f: c for f, names in _COLUMNS.items() if (c := col(names)) is not None}
        cells = []
        for l in (l for l in leads if (l.get("channel") or "") == channel):
            row = _find_row(values, l, dc, ec, pc)
            if row is None:
                out["not_found"] += 1
                continue
            f = fields_of(l)
            last = state.get(str(l["id"]), {})
            changed = []
            for field, c in targets.items():
                new = f.get(field, "")
                if field == "name":
                    new = _clean_name(new)
                cur = values[row][c].strip() if c < len(values[row]) else ""
                if field in last and last[field] == new:
                    continue                         # already written; a later hand edit stays
                if new in _SKIP_VALUES.get(field, ()) or (not new and field != "linkedin profile url"):
                    continue                         # nothing clean to write (a cleared link is)
                if field == "name" and (not _better_name(new, cur) or not (l.get("email") or l.get("phone"))):
                    continue
                if new == cur:
                    last[field] = new
                    continue
                cells.append({"range": rowcol_to_a1(row + 1, c + 1), "values": [[new]]})
                changed.append({"field": field, "old": cur, "new": new})
                last[field] = new
            if changed:
                out["rows"] += 1
                out["plan"].append({"lead": l["id"], "name": f.get("name"), "tab": title,
                                    "row": row + 1, "changes": changed})
                log += [{"at": time.time(), "lead": l["id"], "tab": title, "row": row + 1, **ch} for ch in changed]
            state[str(l["id"])] = last
        if cells and live:
            ws.batch_update(cells, value_input_option="RAW")
        out["written"] += len(cells)
    if live:
        db.meta_set("src_wb", json.dumps(state))
        if log:
            try:
                old = json.loads(db.meta_get("src_wb_log", "[]") or "[]")
            except ValueError:
                old = []
            db.meta_set("src_wb_log", json.dumps((log + old)[:5000]))
    return out
