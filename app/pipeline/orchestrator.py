"""Runs the full pipeline for a single lead and persists progress.

Flow (matches the diagram):
  2 extract -> 3 queries -> 4 search(Tavily) -> 5 classify -> 6 acquire content
  (Tavily-first / Firecrawl-or-Apify fallback) -> 7 match(LLM) -> 8 decide -> 9 store
"""
from __future__ import annotations

import threading

from .. import config, db
from . import llm
from .classify import classify
from .decision import decide
from .fetch import acquire_content
from .linkedin import search_by_name
from .search import linkedin_search, search_many

# One worker at a time keeps rate limits and cost predictable.
_worker_lock = threading.Lock()
_worker_running = False


def _set_stage(lead_id: int, stage: str) -> None:
    db.update_lead(lead_id, stage=stage, status="processing")


def process_lead(lead_id: int) -> None:
    lead = db.get_lead(lead_id)
    if not lead:
        return
    try:
        # Step 2 — extract & normalize
        _set_stage(lead_id, "extracting")
        extracted = llm.extract_fields(lead)
        db.update_lead(lead_id, extracted=extracted)

        # Step 3 — generate queries
        _set_stage(lead_id, "querying")
        queries = llm.generate_queries(extracted)
        db.update_lead(lead_id, queries=queries)

        # Step 4 — search
        _set_stage(lead_id, "searching")
        results = search_many(queries, cap=config.MAX_RESULTS_PER_LEAD)

        # ── LinkedIn handling ────────────────────────────────────────────
        # LinkedIn profiles need special care: they rank low in general search
        # (login-walled) AND the correct profile may be found by the *general*
        # query but not a domain-restricted one (or vice-versa). So we gather
        # LinkedIn candidates from three sources and guarantee they get matched,
        # letting the LLM matcher disambiguate same-name people.
        def _is_li_profile(u: str) -> bool:
            u = u.lower()
            return "linkedin.com" in u and "/in/" in u

        gen_li = [r for r in results if _is_li_profile(r["url"])]
        gen_other = [r for r in results if not _is_li_profile(r["url"])]

        # 1) URL the lead already provided (highest trust)
        provided_hits = []
        provided_li = (lead.get("linkedin") or "").strip()
        if provided_li.startswith("http") and "linkedin.com" in provided_li.lower():
            provided_hits = [{"url": provided_li, "title": "Provided LinkedIn URL",
                              "snippet": "", "raw_content": ""}]

        # 2) LinkedIn from the general (name+company+role) results — high precision
        # 3) LinkedIn discovery via the Apify search actor (queries LinkedIn's own
        #    index; "Full" mode returns profile data inline). Falls back to a
        #    Tavily domain-restricted pass if the search actor isn't configured.
        # If the lead said they have no LinkedIn, do NO LinkedIn searching of any
        # kind: skip discovery AND drop any LinkedIn URLs the general web search
        # surfaced. Other enrichment (personal site, company, news, ...) still runs.
        if lead.get("li_optout") and not provided_hits:
            lane_li = []
            gen_li = []
        else:
            lane_li = search_by_name(extracted)
            if not lane_li:
                lane_li = linkedin_search(extracted)

        # Merge with dedup, preserving priority order:
        # provided URL > Apify search (reliable, has data) > Tavily-found LinkedIn.
        seen, li_ordered = set(), []
        for r in provided_hits + lane_li + gen_li:
            key = r["url"].rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                li_ordered.append(r)
        li_added = li_ordered[: config.MAX_LINKEDIN_CANDIDATES]

        # LinkedIn candidates go first (each fetched via Apify), then general.
        results = li_added + gen_other

        # Step 5 — classify
        for res in results:
            res["source_type"] = classify(res["url"], extracted.get("company", ""))

        # Steps 6 + 7 — acquire content and match (capped for cost)
        _set_stage(lead_id, "matching")
        candidates: list[dict] = []
        match_cap = config.MAX_CANDIDATES_MATCHED + len(li_added)
        for res in results[:match_cap]:
            content, content_src = acquire_content(res)
            match = llm.match_candidate(extracted, res, content,
                                        trusted_note=res.get("_matched_note", ""))
            person = match.get("person", {}) if isinstance(match, dict) else {}
            candidates.append(
                {
                    "url": res["url"],
                    "title": res["title"],
                    "snippet": res.get("snippet", ""),
                    "source_type": res["source_type"],
                    "content_source": content_src,
                    "score": match.get("score", 0.0),
                    "reasoning": match.get("reasoning", ""),
                    "signals": {
                        "name": match.get("name_match"),
                        "company": match.get("company_match"),
                        "school": match.get("school_match"),
                        "location": match.get("location_match"),
                        "role": match.get("role_match"),
                    },
                    "person": person,
                }
            )

        # Display order: highest match score first (best match on top). The
        # decision below still applies LinkedIn priority when choosing the profile.
        candidates.sort(key=lambda c: c.get("score", 0.0) or 0.0, reverse=True)

        # Step 8 — decide (LinkedIn-priority logic; returns the chosen profile)
        status, confidence, reasoning, chosen = decide(candidates)
        db.update_lead(
            lead_id,
            candidates=candidates,
            status=status,
            stage="done",
            confidence=confidence,
            reasoning=reasoning,
            chosen=chosen,
            error=None,
        )
    except Exception as e:  # noqa: BLE001 — surface any failure on the lead itself
        db.update_lead(lead_id, status="error", stage="failed", error=str(e))


def _drain_queue() -> None:
    global _worker_running
    try:
        while True:
            ids = db.queued_lead_ids()
            if not ids:
                break
            for lead_id in ids:
                process_lead(lead_id)
    finally:
        with _worker_lock:
            _worker_running = False


def start_worker() -> bool:
    """Start the background worker if not already running. Returns True if started."""
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return False
        _worker_running = True
    threading.Thread(target=_drain_queue, daemon=True).start()
    return True


def is_running() -> bool:
    return _worker_running
