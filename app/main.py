"""FastAPI app: upload leads, run the pipeline, review results, export."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, gtm, sheets
from .pipeline import distribution, llm, orchestrator
from .pipeline.linkedin import canonical_linkedin
from .pipeline.urls import canonical_url

app = FastAPI(title="LeadSearch")
STATIC_DIR = config.ROOT / "app" / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # A restart/deploy can cut a lead off mid-pipeline; put it back in the queue.
    requeued = db.requeue_interrupted()
    # Resume reviewer-triggered re-screens a restart cut off.
    for l in db.list_leads():
        if (l.get("screening") or {}).get("note") == "Screening this LinkedIn profile…":
            orchestrator.start_rescreen(l["id"])
    if requeued and config.SCHEDULER_ENABLED:
        orchestrator.start_worker()
    _start_scheduler()


# ── Automation ────────────────────────────────────────────────────────────
# Every SCHEDULER_INTERVAL_SECONDS: pull new leads -> process them -> push to the
# destination sheet. Pull and push can each be paused independently; while push
# is playing the manual Push button is disabled (cycles already push). The
# next-run time and pause states live in the DB, so restarts keep them.
_scheduler_thread = None
_scheduler_stop = threading.Event()
_wake = threading.Event()            # set on resume -> run a cycle now
_cycle_lock = threading.Lock()       # one pull/process/push cycle at a time
_sync_lock = threading.Lock()        # one sheet sync at a time (manual or auto)


def _set_next_run(at: float) -> None:
    db.meta_set("sched_next_run", str(at))


def _next_run() -> float:
    try:
        return float(db.meta_get("sched_next_run", "0"))
    except ValueError:
        return 0.0


def _run_cycle(trigger: str) -> dict:
    """One automation cycle. Never raises; the outcome is recorded for the UI."""
    result: dict = {"trigger": trigger, "started": time.time()}
    with _cycle_lock:
        db.meta_set("sched_running", "1")
        try:
            if config.GOOGLE_SOURCE_SHEET_ID and not _paused():
                if db.count_seen() == 0:
                    # Fresh database: never process the backlog. Mark everything
                    # currently in the sheet as seen; only later leads get processed.
                    result["baselined"] = _baseline()["baselined"]
                else:
                    result["pulled"] = _pull_new(limit=config.SCHEDULER_MAX_PER_CYCLE)["inserted"]
            # Process everything queued, and wait for it, so this cycle pushes
            # its own results instead of the next one.
            orchestrator.start_worker()
            while orchestrator.is_running() and not _scheduler_stop.is_set():
                time.sleep(1)
            if config.GOOGLE_DEST_SHEET_ID and not _push_paused():
                pushed = _locked_sync()
                result.update(added=pushed["added"], updated=pushed["updated"],
                              in_review=pushed["in_review"], incomplete=len(pushed["incomplete"]))
            if config.GTM_SHEET_ID:
                # Weekly GTM report: +1 per newly accepted lead (channel + AE SQL).
                rep = gtm.sync_report()
                result["report"] = {"added": rep["added"], "removed": rep["removed"],
                                    "skipped": len(rep["skipped"])}
                if rep["skipped"]:
                    result["report_issue"] = rep["skipped"][0][:200]
        except Exception as e:  # noqa: BLE001 — never let the loop die; show it instead
            result["error"] = str(e)[:300]
        finally:
            result["finished"] = time.time()
            db.meta_set("sched_last_run", json.dumps(result))
            db.meta_set("sched_running", "0")
            _set_next_run(time.time() + config.SCHEDULER_INTERVAL_SECONDS)
    return result


def _paused() -> bool:
    """Pull automation paused (no new leads pulled from the source sheet)."""
    return db.meta_get("sched_paused") == "1"


def _push_paused() -> bool:
    """Push automation paused (nothing goes to the destination sheet on its own;
    the manual Push button is enabled instead)."""
    return db.meta_get("sched_push_paused") == "1"


def _all_paused() -> bool:
    return _paused() and _push_paused()


def _scheduler_loop() -> None:
    if _next_run() <= 0:
        _set_next_run(time.time() + 60)        # first ever start: first cycle in a minute
    while not _scheduler_stop.is_set():
        if _all_paused():
            _wake.wait()                        # sleep until resumed (or stopped)
            _wake.clear()
            continue
        woke = _wake.wait(max(0.0, _next_run() - time.time()))
        _wake.clear()
        if _scheduler_stop.is_set():
            break
        if _all_paused():
            continue                            # paused while waiting
        _run_cycle("manual" if woke else "scheduled")


def _start_scheduler() -> None:
    global _scheduler_thread
    if config.SCHEDULER_ENABLED and _scheduler_thread is None:
        db.meta_set("sched_running", "0")      # a restart ends any cycle in flight
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()


def _locked_sync() -> dict:
    with _sync_lock:
        return _sync_dest()


def _automation_status() -> dict:
    try:
        last = json.loads(db.meta_get("sched_last_run", "") or "null")
    except json.JSONDecodeError:
        last = None
    nxt = _next_run()
    return {
        "enabled": config.SCHEDULER_ENABLED,
        "interval": config.SCHEDULER_INTERVAL_SECONDS,
        "max_per_cycle": config.SCHEDULER_MAX_PER_CYCLE,
        "running": db.meta_get("sched_running") == "1",
        "paused": _paused(),
        "push_paused": _push_paused(),
        "next_in": (max(0, round(nxt - time.time()))
                    if config.SCHEDULER_ENABLED and nxt and not _all_paused() else None),
        "last": last,
        "seen": db.count_seen(),
        "persistent_disk": bool(os.getenv("DATA_DIR")),
    }


# A LinkedIn URL, tolerant of distortions: optional scheme, any/garbled subdomain
# (www / w / ww / in / uk / none), embedded in surrounding text.
_LINKEDIN_RE = re.compile(r"(?:https?://)?[a-z0-9.\-]*linked[il1]n\.com/[^\s,;\"'>)\]]+", re.I)
# Any other URL (http(s):// or www.), used to spot a non-LinkedIn link.
_ANY_URL_RE = re.compile(r"(?:https?://|www\.)[^\s,;\"'>)\]]+", re.I)
_TRIM = ".,;)]'\""
# Phrases where the lead says they have no LinkedIn -> the pipeline skips the
# LinkedIn search (other web enrichment still runs) and the lead isn't screened.
_LI_OPTOUT = (
    "don't have", "do not have", "dont have", "no linkedin", "not on linkedin",
    "don't use", "dont use", "no profile", "i don't have", "not available",
    "no account", "n/a",
)


def _normalize_linkedin(text: str) -> str:
    """Extract a LinkedIn URL from messy text and canonicalize it to
    https://www.linkedin.com/in/<slug>; '' if none (or no profile path)."""
    m = _LINKEDIN_RE.search(text or "")
    if not m:
        return ""
    return canonical_linkedin(m.group(0).rstrip(_TRIM))


