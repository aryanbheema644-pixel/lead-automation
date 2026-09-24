"""Content acquisition with the Tavily-first / Firecrawl-fallback logic.

Decision per candidate URL:
  * LinkedIn      -> Apify actor (if configured), else fall through to web fetch.
  * PDF / thin    -> Firecrawl (handles PDFs and heavy-JS pages, returns clean text).
  * otherwise     -> use Tavily's raw_content directly when it's substantial enough.
"""
from __future__ import annotations

import httpx

from .. import config
from .linkedin import fetch_linkedin


def _looks_heavy_js(raw: str) -> bool:
    """Very light heuristic: near-empty content usually means JS-rendered."""
    return len(raw.strip()) < config.MIN_CONTENT_CHARS


def firecrawl_scrape(url: str) -> str:
    """Fetch clean markdown/text for a page via Firecrawl."""
    if not config.FIRECRAWL_API_KEY:
        return ""
    headers = {
        "Authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    try:
        with httpx.Client(timeout=120) as client:
            r = client.post(
                "https://api.firecrawl.dev/v1/scrape", headers=headers, json=payload
            )
    except httpx.HTTPError:
        return ""
    if r.status_code != 200:
        return ""
    data = r.json().get("data", {})
    return data.get("markdown") or data.get("content") or ""


def acquire_content(candidate: dict) -> tuple[str, str]:
    """Return (content, source_of_content) for a classified candidate.

    source_of_content is one of: 'apify', 'tavily', 'firecrawl', 'none'.
    """
    url = candidate["url"]
    kind = candidate["source_type"]
    raw = candidate.get("raw_content", "") or ""

    # 0) Content already fetched by the LinkedIn search actor (Full mode) -> use it.
    if candidate.get("_source") == "apify_search" and raw:
        return raw, "apify_search"

    # 1) LinkedIn -> Apify actor when configured.
    if kind == "linkedin":
        li = fetch_linkedin(url)
        if li:
            return li, "apify"
        # fall through to generic web handling if Apify unavailable/failed

    # 2) PDFs and pages Tavily couldn't render -> Firecrawl fallback.
    if kind == "pdf" or _looks_heavy_js(raw):
        fc = firecrawl_scrape(url)
        if fc:
            return fc, "firecrawl"
        if raw:
            return raw, "tavily"
        return "", "none"

    # 3) Tavily content is sufficient -> use it directly (the primary path).
    if raw:
        return raw, "tavily"

    # 4) Nothing from Tavily -> last-chance Firecrawl.
    fc = firecrawl_scrape(url)
    if fc:
        return fc, "firecrawl"
    return candidate.get("snippet", ""), "tavily" if candidate.get("snippet") else "none"
