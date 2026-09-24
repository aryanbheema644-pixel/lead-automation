"""Tavily web search. Returns results including raw page content when available."""
from __future__ import annotations

import httpx

from .. import config


class SearchError(RuntimeError):
    pass


def tavily_search(query: str, max_results: int = 5,
                  include_domains: list[str] | None = None) -> list[dict]:
    """Run one Tavily query. include_raw_content pulls page text in the same call
    so we can often skip a separate Firecrawl fetch (see fetch.py).
    include_domains restricts results to given domains (used for LinkedIn)."""
    if not config.TAVILY_API_KEY:
        raise SearchError("TAVILY_API_KEY is not set — add it to your .env")
    payload = {
        "api_key": config.TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "include_raw_content": True,
        "max_results": max_results,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post("https://api.tavily.com/search", json=payload)
    except httpx.HTTPError as e:
        raise SearchError(f"Tavily request failed: {e}") from e
    if r.status_code != 200:
        raise SearchError(f"Tavily {r.status_code}: {r.text[:300]}")
    data = r.json()
    out = []
    for item in data.get("results", []):
        out.append(
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("content", ""),  # Tavily's short snippet
                "raw_content": item.get("raw_content") or "",  # full page text (may be empty)
            }
        )
    return out


def linkedin_search(extracted: dict, max_results: int = 4) -> list[dict]:
    """Dedicated LinkedIn discovery pass, restricted to linkedin.com.

    LinkedIn profile pages rank low in general search (they are login-walled),
    so we give them their own domain-restricted query. The LLM matcher then
    disambiguates the many same-name profiles this can return.
    """
    name = (extracted.get("name") or "").strip()
    if not name:
        return []
    company = (extracted.get("company") or "").strip()
    location = (extracted.get("location") or "").strip()
    query = " ".join(p for p in [name, company, location] if p)
    try:
        results = tavily_search(query, max_results=max_results,
                                include_domains=["linkedin.com"])
    except SearchError:
        return []
    # Keep only actual profile pages (…/in/…), drop directory/company pages.
    return [r for r in results if "/in/" in r["url"].lower()]


def search_many(queries: list[str], cap: int) -> list[dict]:
    """Run several queries, dedupe by URL, keep the richest snippet per URL."""
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            results = tavily_search(q, max_results=5)
        except SearchError:
            continue
        for res in results:
            url = res["url"]
            if not url:
                continue
            if url not in seen:
                seen[url] = res
            else:
                # Prefer the entry that carries more raw content.
                if len(res["raw_content"]) > len(seen[url]["raw_content"]):
                    seen[url] = res
    return list(seen.values())[:cap]
