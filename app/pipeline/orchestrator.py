"""Runs the full pipeline for a single lead and persists progress.

Flow (matches the diagram):
  2 extract -> 3 queries -> 4 search(Tavily) -> 5 classify -> 6 acquire content
  (Tavily-first / Firecrawl-or-Apify fallback) -> 7 match(LLM) -> 8 decide -> 9 store
"""
from __future__ import annotations

import threading

from .. import config, db
from . import distribution, llm, screening
from .classify import classify
from .decision import decide
from .fetch import acquire_content
from .linkedin import fetch_linkedin_raw, search_by_name
from .profile_view import to_pdf_view
from .search import linkedin_search, search_many


def _person_from_profile(p: dict) -> dict:
    """Derive display fields (for the sheet) from a scraped/trimmed profile."""
    exp = p.get("experience") or []
    company = exp[0].get("companyName") if exp and isinstance(exp[0], dict) else ""
    name = " ".join(x for x in [p.get("firstName"), p.get("lastName")] if x)
    return {
        "name": name,
        "role": p.get("headline") or "",
        "company": company or "",
        "location": p.get("location") or "",
        "links": [p.get("linkedinUrl")] if p.get("linkedinUrl") else [],
    }


def _screen_lead(lead_id: int, chosen: dict) -> dict | None:
    """Scrape the chosen LinkedIn profile and run it through the screening console.
    Returns a flat screening summary (or a note dict) and enriches `chosen.person`.
    Returns None if there's no LinkedIn URL to screen."""
    url = (chosen or {}).get("url", "")
    if not url or "linkedin.com" not in url.lower():
        return None
    if not config.screening_ready():
        return {"note": "screening not configured"}
    _set_stage(lead_id, "scraping profile")
    raw = fetch_linkedin_raw(url)
    if not raw:
        return {"note": "LinkedIn profile could not be scraped (empty); not screened."}
    profile = to_pdf_view(raw)
    chosen["person"] = _person_from_profile(profile)  # enrich sheet fields from real data
    _set_stage(lead_id, "screening")
    try:
        result = screening.screen_profile(profile)
    except screening.ScreeningError as e:
        return {"error": f"screening failed: {e}"}
    return screening.summarize(result)

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
        # Fast path: the lead already provided a LinkedIn URL -> no refinement
        # (no search/discovery). Go straight to screening on that URL.
        provided = (lead.get("linkedin") or "").strip()
        if provided.startswith("http") and "linkedin.com" in provided.lower():
            chosen = {"url": provided, "source_type": "linkedin (provided)", "person": {}}
            screen_summary = _screen_lead(lead_id, chosen)
            owner = distribution.assign_owner({
                "phone": lead.get("phone"), "message": lead.get("message"),
                "visa": lead.get("visa"), "extracted": {}, "chosen": chosen})
            db.update_lead(
                lead_id, status="accepted", stage="done", confidence=None,
                candidates=[{"url": provided, "source_type": "linkedin (provided)",
                             "score": None, "signals": {}, "person": chosen.get("person", {})}],
                chosen=chosen, screening=screen_summary, screened=1, owner=owner,
                reasoning="LinkedIn URL provided by the lead — no refinement needed.",
                error=None,
            )
            return

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

        # A non-LinkedIn link the lead gave us (Instagram, IMDb, Scholar, ...) is
        # verified like any other candidate rather than trusted blindly.
        other_link = (lead.get("other_link") or "").strip()
        provided_other = []
        if other_link:
            provided_other = [{"url": other_link, "title": "Link provided by the lead",
                               "snippet": "", "raw_content": "", "_provided": True}]
            gen_other = [r for r in gen_other
                         if r["url"].rstrip("/").lower() != other_link.rstrip("/").lower()]

        # LinkedIn candidates: from the general (name+company+role) results — high
        # precision — plus discovery via the Apify search actor (queries LinkedIn's
        # own index; "Full" mode returns profile data inline), falling back to a
        # Tavily domain-restricted pass if the search actor isn't configured.
        # If the lead said they have no LinkedIn, do NO LinkedIn searching of any
        # kind: skip discovery AND drop any LinkedIn URLs the general web search
        # surfaced. Other enrichment (personal site, company, news, ...) still runs.
        if lead.get("li_optout"):
            lane_li = []
            gen_li = []
        else:
            lane_li = search_by_name(extracted)
            if not lane_li:
                lane_li = linkedin_search(extracted)

        # Merge with dedup, preserving priority order:
        # Apify search (reliable, has data) > Tavily-found LinkedIn.
        seen, li_ordered = set(), []
        for r in lane_li + gen_li:
            key = r["url"].rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                li_ordered.append(r)
        li_added = li_ordered[: config.MAX_LINKEDIN_CANDIDATES]

        # LinkedIn candidates go first (each fetched via Apify), then the lead's
        # own link, then general results.
        results = li_added + provided_other + gen_other

        # Step 5 — classify
        for res in results:
            res["source_type"] = classify(res["url"], extracted.get("company", ""))

        # Steps 6 + 7 — acquire content and match (capped for cost)
        _set_stage(lead_id, "matching")
        candidates: list[dict] = []
        match_cap = config.MAX_CANDIDATES_MATCHED + len(li_added) + len(provided_other)
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
                    "provided": bool(res.get("_provided")),
                }
            )

        # Display order: highest match score first (best match on top). The
        # decision below still applies LinkedIn priority when choosing the profile.
        candidates.sort(key=lambda c: c.get("score", 0.0) or 0.0, reverse=True)

        # Step 8 — decide (LinkedIn-priority logic; returns the chosen profile)
        status, confidence, reasoning, chosen = decide(candidates)

        # The lead gave us their own link but nothing verified well enough: don't
        # reject someone over their own link — send it to review for a human.
        if status == "rejected" and provided_other:
            prov = next((c for c in candidates if c.get("provided")), None)
            if prov:
                status, chosen = "review", prov
                reasoning = ("The lead provided this link, but it couldn't be "
                             "confidently verified — please check. "
                             + (prov.get("reasoning") or ""))

        # Step 9 — screen the chosen profile through the RAG console. Only
        # LinkedIn profiles can be screened (_screen_lead skips anything else),
        # and leads who said they have no LinkedIn are never screened.
        screen_summary = (_screen_lead(lead_id, chosen)
                          if chosen and not lead.get("li_optout") else None)

        # Step 10 — assign the AE (Owner) per the distribution rules
        owner = distribution.assign_owner({
            "phone": lead.get("phone"), "message": lead.get("message"),
            "visa": lead.get("visa"), "extracted": extracted, "chosen": chosen or {}})

        db.update_lead(
            lead_id,
            candidates=candidates,
            status=status,
            stage="done",
            confidence=confidence,
            reasoning=reasoning,
            chosen=chosen,
            screening=screen_summary,
            screened=1 if screen_summary else 0,
            owner=owner,
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
