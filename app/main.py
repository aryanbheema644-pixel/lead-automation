"""FastAPI app: upload leads, run the pipeline, review results, export."""
from __future__ import annotations

import csv
import io
import re
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, sheets
from .pipeline import distribution, llm, orchestrator

app = FastAPI(title="LeadSearch")
STATIC_DIR = config.ROOT / "app" / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    _start_scheduler()


# ── Auto-ingest scheduler ─────────────────────────────────────────────────
_scheduler_thread = None
_scheduler_stop = threading.Event()


def _scheduler_cycle() -> None:
    """One automation tick: pull new leads, run the pipeline, push finished ones."""
    if config.GOOGLE_SOURCE_SHEET_ID:
        try:
            _pull_new(limit=config.SCHEDULER_MAX_PER_CYCLE)
        except Exception:  # noqa: BLE001 — never let the loop die
            pass
    orchestrator.start_worker()
    if config.GOOGLE_DEST_SHEET_ID:
        try:
            _push_unpushed()
        except Exception:  # noqa: BLE001
            pass


def _scheduler_loop() -> None:
    while not _scheduler_stop.wait(config.SCHEDULER_INTERVAL_SECONDS):
        _scheduler_cycle()


def _start_scheduler() -> None:
    global _scheduler_thread
    if config.SCHEDULER_ENABLED and _scheduler_thread is None:
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()


# A LinkedIn URL, tolerant of distortions: optional scheme, any/garbled subdomain
# (www / w / ww / in / uk / none), embedded in surrounding text.
_LINKEDIN_RE = re.compile(r"(?:https?://)?[a-z0-9.\-]*linkedin\.com/[^\s,;\"'>)\]]+", re.I)
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
    """Extract and canonicalize a LinkedIn URL from messy text; '' if none.
    Fixes scheme/subdomain distortions -> https://www.linkedin.com/<path>."""
    m = _LINKEDIN_RE.search(text or "")
    if not m:
        return ""
    frag = m.group(0).rstrip(_TRIM)
    i = frag.lower().find("linkedin.com")
    path = frag[i + len("linkedin.com"):]
    if not path.startswith("/"):
        path = "/" + path
    return "https://www.linkedin.com" + path


def _first_other_url(text: str) -> str:
    """A non-LinkedIn URL in the text, normalized with a scheme; '' if none."""
    for m in _ANY_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(_TRIM)
        if "linkedin.com" in u.lower():
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
    """Normalize a source tab title into a channel label for analytics."""
    t = (title or "").lower()
    if "website" in t:
        return "Website"
    if "meta" in t:
        return "Meta"
    if "chatbot" in t or "whatsapp" in t or t.strip().startswith("wa "):
        return "WhatsApp"
    if "google" in t:
        return "Google Ads"
    if "referral" in t:
        return "Referral"
    if "linkedin" in t:
        return "LinkedIn"
    return title or "Other"


