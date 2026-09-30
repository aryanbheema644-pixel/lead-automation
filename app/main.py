"""FastAPI app: upload leads, run the pipeline, review results, export."""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, sheets
from .pipeline import llm, orchestrator

app = FastAPI(title="LeadSearch")
STATIC_DIR = config.ROOT / "app" / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# A LinkedIn URL, tolerant of distortions: optional scheme, any/garbled subdomain
# (www / w / ww / in / uk / none), embedded in surrounding text.
_LINKEDIN_RE = re.compile(r"(?:https?://)?[a-z0-9.\-]*linkedin\.com/[^\s,;\"'>)\]]+", re.I)
# Any other URL (http(s):// or www.), used to spot a non-LinkedIn link.
_ANY_URL_RE = re.compile(r"(?:https?://|www\.)[^\s,;\"'>)\]]+", re.I)
_TRIM = ".,;)]'\""
# Phrases where the lead says they have no LinkedIn -> add directly, no discovery.
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
    blob = f"{li_cell} {message}".lower()        # explicit "no LinkedIn"
    if any(p in blob for p in _LI_OPTOUT):
        return {"linkedin_url": "", "other_url": "", "optout": True}
    return {"linkedin_url": "", "other_url": "", "optout": False}


def _insert_row(val) -> bool:
    if not any(val(k) for k in ("name", "company", "email", "phone", "message")):
        return False
    info = classify_linkedin(val("linkedin"), val("message"))
    lead_id = db.insert_lead(
        val("name"), val("company"), val("email"), val("phone"), val("message"),
        linkedin=info["linkedin_url"], li_optout=1 if info["optout"] else 0,
    )
    # Routing:
    #  - provided LinkedIn -> stays 'queued'; the worker skips discovery but still
    #    scrapes+screens that URL (no search/refinement).
    #  - non-LinkedIn link / opt-out -> accepted directly (nothing to screen).
    #  - no usable link -> 'queued' for the full discovery pipeline + screening.
    if info["linkedin_url"]:
        pass  # queued; process_lead fast-path handles it
    elif info["other_url"]:
        db.update_lead(lead_id, status="accepted", stage="provided", confidence=None,
                       chosen={"url": info["other_url"], "source_type": "other link", "person": {}},
                       reasoning="A non-LinkedIn link was provided; added directly, not verified.")
    elif info["optout"]:
        db.update_lead(lead_id, status="accepted", stage="no_linkedin", confidence=None,
                       chosen={}, reasoning="Lead has no LinkedIn; added directly.")
    # else: stays 'queued' -> the pipeline will refine (discover the LinkedIn).
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


def _ingest_rows(rows: list[list[str]]) -> tuple[int, dict]:
    """Map columns via the LLM, then insert each data row as a queued lead.
    Shared by CSV upload and Google Sheet pull. Returns (inserted, mapping)."""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return 0, {}
    try:
        mapping = llm.map_columns(rows[:5])
    except llm.LLMError as e:
        raise HTTPException(400, f"Could not map columns (LLM): {e}")
    data_rows = rows[1:] if mapping.get("has_header") else rows
    count = 0
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        if _insert_row(val):
            count += 1
    return count, mapping


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
    count, mapping = _ingest_rows(rows)
    mapped = {k: mapping.get(k) for k in
              ("name", "company", "email", "phone", "linkedin", "message")}
    return JSONResponse({"inserted": count, "mapped_columns": mapped,
                         "header_detected": bool(mapping.get("has_header"))})


# Destination-sheet columns for refined + screened leads.
_DEST_HEADER = ["Name", "Email", "Phone", "Company", "Location", "Role",
                "LinkedIn / Match URL", "Source", "Status",
                "Fit Tier", "Screen Confidence", "Best Path", "Backup Path",
                "Key Strength", "Red Flag", "Flip Trigger", "Matched Cases",
                "Screening Notes", "Match Reasoning", "Processed At (UTC)"]


def _dest_row(l: dict) -> list:
    chosen = l.get("chosen") or {}
    person = chosen.get("person") or {}
    ex = l.get("extracted") or {}
    s = l.get("screening") or {}
    return [
        person.get("name") or l.get("name", ""),
        l.get("email", ""),
        l.get("phone", ""),
        person.get("company") or ex.get("company", ""),
        person.get("location") or ex.get("location", ""),
        person.get("role") or ex.get("role_guess", ""),
        chosen.get("url", ""),
        chosen.get("source_type", ""),
        l.get("status", ""),
        s.get("tier", ""),
        s.get("confidence", ""),
        s.get("best_path", ""),
        s.get("backup_path", ""),
        s.get("key_strength", ""),
        s.get("red_flag", ""),
        s.get("flip_trigger", ""),
        s.get("matched_cases", ""),
        (s.get("answer") or s.get("note") or s.get("error") or "").replace("\n", " "),
        (l.get("reasoning", "") or "").replace("\n", " "),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ]


@app.post("/api/sheets/pull")
def sheets_pull() -> JSONResponse:
    """Read new leads from the source Google Sheet into the queue."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured (GOOGLE_SERVICE_ACCOUNT_JSON).")
    if not config.GOOGLE_SOURCE_SHEET_ID:
        raise HTTPException(400, "No source sheet configured (GOOGLE_SOURCE_SHEET_ID).")
    try:
        tabs = sheets.read_source_tabs()
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    total = 0
    per_tab = {}
    for title, rows in tabs:
        count, _ = _ingest_rows(rows)  # map columns per-tab (formats differ per source)
        per_tab[title] = count
        total += count
    return JSONResponse({"inserted": total, "tabs": per_tab})


@app.post("/api/sheets/push")
def sheets_push() -> JSONResponse:
    """Write refined (processed, not-yet-pushed) leads to the destination sheet."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(400, "Google service account not configured (GOOGLE_SERVICE_ACCOUNT_JSON).")
    if not config.GOOGLE_DEST_SHEET_ID:
        raise HTTPException(400, "No destination sheet configured (GOOGLE_DEST_SHEET_ID).")
    done = {"accepted", "review", "rejected"}
    leads = [l for l in db.list_leads()
             if l.get("status") in done and not l.get("pushed")]
    if not leads:
        return JSONResponse({"pushed": 0, "note": "No new refined leads to push."})
    try:
        sheets.append_dest_rows(_DEST_HEADER, [_dest_row(l) for l in leads])
    except sheets.SheetsError as e:
        raise HTTPException(400, str(e))
    for l in leads:
        db.update_lead(l["id"], pushed=1)
    return JSONResponse({"pushed": len(leads)})


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
    return JSONResponse(
        {
            "stats": db.stats(),
            "running": orchestrator.is_running(),
            "providers": config.provider_status(),
        }
    )


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