def _first_other_url(text: str) -> str:
    """A non-LinkedIn URL in the text, normalized with a scheme; '' if none."""
    for m in _ANY_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(_TRIM)
        if re.search(r"linked[il1]n\.com", u, re.I):
            continue
        return u if u.lower().startswith("http") else "https://" + u
    return ""


def classify_linkedin(li_cell: str, message: str) -> dict:
    """Decide how to route a lead based on its LinkedIn field (and message).
    Returns {linkedin_url, other_url, optout} — exactly one is meaningful."""
    for src in (li_cell, message):               # a LinkedIn URL, even distorted
        url = _normalize_linkedin(src)
        if url:
            return {"linkedin_url": url, "other_url": "", "optout": False}
    other = _first_other_url(li_cell)            # a non-LinkedIn link in the field
    if other:
        return {"linkedin_url": "", "other_url": other, "optout": False}
    # Explicit "no LinkedIn": any opt-out phrase in the LinkedIn field, or in the
    # message only when it's about LinkedIn (so "not available Monday" doesn't
    # count). Curly apostrophes ("Don’t have one") are normalized first.
    cell = (li_cell or "").lower().replace("’", "'")
    msg = (message or "").lower().replace("’", "'")
    if any(p in cell for p in _LI_OPTOUT) or (
            "linkedin" in msg and any(p in msg for p in _LI_OPTOUT)):
        return {"linkedin_url": "", "other_url": "", "optout": True}
    return {"linkedin_url": "", "other_url": "", "optout": False}


