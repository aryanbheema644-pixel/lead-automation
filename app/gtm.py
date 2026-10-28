"""Weekly GTM report ("MQL Tracker" tab), recounted from the lead sheet.

Every automation cycle recounts the CURRENT and the PREVIOUS week from the
source sheet (Website / Meta / WhatsApp tabs) and overwrites those weeks' cells:

  - Channel rows: every lead that came in that week counts once, whether it was
    distributed or not, EXCEPT re-engagement leads:
      * a "Re-Engagement …" form, or
      * someone already in Pipedrive (contact made a day or more before this
        application) — they applied again.
    The same person applying twice in a week counts once (first application).
    Junk (test entries, keyboard-mash or bot names — see `_is_junk`) never counts.
    Meta tab -> Inbound - Meta, WhatsApp tab -> Inbound - Website (Whatsapp),
    Website tab -> by "Where have you heard about us" (GTM_WEBSITE_HEARD_ROWS),
    anything unidentified -> Inbound - Website (SEO + AEO).
  - AE rows ("Rahul - SQL", …): those counted leads that went to an AE — the
    LeadSearch owner of an accepted lead, else the owner of a Pipedrive deal
    made for them since they applied (leads distributed by hand).

The week = the lead's date in the source sheet. Formula cells (Inbound - Total,
Totals) are never written; a week written for the first time gets the previous
week's formulas copied into its blank cells, as the team does by hand.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

from gspread.utils import rowcol_to_a1

from . import config, db, sheets

_DATE_FORMATS = ("%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%m/%d/%y", "%d %b %Y", "%b %d, %Y",
                 "%m-%d-%Y")
_WEBSITE_ROW = "Inbound - Website (SEO + AEO)"
_TAB_ROWS = {"Meta": "Inbound - Meta", "WhatsApp": "Inbound - Website (Whatsapp)"}
_INTERNAL = ("beyondborder.co.uk", "beyondborderglobal.com")     # our own test entries
_TEST_DOMAINS = ("example.com", "example.org", "example.net", "test.com")
# Same person on two tabs the same day: the first application wins. The WhatsApp
# chatbot follows up Meta ad leads, so Meta goes before WhatsApp.
_TAB_ORDER = {"Website": 0, "Meta": 1, "WhatsApp": 2}


def _parse_date(text: str) -> dt.date | None:
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"4\d{4}(\.\d+)?", t):                 # a Sheets serial date, e.g. 46323
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(t)))
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


def _hkey(h: str) -> str:
    return " ".join((h or "").lower().split())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _source_leads(channel_of, start: dt.date, end: dt.date) -> list[dict]:
    """Source-sheet rows dated start..end (inclusive), oldest first."""
    today = dt.date.today()
    out = []
    for title, rows in sheets.read_source_tabs():
        if not rows:
            continue
        cols = {}
        for i, h in enumerate(rows[0]):
            cols.setdefault(_hkey(h), i)

        def col(r, *names):
            for n in names:
                i = cols.get(n)
                if i is not None and i < len(r) and r[i].strip():
                    return r[i].strip()
            return ""
        channel = channel_of(title)
        order = _TAB_ORDER.get(channel, 3)
        for n, r in enumerate(rows[1:], start=2):
            d = _parse_date(col(r, "date of lead", "date od lead", "timestamp"))
            if d and d > today + dt.timedelta(days=1):            # day/month swapped
                try:
                    d = dt.date(d.year, d.day, d.month)
                except ValueError:
                    d = None
            if not d or not start <= d <= end:
                continue
            email = col(r, "email").lower()
            if email.endswith(_INTERNAL):
                continue
            out.append({"date": d, "order": order, "row": n, "channel": channel,
                        "name": col(r, "name"), "email": email, "phone": col(r, "number"),
                        "form": col(r, "form type"), "heard": col(r, "where have you heard about us")})
    out.sort(key=lambda x: (x["date"], x["order"], x["row"]))
    return out


def _is_junk(lead: dict) -> bool:
    """Test entries ("Test Person", test@example.com) and made-up names: a
    keyboard mash (kjhdsasdsdf, hardly any vowels) or bot names in random case
    (QBQWWWhdLKEoBnBnWpi)."""
    email, name = lead["email"], lead["name"]
    if email.split("@")[-1] in _TEST_DOMAINS or re.fullmatch(r"test\d*@.*", email):
        return True
    if re.search(r"\btest\b", name.lower()):
        return True
    if "@" in name:                                    # an email typed in the name box
        return False
    for tok in re.findall(r"[A-Za-z]{8,}", name):
        if sum(ch in "aeiouyAEIOUY" for ch in tok) / len(tok) < 0.2 or \
                len(re.findall(r"[a-z][A-Z]", tok)) >= 3:
            return True
    return False


def _channel_row(lead: dict) -> str:
    if lead["channel"] in _TAB_ROWS:
        return _TAB_ROWS[lead["channel"]]
    heard = _hkey(lead["heard"])
    return next((row for needle, row in config.GTM_WEBSITE_HEARD_ROWS if needle in heard), _WEBSITE_ROW)


def _pipedrive_info(c, lead: dict, cache: dict, need_owner: bool) -> dict:
    """{person, prior, owner} for the lead's Pipedrive contact, cached per lead.
    The contact lookup runs once (`prior` — in Pipedrive before this
    application — never changes). `owner` (of a deal made since) is re-read at
    most hourly, only while it's needed and still missing, to spare the API."""
    key = f"{lead['email'] or _digits(lead['phone'])}|{lead['date']}"
    hit = cache.get(key)
    if hit and "ids" not in hit and "prior" in hit:     # cached by the first version
        hit["ids"] = [hit["person"]] if hit.get("person") else []
    if hit and "ids" in hit:
        if c and not hit["ids"] and time.time() - hit.get("ids_at", 0) > 3600:
            hit["ids"] = _find_people(c, lead)         # the contact may have been made since
            hit["person"] = min(hit["ids"]) if hit["ids"] else None
            hit["ids_at"] = time.time()
        if need_owner and c and not hit["owner"] and time.time() - hit.get("owner_at", 0) > 3600:
            hit["owner"] = _deal_owner(c, hit["ids"], lead["date"])
            hit["owner_at"] = time.time()
        return hit
    if not c:                                          # no Pipedrive: nothing to cache
        return {"person": None, "prior": False, "owner": "", "ids": []}
    ids = _find_people(c, lead)
    cutoff = (lead["date"] - dt.timedelta(days=1)).isoformat()
    prior = any((c.get(f"/persons/{pid}").get("data") or {}).get("add_time", "9")[:10] < cutoff
                for pid in ids)
    info = {"ids": ids, "person": min(ids) if ids else None, "prior": prior, "ids_at": time.time(),
            "owner": _deal_owner(c, ids, lead["date"]) if need_owner else "",
            "owner_at": time.time() if need_owner else 0}
    cache[key] = info
    return info


