"""FastAPI app: upload leads, run the pipeline, review results, export."""
from __future__ import annotations

import csv
import io
import re

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .pipeline import orchestrator

app = FastAPI(title="LeadSearch")
STATIC_DIR = config.ROOT / "app" / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ── Column aliases for flexible CSV headers ───────────────────────────────
_ALIASES = {
    "name": {"name", "full name", "fullname", "contact", "lead"},
    "company": {"company", "organization", "org", "employer"},
    "email": {"email", "e-mail", "email address"},
    "phone": {"phone", "phone number", "mobile", "tel", "telephone", "number",
              "contact number", "mobile number"},
    "linkedin": {"linkedin", "linkedin profile url", "linkedin url",
                 "linkedin profile", "li url"},
    "message": {"message", "notes", "note", "comment", "about", "bio"},
}

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


def _map_headers(fieldnames: list[str]) -> dict:
    mapping = {}
    for field in fieldnames or []:
        key = field.strip().lower()
        for canonical, names in _ALIASES.items():
            if key in names and canonical not in mapping:  # first column wins
                mapping[canonical] = field
                break
    return mapping


# Positional column order for headerless exports (the CRM's native layout):
# Name, Email, Number, LinkedIn URL, Visa, Message, ...
_POSITIONAL = {"name": 0, "email": 1, "phone": 2, "linkedin": 3, "message": 5}


def _looks_headerless(first_row: list[str]) -> bool:
    """A real header row has no email/phone values in it; a data row does."""
    joined = " ".join(first_row)
    return "@" in joined  # an email in row 1 means it's data, not a header


def _insert_row(val) -> bool:
    if not any(val(k) for k in ("name", "company", "email", "phone", "message")):
        return False
    url, optout = resolve_linkedin(val("linkedin"), val("message"))
    db.insert_lead(val("name"), val("company"), val("email"),
                   val("phone"), val("message"), url, 1 if optout else 0)
    return True


@app.post("/api/upload")
async def upload(file: UploadFile) -> JSONResponse:
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        raise HTTPException(400, "The CSV is empty.")

    count = 0
    if _looks_headerless(all_rows[0]):
        # No header — map by fixed column position.
        mapping = {k: f"col{v}" for k, v in _POSITIONAL.items()}
        for row in all_rows:
            def val(k: str, _row=row) -> str:
                i = _POSITIONAL.get(k)
                return (_row[i].strip() if i is not None and i < len(_row) else "")
            if _insert_row(val):
                count += 1
        return JSONResponse({"inserted": count, "mapped_columns": mapping,
                             "header_detected": False})

    # Header row present — map columns by name.
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames or [])
    if "name" not in mapping:
        raise HTTPException(
            400,
            f"Could not find a Name column. Detected headers: {reader.fieldnames}. "
            f"Expected one of: Name, Company, Email, Phone, Message.",
        )
    for row in reader:
        def val(k: str, _row=row) -> str:
            col = mapping.get(k)
            return (_row.get(col, "") or "").strip() if col else ""
        if _insert_row(val):
            count += 1
    return JSONResponse({"inserted": count, "mapped_columns": mapping,
                         "header_detected": True})


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
