"""FastAPI app: upload leads, run the pipeline, review results, export."""
from __future__ import annotations

import csv
import io
import re

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .pipeline import llm, orchestrator

app = FastAPI(title="LeadSearch")
STATIC_DIR = config.ROOT / "app" / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# A LinkedIn URL or bare handle, e.g. "linkedin.com/in/foo" or "uk.linkedin.com/in/foo".
_LI_URL_RE = re.compile(r"(https?://[^\s,;]+|(?:[a-z]{2,3}\.)?linkedin\.com/[^\s,;]+)", re.I)
# Phrases where the lead says they have no LinkedIn -> skip discovery entirely.
_LI_OPTOUT = (
    "don't have", "do not have", "dont have", "no linkedin", "not on linkedin",
    "don't use", "dont use", "no profile", "i don't have", "not available",
    "no account", "n/a",
)


def resolve_linkedin(li_cell: str, message: str) -> tuple[str, bool]:
    """Interpret the free-text LinkedIn column (and message).

    Returns (url, optout):
      * url    — a LinkedIn URL found in the cell or the message ("check me at ..."),
                 normalized with a scheme; empty if none.
      * optout — True if the lead explicitly says they have no LinkedIn, so we
                 should NOT run discovery for them.
    """
    for src in (li_cell, message):
        m = _LI_URL_RE.search(src or "")
        if m and "linkedin.com" in m.group(0).lower():
            url = m.group(0).rstrip(".,;)")
            if not url.lower().startswith("http"):
                url = "https://" + url
            return url, False
    blob = f"{li_cell} {message}".lower()
    if any(p in blob for p in _LI_OPTOUT):
        return "", True
    return "", False


def _insert_row(val) -> bool:
    if not any(val(k) for k in ("name", "company", "email", "phone", "message")):
        return False
    url, optout = resolve_linkedin(val("linkedin"), val("message"))
    db.insert_lead(val("name"), val("company"), val("email"),
                   val("phone"), val("message"), url, 1 if optout else 0)
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

    # Let the LLM figure out which column is which (handles messy/renamed headers,
    # split first/last name, headerless files, reordered or partial columns).
    try:
        mapping = llm.map_columns(rows[:5])
    except llm.LLMError as e:
        raise HTTPException(400, f"Could not map columns (LLM): {e}")

    has_header = bool(mapping.get("has_header"))
    data_rows = rows[1:] if has_header else rows

    count = 0
    for row in data_rows:
        def val(key: str, _row=row) -> str:
            cells = [_row[i].strip() for i in _indices(mapping, key) if 0 <= i < len(_row)]
            return " ".join(c for c in cells if c).strip()
        if _insert_row(val):
            count += 1

    mapped = {k: mapping.get(k) for k in
              ("name", "company", "email", "phone", "linkedin", "message")}
    return JSONResponse({"inserted": count, "mapped_columns": mapped,
                         "header_detected": has_header})


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