def _find_people(c, lead: dict) -> list[int]:
    """Pipedrive contacts with the lead's email, or the same phone number."""
    from .pipedrive import _same_phone
    ids = set()
    if lead["email"]:
        for i in (c.get("/persons/search", term=lead["email"], fields="email",
                        exact_match="true").get("data") or {}).get("items") or []:
            ids.add(i["item"]["id"])
    want = _digits(lead["phone"])
    if len(want) >= 7:
        for i in (c.get("/persons/search", term=want[-9:], fields="phone").get("data") or {}).get("items") or []:
            if any(_same_phone(want, _digits(p)) for p in i["item"].get("phones") or []):
                ids.add(i["item"]["id"])
    return sorted(ids)


def _deal_owner(c, ids: list[int], since: dt.date) -> str:
    """Owner of the first deal made for these contacts on/after `since`."""
    for pid in ids:
        for d in c.get(f"/persons/{pid}/deals", status="all_not_deleted").get("data") or []:
            if (d.get("add_time") or "")[:10] >= since.isoformat():
                u = d.get("user_id")
                if isinstance(u, dict) and u.get("name"):
                    return u["name"]
    return ""


def _app_owners() -> tuple[dict, dict]:
    """LeadSearch's AE per email / phone (accepted leads; duplicates follow
    the lead they were merged into). '' = in LeadSearch but not distributed."""
    leads = {l["id"]: l for l in db.list_leads()}
    by_email, by_phone = {}, {}
    for l in leads.values():
        t, hops = l, 0
        while t and t.get("status") == "duplicate" and t.get("merged_into") and hops < 5:
            t, hops = leads.get(t["merged_into"]), hops + 1
        owner = (t or {}).get("owner") if (t or {}).get("status") == "accepted" else ""
        owner = owner or ""
        e, p = (l.get("email") or "").strip().lower(), _digits(l.get("phone"))[-9:]
        if e and not by_email.get(e):
            by_email[e] = owner
        if len(p) >= 7 and not by_phone.get(p):
            by_phone[p] = owner
    return by_email, by_phone