def _channel_from_tab(title: str) -> str:
    """Normalize a source tab title (or a per-row channel/source cell) into a
    channel label for analytics."""
    t = (title or "").lower().strip()
    if "website" in t or t in ("web", "site") or "contact form" in t:
        return "Website"
    if "meta" in t or "facebook" in t or "instagram" in t or t in ("fb", "ig"):
        return "Meta"
    if "chatbot" in t or "whatsapp" in t or t == "wa" or t.startswith("wa "):
        return "WhatsApp"
    if "google" in t:
        return "Google Ads"
    if "referral" in t:
        return "Referral"
    if "linkedin" in t:
        return "LinkedIn"
    return title or "Other"


# Visa interest -> standard form. Most specific first; a letter suffix (O1A,
# EB1A) is kept only when the lead actually wrote it.
_L, _R = r"(?<![a-z])", r"(?![a-z])"      # not inside a longer word
_SEP = r"[\s_-]*"
_VISA_RULES = [
    (rf"niw", "EB2 NIW"),
    (rf"{_L}eb{_SEP}1{_SEP}a{_R}", "EB1A"),
    (rf"{_L}eb{_SEP}1", "EB1"),
    (rf"{_L}eb{_SEP}2", "EB2"),
    (rf"{_L}eb{_SEP}3", "EB3"),
    (rf"{_L}h{_SEP}1{_SEP}b", "H1B"),
    (rf"{_L}o{_SEP}1{_SEP}a{_R}", "O1A"),
    (rf"{_L}o{_SEP}1{_SEP}b{_R}", "O1B"),
    (rf"{_L}o{_SEP}1", "O1"),
    (rf"{_L}l{_SEP}1(?![a-z0-9])", "L1"),
    (rf"{_L}e{_SEP}2(?![a-z0-9])", "E2"),
]


def normalize_visa(text: str) -> str:
    """'O1 Visa' / 'O-1' / 'o1' -> 'O1'; 'o-1a_' -> 'O1A'; 'eb-2_niw' /
    'EB-2 NIW Green Card' -> 'EB2 NIW'; 'eb-1a' -> 'EB1A'; 'H1-B Visa' -> 'H1B'.
    "Not sure (yet)" defaults to 'O1'. Anything unrecognized is kept as typed."""
    raw = (text or "").strip()
    t = raw.lower()
    if not t or "linkedin.com" in t or "http" in t or "www." in t:
        return raw
    if re.search(r"not[\s_-]*sure", t):
        return "O1"
    for pat, code in _VISA_RULES:
        if re.search(pat, t):
            return code
    return raw


# Channel -> value for the destination sheet's "Source" column.
_SOURCE_LABEL = {"Meta": "Meta Ads", "WhatsApp": "WhatsApp", "Website": "Website",
                 "Google Ads": "Google Ads", "Referral": "Referral", "LinkedIn": "LinkedIn"}


def _insert_row(val, channel: str = "", link: str = "", origin: str = "") -> bool:
    """Insert one mapped row. `link` is the LLM-repaired profile link, if any;
    otherwise the raw cell is used (deterministic parsing still applies)."""
    if not any(val(k) for k in ("name", "company", "email", "phone", "message")):
        return False
    raw_link = val("linkedin")
    info = classify_linkedin(link or raw_link, val("message"))
    row_channel = val("channel")
    lead_id = db.insert_lead(
        val("name"), val("company"), val("email"), val("phone"), val("message"),
        linkedin=info["linkedin_url"], li_optout=1 if info["optout"] else 0,
        other_link=canonical_url(info["other_url"]) or info["other_url"],
        link_raw=raw_link, visa=normalize_visa(val("visa")), lead_date=val("date"),
        channel=_channel_from_tab(row_channel) if row_channel else channel,
        origin=origin,
    )
    # Every lead stays 'queued'; process_lead routes it:
    #  - provided LinkedIn -> fast path: scrape + screen that URL, no search.
    #  - non-LinkedIn link -> full pipeline, with that link verified as a candidate.
    #  - "no LinkedIn"     -> full pipeline minus the LinkedIn search; not screened.
    #  - nothing usable    -> full pipeline (LinkedIn discovery) + screening.
    return True