def _insert_row(val, channel: str = "") -> bool:
    if not any(val(k) for k in ("name", "company", "email", "phone", "message")):
        return False
    info = classify_linkedin(val("linkedin"), val("message"))
    lead_id = db.insert_lead(
        val("name"), val("company"), val("email"), val("phone"), val("message"),
        linkedin=info["linkedin_url"], li_optout=1 if info["optout"] else 0,
        other_link=info["other_url"],
        visa=val("visa"), lead_date=val("date"), channel=channel,
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


def _dedup_key(name: str, email: str, phone: str) -> str:
    """Stable per-lead key for dedup: email if present, else name|phone."""
    email = (email or "").strip().lower()
    if email:
        return email
    np = f"{(name or '').strip().lower()}|{re.sub(r'[^0-9]', '', phone or '')}"
    return np if np != "|" else ""


def _ingest_rows(rows: list[list[str]], dedup: bool = False,
                 limit: int | None = None, channel: str = "") -> tuple[int, dict]:
    """Map columns via the LLM, then insert each data row as a queued lead.
    With dedup=True, skip rows already ingested (by _dedup_key) and record new
    ones as seen. Shared by CSV upload, Google Sheet pull, and the scheduler."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return 0, {}
    try:
        mapping = llm.map_columns(rows[:5])
    except llm.LLMError as e:
        raise HTTPException(400, f"Could not map columns (LLM): {e}")
    data_rows = rows[1:] if mapping.get("has_header") else rows
    seen = db.seen_keys() if dedup else set()
    count, new_keys = 0, []
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        key = _dedup_key(val("name"), val("email"), val("phone"))
        if dedup and key and key in seen:
            continue
        if _insert_row(val, channel=channel):
            count += 1
            if key:
                seen.add(key)
                new_keys.append(key)
            if limit and count >= limit:
                break
    if new_keys:
        db.mark_seen(new_keys)
    return count, mapping


def _row_keys(rows: list[list[str]]) -> list[str]:
    """Dedup keys for all data rows in a tab (used to baseline the backlog)."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    try:
        mapping = llm.map_columns(rows[:5])
    except llm.LLMError:
        return []
    data_rows = rows[1:] if mapping.get("has_header") else rows
    keys = []
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        k = _dedup_key(val("name"), val("email"), val("phone"))
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


# Destination-sheet columns — matches the "Master Leads Sheet Cleaned" format
# (trailing spaces kept to mirror the sheet's own headers, used only if empty).
_DEST_HEADER = ["Name ", "Email", "Number", "Linkedin Profile URL",
                "Visa intrested in", "Message", "Date of lead", "Company",
                "Designation ", "Owner (AE)", "Qualification Status"]


def _dest_row(l: dict) -> list:
    chosen = l.get("chosen") or {}
    person = chosen.get("person") or {}
    ex = l.get("extracted") or {}
    s = l.get("screening") or {}
    tier = s.get("tier", "")
    qual = tier or l.get("status", "")
    if tier and s.get("best_path"):
        qual = f"{tier} · {s.get('best_path')}"
    return [
        person.get("name") or l.get("name", ""),                # Name
        l.get("email", ""),                                     # Email
        l.get("phone", ""),                                     # Number
        chosen.get("url", ""),                                  # Linkedin Profile URL
        l.get("visa", ""),                                      # Visa intrested in
        l.get("message", ""),                                   # Message
        l.get("lead_date", ""),                                 # Date of lead
        person.get("company") or ex.get("company", ""),         # Company
        person.get("role") or ex.get("role_guess", ""),         # Designation
        l.get("owner", ""),                                     # Owner (AE)
        qual,                                                   # Qualification Status
    ]


def _pull_new(limit: int | None = None) -> dict:
    """Read the source tabs and ingest only NEW (deduped) rows. Returns per-tab counts."""
    tabs = sheets.read_source_tabs()
    total, per_tab, remaining = 0, {}, limit
    for title, rows in tabs:
        count, _ = _ingest_rows(rows, dedup=True, limit=remaining,
                                channel=_channel_from_tab(title))
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
    try:
        return JSONResponse(_pull_new())
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))


@app.post("/api/sheets/baseline")
def sheets_baseline() -> JSONResponse:
    """Mark ALL current source rows as already-seen WITHOUT processing them, so
    the existing backlog is skipped and only new leads get processed going forward."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured.")
    if not config.GOOGLE_SOURCE_SHEET_ID:
        raise HTTPException(400, "No source sheet configured.")
    try:
        tabs = sheets.read_source_tabs()
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    keys = []
    for _title, rows in tabs:
        keys.extend(_row_keys(rows))
    db.mark_seen(keys)
    return JSONResponse({"baselined": len(keys), "total_seen": db.count_seen()})


def _push_unpushed() -> int:
    """Assign owners and write all processed, not-yet-pushed leads to the dest sheet."""
    done = {"accepted", "review", "rejected"}
    leads = [l for l in db.list_leads()
             if l.get("status") in done and not l.get("pushed")]
    if not leads:
        return 0
    for l in leads:
        if not l.get("owner"):
            l["owner"] = distribution.assign_owner(l)
            db.update_lead(l["id"], owner=l["owner"])
    sheets.append_dest_rows(_DEST_HEADER, [_dest_row(l) for l in leads])
    for l in leads:
        db.update_lead(l["id"], pushed=1)
    return len(leads)


@app.post("/api/sheets/push")
def sheets_push() -> JSONResponse:
    """Write refined (processed, not-yet-pushed) leads to the destination sheet."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured (GOOGLE_SERVICE_ACCOUNT_JSON).")
    if not config.GOOGLE_DEST_SHEET_ID:
        raise HTTPException(400, "No destination sheet configured (GOOGLE_DEST_SHEET_ID).")
    try:
        n = _push_unpushed()
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"pushed": n} if n else {"pushed": 0, "note": "No new refined leads to push."})


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
            "scheduler": config.scheduler_status(),
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
    db.update_lead(
        lead_id, status="accepted", chosen=chosen,
        confidence=chosen.get("score", 0.0),
        reasoning=f"Manually selected by reviewer. {chosen.get('reasoning','')}",
    )
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
