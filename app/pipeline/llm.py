"""OpenRouter (OpenAI-compatible) chat calls that return structured JSON."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .. import config


class LLMError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY is not set — add it to your .env")
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": config.OPENROUTER_APP_URL,
        "X-Title": config.OPENROUTER_APP_NAME,
    }


def _extract_json(text: str) -> Any:
    """Best-effort parse: handle raw JSON or JSON fenced in ```json blocks."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Last resort: grab the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise LLMError(f"LLM did not return valid JSON: {text[:200]}")


def chat_json(system: str, user: str, *, max_tokens: int = 1500) -> Any:
    """One chat completion, expected to return a JSON object."""
    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=90) as client:
            r = client.post(
                f"{config.OPENROUTER_BASE_URL}/chat/completions",
                headers=_headers(),
                json=payload,
            )
    except httpx.HTTPError as e:
        raise LLMError(f"OpenRouter request failed: {e}") from e

    if r.status_code != 200:
        raise LLMError(f"OpenRouter {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected OpenRouter response: {data}") from e
    return _extract_json(content)


# ── Step 0: map arbitrary CSV columns to canonical fields ────────────────
MAP_SYS = (
    "You map the columns of a leads CSV to a fixed set of canonical fields. "
    "Input: the first few parsed rows as a JSON array of rows (each row is a list "
    "of cell strings). For each field, output the 0-based COLUMN INDEX that holds "
    "it, or null if absent.\n"
    "FIELDS:\n"
    "- name: the person's full name. If given-name and surname are in SEPARATE "
    "columns, return a LIST of indices to join in order (e.g. [0,1]).\n"
    "- company: the employer as a SHORT standalone organization name (e.g. "
    "'Wells Fargo'). A descriptive sentence that merely mentions an employer is "
    "NOT the company — it is the message.\n"
    "- email: an email address.\n"
    "- phone: a phone number.\n"
    "- linkedin: a LinkedIn URL or handle.\n"
    "- message: the main free-text note / enquiry / bio / 'about' / comment — "
    "usually the longest sentence-like cell.\n"
    "RULES:\n"
    "1. Set has_header true only if row 0 holds column LABELS (no real data "
    "values); otherwise false (row 0 is already data).\n"
    "2. Decide by the actual VALUES, not just header text. Headers may be renamed, "
    "prefixed, or missing ('Person - First name', 'Mobile No.', 'Deal - Title'). "
    "A cell with '@' is email; mostly digits/'+'/'-'/() is phone; containing "
    "'linkedin.com' is linkedin; a short 'First Last' is a name; a long sentence "
    "is the message.\n"
    "3. Ignore unrelated columns (dates, times, status, visa type, deal/opportunity "
    "title, source, owner, tags, IDs, etc).\n"
    "4. Pick the single best column per field; use null if none fits.\n"
    "EXAMPLE INPUT: [[\"Person - First name\",\"Surname\",\"Email\",\"Deal - Title\","
    "\"LinkedIn\",\"Notes\"],[\"Gauri\",\"Bansal\",\"gb@nyu.edu\",\"O1 Visa\","
    "\"linkedin.com/in/gauribansal\",\"Exec Director at Acme, exploring O-1A\"]]\n"
    "EXAMPLE OUTPUT: {\"has_header\": true, \"name\": [0,1], \"company\": null, "
    "\"email\": 2, \"phone\": null, \"linkedin\": 4, \"message\": 5}\n"
    "(company is null there because 'Exec Director at Acme...' is a sentence = the "
    "message, not a standalone company name.)\n"
    "Respond ONLY with the JSON object, no prose."
)
MAP_SCHEMA = (
    '{"has_header": bool, "name": int|[int,...]|null, "company": int|null, '
    '"email": int|null, "phone": int|null, "linkedin": int|null, "message": int|null}'
)


def map_columns(sample_rows: list[list[str]]) -> dict:
    """Ask the LLM which columns map to which canonical field (one call/upload)."""
    user = (
        f"Rows (row 0 first):\n{json.dumps(sample_rows[:5], ensure_ascii=False)}\n\n"
        f"Return JSON with exactly these keys: {MAP_SCHEMA}."
    )
    out = chat_json(MAP_SYS, user, max_tokens=300)
    return out if isinstance(out, dict) else {}


# ── Step 2: extract & normalize ──────────────────────────────────────────
EXTRACT_SYS = (
    "You are a data-extraction engine for a lead-enrichment pipeline. "
    "Given one raw lead row, extract and lightly enrich a structured profile. "
    "Infer location from phone country codes when possible. Pull role hints and "
    "topical keywords from the free-text message. "
    "For 'company', use the employer name; infer it from the email domain when it "
    "is clearly a company domain (e.g. jane@acme.com -> Acme), but leave it EMPTY "
    "for generic providers (gmail, outlook, yahoo, icloud, etc). "
    "For 'school', give the full name of the university/college the person is "
    "affiliated with when you can infer it — especially from an .edu email domain "
    "(e.g. gb1777@nyu.edu -> New York University) or from the message. Otherwise "
    "leave it empty. Never invent facts you cannot reasonably infer. "
    "Respond ONLY with a JSON object."
)
EXTRACT_SCHEMA = (
    '{"name": str, "company": str, "school": str, "email": str, "phone": str, '
    '"location": str, "role_guess": str, "keywords": [str], "notes": str}'
)


def extract_fields(lead: dict) -> dict:
    user = (
        f"Raw lead:\n"
        f"- Name: {lead.get('name','')}\n"
        f"- Company: {lead.get('company','')}\n"
        f"- Email: {lead.get('email','')}\n"
        f"- Phone: {lead.get('phone','')}\n"
        f"- Message: {lead.get('message','')}\n\n"
        f"Return JSON with exactly these keys: {EXTRACT_SCHEMA}. "
        f"Use empty string / empty list when unknown."
    )
    return chat_json(EXTRACT_SYS, user)


# ── Step 3: generate search queries ──────────────────────────────────────
QUERY_SYS = (
    "You generate targeted web-search queries to find a specific person's online "
    "presence (LinkedIn, personal site, company page, GitHub, Scholar, IMDb, news). "
    "Produce diverse, high-signal queries combining name with company, role, "
    "location and platform hints. Respond ONLY with a JSON object."
)


def generate_queries(extracted: dict, n: int = 5) -> list[str]:
    user = (
        f"Extracted profile:\n{json.dumps(extracted, ensure_ascii=False)}\n\n"
        f'Return JSON: {{"queries": [str]}} with {n} distinct queries, most '
        f"specific first."
    )
    out = chat_json(QUERY_SYS, user)
    queries = out.get("queries") if isinstance(out, dict) else None
    if not queries:
        # Fallback to a couple of deterministic queries.
        name = extracted.get("name", "")
        company = extracted.get("company", "")
        queries = [q for q in [f'"{name}" {company}'.strip(), name] if q]
    return [q for q in queries if q][:n]


# ── Step 7: analyze & match ──────────────────────────────────────────────
MATCH_SYS = (
    "You verify whether a web page is about the same PERSON as a lead — this is "
    "identity resolution, NOT current-state matching. A person's job, title, "
    "employer, city, and even their latest degree change over time, and a lead "
    "often carries only stale or partial info (e.g. an old university email). "
    "So a DIFFERENT current company, role, location, or a newer/additional degree "
    "is NOT evidence of a different person — do not penalize for it. Someone can "
    "go undergrad at one school, then an MBA at another, then work somewhere new "
    "in a new city; that is the SAME person. "
    "Confirm identity when the full name matches AND at least one STABLE "
    "corroborator lines up: a school they attended (past or present), an employer "
    "they have worked at (past or present), or an explicit cross-link. Given name "
    "+ one such corroborator, score high (>= 0.85). Only treat it as a different "
    "person when a stable corroborator actively CONFLICTS (e.g. the lead's school "
    "does not appear anywhere and none of their employers overlap), or when the "
    "name is common and there is no corroborator at all. "
    "Judge name similarity, company (past OR present) match, SCHOOL/education "
    "(past OR present) match, location relevance, and role/keyword relevance. "
    "Produce an overall confidence score in [0,1] and a short reasoning, and "
    "extract the person's details as found on the page. Respond ONLY with a JSON object."
)
MATCH_SCHEMA = (
    '{"score": float, "name_match": bool, "company_match": bool, '
    '"school_match": bool, "location_match": bool, "role_match": bool, '
    '"reasoning": str, "person": {"name": str, "role": str, "company": str, '
    '"school": str, "location": str, "links": [str]}}'
)


def match_candidate(extracted: dict, candidate: dict, content: str,
                    trusted_note: str = "") -> dict:
    content = (content or "")[:8000]
    trusted = ""
    if trusted_note:
        trusted = (
            f"\nTRUSTED SIGNAL: {trusted_note} This came from LinkedIn's own "
            f"structured index, so treat that attribute as CONFIRMED (set the "
            f"matching signal true and score high) unless the page content "
            f"clearly contradicts it.\n"
        )
    user = (
        f"TARGET LEAD:\n{json.dumps(extracted, ensure_ascii=False)}\n"
        f"{trusted}\n"
        f"CANDIDATE PAGE:\n"
        f"- URL: {candidate.get('url','')}\n"
        f"- Type: {candidate.get('source_type','')}\n"
        f"- Title: {candidate.get('title','')}\n"
        f"- Content:\n{content}\n\n"
        f"Return JSON with exactly these keys: {MATCH_SCHEMA}."
    )
    out = chat_json(MATCH_SYS, user)
    if not isinstance(out, dict):
        out = {}
    try:
        out["score"] = max(0.0, min(1.0, float(out.get("score", 0))))
    except (TypeError, ValueError):
        out["score"] = 0.0
    return out