def _indices(mapping: dict, key: str) -> list[int]:
    """Normalize a mapping value (int | list | null) to a list of column indices."""
    v = mapping.get(key)
    if v is None:
        return []
    out = []
    for x in (v if isinstance(v, list) else [v]):
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _dedup_keys(name: str, email: str, phone: str) -> tuple[str, str]:
    """(key, legacy_key) identifying a lead across pulls.

    key: the email; else the phone digits; else the name. Phone beats name
    because a name can map differently between pulls (one column vs first+last),
    a phone can't. legacy_key is the older "name|digits" form, still checked so
    rows seen before this change aren't treated as new. '' if no identity."""
    email = (email or "").strip().lower()
    digits = re.sub(r"[^0-9]", "", phone or "")
    nm = " ".join((name or "").lower().split())
    legacy = email or (f"{nm}|{digits}" if (nm or digits) else "")
    if email:
        return email, legacy
    if len(digits) >= 7:
        return f"tel:{digits}", legacy
    return (f"name:{nm}" if nm else ""), legacy


def _map_columns(rows: list[list[str]]) -> dict:
    """LLM column mapping, cached per header row: the same tab layout maps the
    same way every pull (stable dedup keys) and costs no LLM call after the first."""
    header = [(c or "").strip() for c in rows[0]]
    ck = "map:" + hashlib.sha1(json.dumps(header).encode()).hexdigest()
    cached = db.meta_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass
    mapping = llm.map_columns(rows[:5])
    # Only cache a confident mapping of a real header row.
    if mapping.get("has_header") and (_indices(mapping, "email") or _indices(mapping, "name")):
        db.meta_set(ck, json.dumps(mapping))
    return mapping


def _ingest_rows(rows: list[list[str]], dedup: bool = False,
                 limit: int | None = None, channel: str = "",
                 origin: str = "upload") -> tuple[int, dict]:
    """Map columns via the LLM, then insert each data row as a queued lead.
    With dedup=True, skip rows already ingested (by _dedup_key) and record new
    ones as seen. Shared by CSV upload, Google Sheet pull, and the scheduler."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return 0, {}
    try:
        mapping = _map_columns(rows)
    except llm.LLMError as e:
        raise HTTPException(400, f"Could not map columns (LLM): {e}")
    data_rows = rows[1:] if mapping.get("has_header") else rows
    seen = db.seen_keys() if dedup else set()

    # Pick the rows to insert first, so the link repair runs only on those.
    picked = []
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        key, legacy = _dedup_keys(val("name"), val("email"), val("phone"))
        if not key:
            continue            # no name, email or phone: nothing to identify or process
        if dedup and (key in seen or legacy in seen):
            continue
        seen.add(key)
        picked.append((val, key))
        if limit and len(picked) >= limit:
            break

    links = _repair_links([val("linkedin") for val, _ in picked])
    count, new_keys = 0, []
    for i, (val, key) in enumerate(picked):
        if _insert_row(val, channel=channel, link=links.get(i, ""), origin=origin):
            count += 1
            new_keys.append(key)
    if new_keys:
        db.mark_seen(new_keys)
    return count, mapping


def _repair_links(cells: list[str], batch: int = 40) -> dict[int, str]:
    """LLM-repair the profile-link cells that aren't already clean URLs
    (typos, '@x (Instagram)', missing /in/, spaces…). Returns {index: url} for
    cells it fixed. Best-effort: if the LLM fails, raw cells are used as-is."""
    todo = [(i, c) for i, c in enumerate(cells) if c and canonical_url(c) != c]
    fixed: dict[int, str] = {}
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        try:
            out = llm.clean_links([c for _, c in chunk])
        except llm.LLMError:
            continue
        for j, url in out.items():
            if 0 <= j < len(chunk) and url:
                clean = canonical_url(url)
                if clean:
                    fixed[chunk[j][0]] = clean
    return fixed


def _row_keys(rows: list[list[str]]) -> list[str]:
    """Dedup keys for all data rows in a tab (used to baseline the backlog)."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    try:
        mapping = _map_columns(rows)
    except llm.LLMError:
        return []
    data_rows = rows[1:] if mapping.get("has_header") else rows
    keys = []
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        k, _ = _dedup_keys(val("name"), val("email"), val("phone"))
        if k:
            keys.append(k)
    return keys


