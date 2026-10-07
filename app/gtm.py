"""Weekly GTM report ("MQL Tracker" tab): +1 per accepted lead.

Each ACCEPTED lead pulled from the source sheet adds 1 to
  - its channel row   (Inbound - Meta / - Website (Whatsapp) / - Website (SEO + AEO))
  - its AE's SQL row  (Rahul - SQL, Vansh - SQL, …)
in the column of the week its "Date of lead" falls in (row 1 holds each week's
start date). The team also enters numbers by hand, so this only ever ADDS to
what's in a cell — it never recomputes or overwrites a week.

Every lead remembers exactly where it was counted (leads.gtm_counted), which
makes the sync idempotent and reversible:
  - counted once, never twice (retries, restarts, redeploys)
  - accepted -> rejected later: its +1s are taken back (-1)
  - AE changed after counting: -1 on the old AE's row, +1 on the new one
Rows are found by their label in column B, so inserted rows don't break it, and
formula cells are never written.
"""
from __future__ import annotations

import datetime as dt
import re

from gspread.utils import rowcol_to_a1

from . import config, db, sheets

_DATE_FORMATS = ("%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%m/%d/%y", "%d %b %Y", "%b %d, %Y")


def _parse_date(text: str) -> dt.date | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.split("T")[0].split(" ")[0] if re.match(r"\d{4}-\d{2}-\d{2}", t) else t
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)          # "09/30/2026 5:05:51"
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def lead_day(lead: dict) -> dt.date:
    """The lead's 'Date of lead', else the day LeadSearch picked it up."""
    d = _parse_date(lead.get("lead_date", ""))
    if d:
        return d
    return dt.datetime.utcfromtimestamp(lead.get("created_at") or 0).date()


def _desired(lead: dict) -> dict | None:
    """Where this lead should be counted right now, or None if it shouldn't."""
    if lead.get("status") != "accepted" or lead.get("origin") != "sheet":
        return None
    channel_row = config.GTM_CHANNEL_ROWS.get(lead.get("channel") or "")
    if not channel_row:
        return None
    owner = (lead.get("owner") or "").split()
    return {
        "week": lead_day(lead).isoformat(),
        "channel_row": channel_row,
        "sql_row": config.GTM_SQL_ROW.format(ae=owner[0]) if owner else "",
    }


def _num(v: str) -> float:
    v = (v or "").replace(",", "").strip()
    try:
        return float(v)
    except ValueError:
        return 0.0


def _fmt(n: float) -> int | float:
    return int(n) if float(n).is_integer() else n


def baseline_existing() -> int:
    """First run only: mark leads that are already accepted as counted (with no
    location), so turning this on doesn't suddenly add every past lead."""
    n = 0
    for l in db.list_leads("accepted"):
        if l.get("gtm_counted") is None:
            db.update_lead(l["id"], gtm_counted={"baseline": True})
            n += 1
    return n


def sync_report() -> dict:
    """Apply pending +1/-1s to the GTM report. Returns a summary for the UI."""
    out = {"added": 0, "removed": 0, "skipped": []}
    if not config.GTM_SHEET_ID:
        return out
    if db.meta_get("gtm_started") != "1":
        out["baselined"] = baseline_existing()
        db.meta_set("gtm_started", "1")

    # Work out every lead's change: (lead, new_counted, [(week, row_label, delta)]).
    changes = []
    for l in db.list_leads():
        old = l.get("gtm_counted")
        if isinstance(old, dict) and old.get("baseline"):
            continue                                  # existed before go-live
        new = _desired(l)
        old = old if isinstance(old, dict) and old.get("week") else None
        if old == new:
            continue
        deltas = []
        if old:
            deltas += [(old["week"], old["channel_row"], -1)]
            if old.get("sql_row"):
                deltas += [(old["week"], old["sql_row"], -1)]
        if new:
            deltas += [(new["week"], new["channel_row"], +1)]
            if new.get("sql_row"):
                deltas += [(new["week"], new["sql_row"], +1)]
        changes.append((l, new, deltas))
    if not changes:
        return out

    ws = sheets._client().open_by_key(config.GTM_SHEET_ID).worksheet(config.GTM_TAB)
    values = ws.get_all_values()
    formulas = ws.get_all_values(value_render_option="FORMULA")

    # Row 1 = week start dates; column B = row labels.
    weeks = []
    for j, h in enumerate(values[0] if values else []):
        d = _parse_date(h)
        if d:
            weeks.append((d, j))
    rows = {}
    for i, r in enumerate(values):
        label = " ".join((r[1] if len(r) > 1 else "").split()).lower()
        if label and label not in rows:
            rows[label] = i

    def week_col(iso: str) -> int | None:
        d = dt.date.fromisoformat(iso)
        cols = [j for start, j in weeks if start <= d < start + dt.timedelta(days=7)]
        return cols[-1] if cols else None

    totals: dict[tuple[int, int], int] = {}
    applied = []
    for l, new, deltas in changes:
        cells, problem = [], ""
        for week, label, delta in deltas:
            r, c = rows.get(" ".join(label.split()).lower()), week_col(week)
            if r is None:
                problem = f"no row '{label}'"
            elif c is None:
                problem = f"no week column for {week}"
            elif str(formulas[r][c] if c < len(formulas[r]) else "").startswith("="):
                problem = f"{rowcol_to_a1(r + 1, c + 1)} is a formula"
            else:
                cells.append(((r, c), delta))
        if problem:
            out["skipped"].append(f"{l.get('name') or 'Lead #' + str(l['id'])}: {problem}")
            continue
        for key, delta in cells:
            totals[key] = totals.get(key, 0) + delta
        applied.append((l, new, deltas))

    writes = []
    for (r, c), delta in totals.items():
        if delta:
            cur = values[r][c] if c < len(values[r]) else ""
            writes.append((r + 1, c + 1, _fmt(max(0.0, _num(cur) + delta))))
    if writes:
        sheets.write_cells(ws, writes)
    for l, new, deltas in applied:
        db.update_lead(l["id"], gtm_counted=new or {})
        out["added"] += sum(1 for *_, d in deltas if d > 0)
        out["removed"] += sum(1 for *_, d in deltas if d < 0)
    return out