def count_week(start: dt.date, channel_of) -> dict:
    """{row label: count} for the week starting `start`, plus the per-lead detail."""
    from .pipedrive import Client
    end = start + dt.timedelta(days=6)
    leads = _source_leads(channel_of, start, end)
    c = Client() if config.PIPEDRIVE_API_TOKEN else None
    try:
        cache = json.loads(db.meta_get("gtm_pd_cache", "{}") or "{}")
    except ValueError:
        cache = {}
    by_email, by_phone = _app_owners()
    aes = {n.lower(): n for n in config.AE_ROUND_ROBIN + config.AE_LATAM + [config.AE_PILOT] if n}
    counts, seen, detail = {}, set(), []
    for lead in leads:
        p = _digits(lead["phone"])[-9:]
        in_app = lead["email"] in by_email or (len(p) >= 7 and p in by_phone)
        junk = _is_junk(lead)
        info = {"ids": [], "prior": False} if junk else _pipedrive_info(c, lead, cache, need_owner=False)
        # Same person = any shared contact, email or phone number.
        who = {f"pd:{i}" for i in info["ids"]} | {x for x in (lead["email"], p if len(p) >= 7 else "") if x}
        if junk:
            kind = "junk"
        elif lead["form"].lower().startswith("re-engagement") or info["prior"]:
            kind = "re-engagement"
        elif who & seen:
            kind = "repeat"
            seen |= who
        else:
            kind = "counted"
            seen |= who
        row, ae = _channel_row(lead), ""
        if kind == "counted":
            counts[row] = counts.get(row, 0) + 1
            owner = by_email.get(lead["email"]) or (by_phone.get(p) if len(p) >= 7 else "") or ""
            if not owner and not in_app:              # not in LeadSearch: distributed by hand?
                owner = _pipedrive_info(c, lead, cache, need_owner=True)["owner"]
            first = (owner.split() or [""])[0].lower()
            if first in aes:
                ae = aes[first]
                label = config.GTM_SQL_ROW.format(ae=ae)
                counts[label] = counts.get(label, 0) + 1
        detail.append({**lead, "date": lead["date"].isoformat(), "kind": kind, "row": row, "ae": ae})
    keep = (start - dt.timedelta(days=21)).isoformat()     # forget leads long out of the window
    db.meta_set("gtm_pd_cache", json.dumps({k: v for k, v in cache.items() if k.rsplit("|", 1)[-1] >= keep}))
    return {"counts": counts, "leads": detail}


def _week_rows() -> list[str]:
    """Every row this report owns (overwritten each recount)."""
    rows = [_WEBSITE_ROW, *_TAB_ROWS.values(), *(r for _, r in config.GTM_WEBSITE_HEARD_ROWS)]
    aes = dict.fromkeys(n for n in config.AE_ROUND_ROBIN + config.AE_LATAM + [config.AE_PILOT] if n)
    rows += [config.GTM_SQL_ROW.format(ae=a) for a in aes]
    return list(dict.fromkeys(rows))


def recount(channel_of, weeks_back: int = 1, write: bool = True) -> dict:
    """Recount this week and `weeks_back` weeks before it; overwrite their cells."""
    out = {"weeks": {}, "changed": 0, "skipped": []}
    if not config.GTM_SHEET_ID:
        return out
    ws = sheets._client().open_by_key(config.GTM_SHEET_ID).worksheet(config.GTM_TAB)
    values = ws.get_all_values()
    formulas = ws.get_all_values(value_render_option="FORMULA")
    weeks = [(d, j) for j, h in enumerate(values[0] if values else []) if (d := _parse_date(h))]
    rows = {}
    for i, r in enumerate(values):
        label = _hkey(r[1] if len(r) > 1 else "")
        if label and label not in rows:
            rows[label] = i
    today = dt.date.today()
    current = [(d, j) for d, j in weeks if d <= today < d + dt.timedelta(days=7)]
    if not current:
        out["skipped"].append(f"no week column for {today}")
        return out
    idx = weeks.index(current[-1])
    writes = []
    for d, c in weeks[max(0, idx - weeks_back): idx + 1]:
        res = count_week(d, channel_of)
        out["weeks"][d.isoformat()] = res["counts"]
        out.setdefault("detail", {})[d.isoformat()] = res["leads"]
        if not write:
            continue
        src = _template_col(formulas, weeks, c)
        if src is not None:
            _copy_formulas(ws, values, formulas, src, c)
        for label in _week_rows():
            r = rows.get(_hkey(label))
            if r is None:
                continue
            if str(formulas[r][c] if c < len(formulas[r]) else "").startswith("="):
                out["skipped"].append(f"{rowcol_to_a1(r + 1, c + 1)} is a formula")
                continue
            n = res["counts"].get(label, 0)
            cur = values[r][c] if c < len(values[r]) else ""
            if str(cur).strip() != (str(n) if n else ""):
                writes.append((r + 1, c + 1, n if n else ""))
    if writes:
        sheets.write_cells(ws, writes)
    out["changed"] = len(writes)
    if write:                                     # what the GTM page shows
        db.meta_set("gtm_last", json.dumps({"at": time.time(), "weeks": out["weeks"],
                                            "detail": out["detail"], "rows": _week_rows()}))
    return out


def last() -> dict:
    """The last recount written to the sheet (for the GTM page)."""
    try:
        return json.loads(db.meta_get("gtm_last", "{}") or "{}")
    except ValueError:
        return {}


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