@app.post("/api/upload")
async def upload(file: UploadFile) -> JSONResponse:
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise HTTPException(400, "The CSV is empty.")
    count, mapping = _ingest_rows(rows, channel="Upload")
    mapped = {k: mapping.get(k) for k in
              ("name", "company", "email", "phone", "linkedin", "message")}
    return JSONResponse({"inserted": count, "mapped_columns": mapped,
                         "header_detected": bool(mapping.get("has_header"))})


# Destination-sheet columns, matched to the sheet BY HEADER NAME (case/space
# insensitive), so people can reorder or add columns without breaking the sync.
# (normalized key, header text used only when the sheet is completely empty)
_DEST_COLS = [
    ("name", "Name "), ("email", "Email"), ("number", "Number"),
    ("linkedin profile url", "Linkedin Profile URL"),
    ("visa intrested in", "Visa intrested in"), ("message", "Message"),
    ("date of lead", "Date of lead"), ("company", "Company"),
    ("designation", "Designation "), ("owner (ae) id", "Owner (AE) Id"),
    ("owner name", "Owner Name "), ("qualification status", "Qualification Status"),
    ("source", "Source "),
]
# Pipedrive creates a deal from a row once "Source" is set, so Source is only
# ever written as part of a complete new row — never on its own, never later.
_SOURCE = "source"


def _hkey(header: str) -> str:
    return " ".join((header or "").lower().split())


def _owner_info(owner: str) -> tuple[str, str]:
    """AE name (as assigned) -> (Pipedrive owner id, full name); id '' if unknown."""
    words = (owner or "").split()
    if not words:
        return "", ""
    return config.AE_DIRECTORY.get(owner.strip().lower()) or \
        config.AE_DIRECTORY.get(words[0].lower()) or ("", owner)


def _dest_fields(l: dict) -> dict[str, str]:
    chosen = l.get("chosen") or {}
    person = chosen.get("person") or {}
    ex = l.get("extracted") or {}
    s = l.get("screening") or {}
    tier = s.get("tier", "")
    qual = tier or l.get("status", "")
    if tier and s.get("best_path"):
        qual = f"{tier} · {s.get('best_path')}"
    url = chosen.get("url", "") or ""
    owner_id, owner_name = _owner_info(l.get("owner", ""))
    f = {
        "name": person.get("name") or l.get("name", ""),
        "email": l.get("email", ""),
        "number": l.get("phone", ""),
        "linkedin profile url": canonical_url(url) or url,
        "visa intrested in": normalize_visa(l.get("visa", "")),
        "message": l.get("message", ""),
        "date of lead": l.get("lead_date", ""),
        "company": person.get("company") or ex.get("company", ""),
        "designation": person.get("role") or ex.get("role_guess", ""),
        "owner (ae) id": owner_id,
        "owner name": owner_name,
        "qualification status": qual,
        _SOURCE: _SOURCE_LABEL.get(l.get("channel") or "", ""),
    }
    return {k: str(v or "").strip() for k, v in f.items()}


def _missing(f: dict[str, str]) -> list[str]:
    """What a row lacks to become a usable Pipedrive deal (empty = complete)."""
    miss = []
    if not f["name"]:
        miss.append("name")
    if not (f["email"] or f["number"]):
        miss.append("email/number")
    if not f["owner (ae) id"]:
        miss.append("owner id")
    return miss


def _pull_new(limit: int | None = None) -> dict:
    """Read the source tabs and ingest only NEW (deduped) rows. Returns per-tab counts."""
    tabs = sheets.read_source_tabs()
    total, per_tab, remaining = 0, {}, limit
    for title, rows in tabs:
        count, _ = _ingest_rows(rows, dedup=True, limit=remaining,
                                channel=_channel_from_tab(title), origin="sheet")
        per_tab[title] = count
        total += count
        if remaining is not None:
            remaining -= count
            if remaining <= 0:
                break
    return {"inserted": total, "tabs": per_tab}


