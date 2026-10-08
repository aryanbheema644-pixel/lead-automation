"""Weekly GTM report ("MQL Tracker" tab): +1 per lead sent to the Pipedrive sheet.

Runs right after every push (button or automation) — the report follows the
OUTFLOW, not the inflow. Each ACCEPTED lead that is in the destination sheet and
has a known channel (pulled from the source sheet, or a CSV upload with a
Channel column) adds 1 to
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

Week columns are set up the way the team does it: the first time a week is
written to, the previous week's formulas (Inbound - Total, the Total rows, the
=prev+7 date) are copied into that column's BLANK cells; and if a lead's date is
past the last week, a new week column is added after it the same way.
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
    if lead.get("status") != "accepted" or not lead.get("pushed"):
        return None                                   # only what's in the Pipedrive sheet
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
    """First run only: mark leads already in the Pipedrive sheet as counted (with
    no location), so turning this on doesn't suddenly add every past lead."""
    n = 0
    for l in db.list_leads("accepted"):
        if l.get("gtm_counted") is None and l.get("pushed"):
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

    # Add week columns for dates past the last week (rare: the sheet already runs
    # far ahead), then make sure every week we write to has its formulas.
    need = {wk for _, _, deltas in changes for wk, _, d in deltas if d > 0}
    for wk in sorted(need):
        guard = 0
        while week_col(wk) is None and weeks and dt.date.fromisoformat(wk) >= weeks[-1][0] and guard < 60:
            _add_week_column(ws, values, formulas, weeks)
            guard += 1
    prepared = {week_col(wk) for wk in need} - {None}
    for c in sorted(prepared):
        src = _template_col(formulas, weeks, c)
        if src is not None:
            _copy_formulas(ws, values, formulas, src, c)

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


def _copy_paste(ws, src_col: int, dst_col: int, rows: list[int] | None, paste_type: str) -> list:
    """copyPaste requests from one column to another (0-based). rows=None -> whole column.
    Sheets shifts relative references, exactly like copying a column by hand."""
    def rng(col, r0=None, r1=None):
        g = {"sheetId": ws.id, "startColumnIndex": col, "endColumnIndex": col + 1}
        if r0 is not None:
            g.update(startRowIndex=r0, endRowIndex=r1)
        return g
    if rows is None:
        return [{"copyPaste": {"source": rng(src_col), "destination": rng(dst_col), "pasteType": paste_type}}]
    return [{"copyPaste": {"source": rng(src_col, r, r + 1), "destination": rng(dst_col, r, r + 1),
                           "pasteType": paste_type}} for r in rows]


def _template_col(formulas, weeks, before: int) -> int | None:
    """The most recent fully set-up week before `before`: the latest week column
    with formulas beyond the row-1 date (future weeks only have the date)."""
    for _, j in sorted(weeks, key=lambda w: w[1], reverse=True):
        if j < before and any(str(fr[j] if j < len(fr) else "").startswith("=")
                              for fr in formulas[1:]):
            return j
    return None


def _copy_formulas(ws, values, formulas, src: int, dst: int) -> int:
    """Copy src week's formulas into dst's BLANK cells (never overwrite)."""
    rows = []
    for i, fr in enumerate(formulas):
        f = fr[src] if src < len(fr) else ""
        cur = (fr[dst] if dst < len(fr) else "")
        if str(f).startswith("=") and str(cur) == "":
            rows.append(i)
    if rows:
        ws.spreadsheet.batch_update({"requests": _copy_paste(ws, src, dst, rows, "PASTE_FORMULA")})
        for i in rows:                                 # keep the local view in sync
            for m in (formulas, values):
                while len(m[i]) <= dst:
                    m[i].append("")
            formulas[i][dst] = "=(copied)"
            values[i][dst] = ""
    return len(rows)


def _add_week_column(ws, values, formulas, weeks) -> None:
    """Add the next week column right after the last one: same formatting, and
    the last week's formulas (incl. the =prev+7 date in row 1)."""
    last_date, last = weeks[-1]
    new = last + 1
    if new >= ws.col_count:
        ws.add_cols(new - ws.col_count + 1)
    ws.spreadsheet.batch_update({"requests": _copy_paste(ws, last, new, None, "PASTE_FORMAT")})
    _copy_formulas(ws, values, formulas, last, new)              # =prev+7 date
    src = _template_col(formulas, weeks, new)
    if src is not None:
        _copy_formulas(ws, values, formulas, src, new)           # Totals etc.
    weeks.append((last_date + dt.timedelta(days=7), new))
