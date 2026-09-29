"""Client for the RAG screening console (ForRAG service).

Sends a profile JSON as a multipart file upload and returns the screening verdict.
The endpoint expects: POST <SCREENING_URL>?ingest=false with a file field named
"file" whose filename ends in .json. Response shape:
  {"answer": "<verdict text>", "candidate": "...", "cases": [...],
   "screening": {tier, confidence, vertical, best_path, backup_path,
                 criteria_met, key_strength, red_flag, flip_trigger,
                 matched_cases:[{case_id,name,score}]}, "ingested": false}
"answer" is the source of record; "screening" is convenience (may be null-filled).
"""
from __future__ import annotations

import json
import re

import httpx

from .. import config


class ScreeningError(RuntimeError):
    pass


def screen_profile(profile: dict) -> dict:
    """Screen one profile dict. Returns the raw response JSON."""
    if not config.SCREENING_URL:
        raise ScreeningError("SCREENING_URL is not set")
    headers = {}
    if config.SCREENING_API_KEY:
        headers["X-API-Key"] = config.SCREENING_API_KEY
    payload = json.dumps(profile, ensure_ascii=False).encode("utf-8")
    files = {"file": ("profile.json", payload, "application/json")}
    try:
        with httpx.Client(timeout=config.SCREENING_TIMEOUT) as client:
            r = client.post(config.SCREENING_URL, params={"ingest": "false"},
                            files=files, headers=headers)
    except httpx.HTTPError as e:
        raise ScreeningError(f"screening request failed: {e}") from e
    if r.status_code != 200:
        raise ScreeningError(f"screening {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise ScreeningError(f"screening returned non-JSON: {r.text[:200]}") from e


def _grab(answer: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}:\s*(.+)", answer)
    return m.group(1).split("|")[0].strip() if m else ""


def _parse_answer(answer: str) -> dict:
    """Fallback: parse the structured fields out of the verdict text (used until
    the endpoint returns the structured 'screening' object)."""
    tier = re.search(r"\b(NO-GO|NURTURE|REVIEW|GO)\b", answer)
    crit = re.search(r"Criteria Met:\s*(\d+\s*/\s*\d+)", answer)
    return {
        "tier": tier.group(1) if tier else "",
        "confidence": _grab(answer, "Confidence"),
        "vertical": _grab(answer, "Vertical"),
        "best_path": _grab(answer, "Best Path"),
        "backup_path": _grab(answer, "Backup Path"),
        "criteria_met": crit.group(1).replace(" ", "") if crit else "",
        "key_strength": _grab(answer, "Key Strength"),
        "red_flag": _grab(answer, "Red Flag"),
        "flip_trigger": _grab(answer, "Flip Trigger"),
    }


def summarize(result: dict) -> dict:
    """Flatten a screening response into flat fields for storage / the sheet.
    Prefers the structured 'screening' object; falls back to parsing 'answer'."""
    s = result.get("screening") or {}
    if not s:  # structured object not deployed yet -> parse the verdict text
        s = _parse_answer(result.get("answer", "") or "")
    cases = result.get("screening", {}).get("matched_cases") or result.get("cases") or []
    case_str = "; ".join(
        f"{c.get('name') or c.get('candidate_name','?')} ({c.get('score', c.get('rerank_score','?'))})"
        for c in cases[:5]
    )
    return {
        "tier": s.get("tier", ""),
        "confidence": s.get("confidence", ""),
        "vertical": s.get("vertical", ""),
        "best_path": s.get("best_path", ""),
        "backup_path": s.get("backup_path", ""),
        "criteria_met": s.get("criteria_met", ""),
        "key_strength": s.get("key_strength", ""),
        "red_flag": s.get("red_flag", ""),
        "flip_trigger": s.get("flip_trigger", ""),
        "matched_cases": case_str,
        "answer": result.get("answer", ""),
    }