@app.post("/api/sheets/pull")
def sheets_pull() -> JSONResponse:
    """Read NEW leads (deduped) from the source Google Sheet into the queue."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured (GOOGLE_SERVICE_ACCOUNT_JSON).")
    if not config.GOOGLE_SOURCE_SHEET_ID:
        raise HTTPException(400, "No source sheet configured (GOOGLE_SOURCE_SHEET_ID).")
    if not _cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "An automation cycle is running — it pulls new leads itself.")
    try:
        return JSONResponse(_pull_new())
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    finally:
        _cycle_lock.release()


def _baseline() -> dict:
    """Mark every current source row as seen without processing it."""
    keys = []
    for _title, rows in sheets.read_source_tabs():
        keys.extend(_row_keys(rows))
    db.mark_seen(keys)
    return {"baselined": len(keys), "total_seen": db.count_seen()}


@app.post("/api/sheets/baseline")
def sheets_baseline() -> JSONResponse:
    """Mark ALL current source rows as already-seen WITHOUT processing them, so
    the existing backlog is skipped and only new leads get processed going forward."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured.")
    if not config.GOOGLE_SOURCE_SHEET_ID:
        raise HTTPException(400, "No source sheet configured.")
    if not _cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "An automation cycle is running — try again in a moment.")
    try:
        return JSONResponse(_baseline())
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    finally:
        _cycle_lock.release()


# Only decided leads go to the sheet; 'review' waits for a human decision.
_PUSHABLE = {"accepted", "rejected"}


def _find_dest_row(values: list[list[str]], cols: dict[str, int],
                   hint: int | None, snap: dict[str, str]) -> int | None:
    """Locate the sheet row (1-based) holding a lead, given what we last wrote
    there. Rows shift when people delete/insert/sort, so the stored row number is
    only a hint: verify it, else scan by email (or name + number if no email)."""
    email = snap.get("email", "").lower()
    name = snap.get("name", "").lower()
    phone = snap.get("number", "")

    def cell(row, key):
        i = cols.get(key)
        return (row[i] if i is not None and i < len(row) else "").strip()

    def same(row):
        if email:
            return cell(row, "email").lower() == email
        return bool(name) and cell(row, "name").lower() == name and cell(row, "number") == phone

    if hint and 1 < hint <= len(values) and same(values[hint - 1]):
        return hint
    matches = [i + 1 for i, row in enumerate(values) if i > 0 and same(row)]
    if not matches:
        return None
    exact = [r for r in matches if cell(values[r - 1], "name").lower() == name]
    return (exact or matches)[0]


def _sync_dest() -> dict:
    """Make the destination sheet reflect the decided leads, editing in place.

    - accepted/rejected leads never pushed  -> appended as one complete row
    - pushed leads whose values changed     -> only the changed cells rewritten
      (diffed against what we last wrote, so manual edits elsewhere survive)
    - unchanged                             -> skipped
    - pushed but row gone from the sheet    -> re-added only if it changed since
    - review / queued / processing / error  -> not pushed
    - incomplete (no name, no email+number, or no owner id) -> held back, so
      Pipedrive never gets an empty deal
    Source is written only inside a complete appended row, never updated later.
    """
    all_leads = db.list_leads()
    leads = [l for l in all_leads if l.get("status") in _PUSHABLE]
    out = {"added": 0, "updated": 0, "unchanged": 0, "incomplete": [],
           "in_review": sum(1 for l in all_leads if l.get("status") == "review")}
    pending = []
    for l in leads:
        if not l.get("owner"):
            l["owner"] = distribution.assign_owner(l)
            db.update_lead(l["id"], owner=l["owner"])
        f = _dest_fields(l)
        miss = _missing(f)
        if miss:
            out["incomplete"].append(f"{f['name'] or 'Lead #' + str(l['id'])} (no {', '.join(miss)})")
            continue
        snap = l.get("pushed_row") if isinstance(l.get("pushed_row"), dict) else None
        if l.get("pushed") and snap == f:
            out["unchanged"] += 1
            continue
        pending.append((l, f, snap))
    if not pending:
        return out

    ws, values = sheets.open_dest()
    if not values:
        sheets.append_rows(ws, [[h for _, h in _DEST_COLS]])
        values = [[h for _, h in _DEST_COLS]]
    cols: dict[str, int] = {}
    for i, h in enumerate(values[0]):
        cols.setdefault(_hkey(h), i)
    absent = [k for k in ("name", "email", "number", _SOURCE) if k not in cols]
    if absent:
        raise sheets.SheetsError(f"Destination sheet has no column for: {', '.join(absent)}")
    width = max(len(values[0]), max(cols.values()) + 1)

    to_add, cells, located = [], [], []
    for l, f, snap in pending:
        if not l.get("pushed"):
            to_add.append((l, f))
            continue
        row = _find_dest_row(values, cols, l.get("sheet_row"), snap or f)
        if row is None:
            to_add.append((l, f))          # deleted from the sheet but changed since
            continue
        # Diff against what we last wrote; for leads pushed before snapshots
        # existed, against what's in the sheet now.
        current = values[row - 1] + [""] * width
        prev = snap or {k: current[i].strip() for k, i in cols.items()}
        cells += [(row, cols[k] + 1, v) for k, v in f.items()
                  if k != _SOURCE and k in cols and v != prev.get(k, "")]
        located.append((l, f, row))

    sheets.write_cells(ws, cells)
    for l, f, row in located:
        db.update_lead(l["id"], pushed=1, pushed_row=f, sheet_row=row)
    out["updated"] = len(located)

    new_rows = []
    for _, f in to_add:
        r = [""] * width
        for k, v in f.items():
            if k in cols:
                r[cols[k]] = v
        new_rows.append(r)
    rows_at = sheets.append_rows(ws, new_rows)       # one call: whole rows at once
    for (l, f), row in zip(to_add, rows_at):
        db.update_lead(l["id"], pushed=1, pushed_row=f, sheet_row=row or None)
    out["added"] = len(to_add)
    return out


@app.post("/api/sheets/push")
def sheets_push() -> JSONResponse:
    """Sync decided leads to the destination sheet (add new, edit changed rows).
    Manual only while push automation is paused (or automation is off) — when it
    is playing, every cycle already pushes, so this is refused."""
    if config.SCHEDULER_ENABLED and not _push_paused():
        raise HTTPException(409, "Automation is already pushing leads to the sheet every "
                                 f"{round(config.SCHEDULER_INTERVAL_SECONDS / 60)} min — no need "
                                 "to click Push. Pause push automation to push manually.")
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured (GOOGLE_SERVICE_ACCOUNT_JSON).")
    if not config.GOOGLE_DEST_SHEET_ID:
        raise HTTPException(400, "No destination sheet configured (GOOGLE_DEST_SHEET_ID).")
    try:
        return JSONResponse(_locked_sync())
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))


@app.post("/api/automation/pause")
def automation_pause(body: dict) -> JSONResponse:
    """Pause or resume the pull or the push automation ({"target": "pull"|"push",
    "paused": bool}); the two are independent. Pausing lets a running cycle
    finish; resuming runs a cycle right away (catching leads that arrived, or
    pushing what was held back), then every interval again. Persisted."""
    if not config.SCHEDULER_ENABLED:
        raise HTTPException(400, "Automation is not enabled on this server (SCHEDULER_ENABLED).")
    target = body.get("target", "pull")
    if target not in ("pull", "push"):
        raise HTTPException(400, "target must be 'pull' or 'push'")
    paused = bool(body.get("paused"))
    db.meta_set("sched_paused" if target == "pull" else "sched_push_paused", "1" if paused else "0")
    if not paused:
        _set_next_run(time.time())
    _wake.set()                                 # let the loop see the change now
    return JSONResponse(_automation_status())


@app.post("/api/run")
def run() -> JSONResponse:
    if not config.OPENROUTER_API_KEY:
        raise HTTPException(400, "OPENROUTER_API_KEY is not set in .env")
    if not config.TAVILY_API_KEY:
        raise HTTPException(400, "TAVILY_API_KEY is not set in .env")
    queued = len(db.queued_lead_ids())
    started = orchestrator.start_worker()
    return JSONResponse({"started": started, "queued": queued})


@app.get("/api/status")
def status() -> JSONResponse:
    processing = db.list_leads("processing")
    current = None
    if processing:
        p = processing[0]
        current = {"stage": p.get("stage"), "name": p.get("name"), "id": p.get("id")}
    return JSONResponse(
        {
            "stats": db.stats(),
            "running": orchestrator.is_running(),
            "current": current,
            "providers": config.provider_status(),
            "scheduler": _automation_status(),
        }
    )


@app.get("/api/analytics")
def analytics() -> JSONResponse:
    return JSONResponse(db.analytics())


@app.get("/api/leads")
def leads(status: str = "all") -> JSONResponse:
    return JSONResponse({"leads": db.list_leads(status)})


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: int) -> JSONResponse:
    lead = db.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return JSONResponse(lead)


@app.post("/api/leads/{lead_id}/choose")
def choose(lead_id: int, body: dict) -> JSONResponse:
    lead = db.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    idx = int(body.get("candidate_index", -1))
    candidates = lead.get("candidates") or []
    if idx < 0 or idx >= len(candidates):
        raise HTTPException(400, "Invalid candidate_index")
    chosen = candidates[idx]
    old = lead.get("chosen") or {}
    canon = lambda u: canonical_url(u) or (u or "")
    fields, rescreen = {"candidates": candidates}, False
    if canon(chosen.get("url")).rstrip("/").lower() != canon(old.get("url")).rstrip("/").lower():
        # The verdict belongs to a specific profile. Keep the old one with its
        # candidate (so switching back restores it for free), then:
        #   non-LinkedIn          -> no verdict (only LinkedIn can be screened)
        #   LinkedIn, seen before -> restore its cached verdict
        #   LinkedIn, never seen  -> screen it in the background
        orchestrator._cache_on_candidate(candidates, old, lead.get("screening"))
        if not orchestrator.is_linkedin(chosen.get("url")) or lead.get("li_optout"):
            fields.update(screening=None, screened=0)
        elif chosen.get("screening"):
            fields.update(screening=chosen["screening"], screened=1)
        else:
            fields.update(screening={"note": "Screening this LinkedIn profile…"}, screened=0)
            rescreen = True
    db.update_lead(
        lead_id, status="accepted", chosen=chosen,
        confidence=chosen.get("score", 0.0),
        reasoning=f"Manually selected by reviewer. {chosen.get('reasoning','')}",
        **fields,
    )
    if rescreen:
        orchestrator.start_rescreen(lead_id)
    return JSONResponse({"ok": True})


@app.post("/api/leads/{lead_id}/reject")
def reject(lead_id: int) -> JSONResponse:
    if not db.get_lead(lead_id):
        raise HTTPException(404, "Lead not found")
    db.update_lead(lead_id, status="rejected",
                   reasoning="Manually rejected by reviewer.")
    return JSONResponse({"ok": True})


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int) -> JSONResponse:
    if not db.delete_lead(lead_id):
        raise HTTPException(404, "Lead not found")
    return JSONResponse({"ok": True})


@app.post("/api/reset")
def reset() -> JSONResponse:
    db.clear_all()
    return JSONResponse({"ok": True})


@app.get("/api/export")
def export() -> StreamingResponse:
    rows = db.list_leads("accepted")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "name", "company", "email", "phone", "location", "role",
         "matched_url", "source_type", "confidence", "reasoning"]
    )
    for lead in rows:
        chosen = lead.get("chosen") or {}
        person = chosen.get("person") or {}
        ex = lead.get("extracted") or {}
        writer.writerow(
            [
                lead["id"],
                person.get("name") or lead.get("name", ""),
                person.get("company") or ex.get("company", ""),
                lead.get("email", ""),
                lead.get("phone", ""),
                person.get("location") or ex.get("location", ""),
                person.get("role", ""),
                chosen.get("url", ""),
                chosen.get("source_type", ""),
                lead.get("confidence", ""),
                (lead.get("reasoning", "") or "").replace("\n", " "),
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=verified_leads.csv"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Mount static assets last so /api routes take precedence.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
