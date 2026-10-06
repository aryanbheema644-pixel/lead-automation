"""LinkedIn profile fetch via an Apify actor (run synchronously).

The actor is configurable (APIFY_LINKEDIN_ACTOR) because Apify has several LinkedIn
scrapers with different input schemas. We send a permissive input and flatten
whatever the actor returns into readable text for the matcher.
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

import httpx

from .. import config

# First path segments that are real LinkedIn sections; anything else right after
# the domain ("linkedin.com/fabiancs") is a profile slug missing its "/in/".
_LI_SECTIONS = {"in", "company", "school", "pub", "feed", "posts", "jobs", "groups",
                "showcase", "events", "pulse", "learning", "sales", "talent"}
# Also tolerates the common "linkedln" typo.
_LI_HOST_RE = re.compile(r"linked[il1]n\.com(/[^\s?#,;\"'<>)\]]*)?", re.I)


def canonical_linkedin(url: str) -> str:
    """Normalize any LinkedIn URL to https://www.linkedin.com/in/<slug>.

    Fixes missing scheme/www, country subdomains (pe., bo.), a missing "/in/",
    query strings (?utm_…, ?trk=…), trailing segments (/en, /details/…), and
    percent-encoding. Returns '' when there is no profile path (e.g.
    a bare "linkedin.com"); non-LinkedIn URLs are returned unchanged.
    """
    m = _LI_HOST_RE.search(url or "")
    if not m:
        return url
    parts = [p for p in unquote(m.group(1) or "").split("/") if p]
    if parts and parts[0].lower() in ("m", "mwlite"):      # mobile prefixes
        parts = parts[1:]
    if not parts:
        return ""
    if parts[0].lower() not in _LI_SECTIONS:
        parts = ["in", parts[0]]
    if parts[0].lower() == "in":
        if len(parts) < 2:
            return ""
        # Keep case: vanity slugs are case-insensitive anyway, but internal ids
        # the search actor can return (/in/ACoAA…) are not.
        parts = ["in", parts[1]]
    return "https://www.linkedin.com/" + "/".join(parts)


def _slugify_school(name: str) -> str:
    """'New York University' -> 'new-york-university' (LinkedIn school slug form)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")


def _build_input(url: str) -> dict:
    """Build the actor input.

    Tuned for harvestapi/linkedin-profile-scraper (default), whose schema is:
      queries: [profile URLs or public identifiers]
      profileScraperMode: "Profile details no email ($4 per 1k)"
                        | "Profile details + email search ($10 per 1k)"
    We also send a few common aliases (urls / profileUrls / startUrls) so that
    swapping to another actor still mostly works without a code change.
    """
    mode = (
        "Profile details + email search ($10 per 1k)"
        if config.APIFY_LINKEDIN_EMAIL
        else "Profile details no email ($4 per 1k)"
    )
    return {
        # harvestapi fields
        "queries": [url],
        "profileScraperMode": mode,
        # aliases other actors commonly accept
        "urls": [url],
        "profileUrls": [url],
        "startUrls": [{"url": url}],
    }


def _format_profile(it: dict) -> str:
    """Build an identity-focused summary of a LinkedIn profile, with the stable
    corroborators (education, experience, location) FIRST so they survive the
    matcher's content truncation. The long 'about' blurb goes last, trimmed."""
    if not isinstance(it, dict):
        return _flatten(it)

    def g(k: str, d: str = "") -> str:
        return str(it.get(k) or d)

    name = f"{g('firstName')} {g('lastName')}".strip()
    loc = it.get("location")
    if isinstance(loc, dict):
        loc = loc.get("linkedinText") or (loc.get("parsed") or {}).get("text") or ""
    loc = loc or ""

    edu = []
    for e in (it.get("education") or []):
        if isinstance(e, dict):
            school = e.get("schoolName") or e.get("school") or ""
            deg = e.get("degree") or e.get("fieldOfStudy") or ""
            if school:
                edu.append(f"{school}{(' — ' + deg) if deg else ''}")

    exp = []
    for e in (it.get("experience") or []):
        if isinstance(e, dict):
            comp = e.get("companyName") or ""
            pos = e.get("position") or e.get("title") or ""
            entry = f"{pos} @ {comp}".strip(" @")
            if entry:
                exp.append(entry)

    lines = [
        f"Name: {name}",
        f"Headline: {g('headline')}",
        f"Location: {loc}",
        f"Education (all schools attended): {'; '.join(edu) if edu else '(none listed)'}",
        f"Experience (all employers): {'; '.join(exp[:10]) if exp else '(none listed)'}",
        f"Top skills: {g('topSkills')}",
        f"Profile URL: {g('linkedinUrl')}",
        f"About: {g('about')[:1200]}",
    ]
    return "\n".join(lines)


def _flatten(obj, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            s = _flatten(v, depth + 1)
            if s:
                parts.append(f"{k}: {s}")
        return "\n".join(parts)
    if isinstance(obj, list):
        return "\n".join(_flatten(v, depth + 1) for v in obj if v)
    return str(obj)


def fetch_linkedin(url: str) -> str:
    if not (config.APIFY_API_TOKEN and config.APIFY_LINKEDIN_ACTOR):
        return ""
    actor = config.APIFY_LINKEDIN_ACTOR.replace("/", "~")
    endpoint = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?token={config.APIFY_API_TOKEN}"
    )
    payload = _build_input(url)
    try:
        with httpx.Client(timeout=180) as client:
            r = client.post(endpoint, json=payload)
    except httpx.HTTPError:
        return ""
    if r.status_code not in (200, 201):
        return ""
    try:
        items = r.json()
    except json.JSONDecodeError:
        return ""
    if not items:
        return ""
    return _format_profile(items[0] if isinstance(items, list) else items)


def fetch_linkedin_raw(url: str, retries: int = 1):
    """Scrape a LinkedIn URL and return the RAW actor profile dict (for screening),
    or None if the actor returned nothing / only an error item. Retries once on
    empty, since harvestapi occasionally returns transient empties."""
    if not (config.APIFY_API_TOKEN and config.APIFY_LINKEDIN_ACTOR):
        return None
    from .profile_view import is_actor_error
    for attempt in range(retries + 1):
        items = _run_actor(config.APIFY_LINKEDIN_ACTOR, _build_input(url))
        for it in items:
            if isinstance(it, dict) and not is_actor_error(it) and (
                it.get("firstName") or it.get("headline") or it.get("experience")
            ):
                return it
    return None


def _run_actor(actor: str, payload: dict, timeout: int = 240) -> list:
    """Run an Apify actor synchronously and return dataset items."""
    actor = actor.replace("/", "~")
    endpoint = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?token={config.APIFY_API_TOKEN}"
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(endpoint, json=payload)
    except httpx.HTTPError:
        return []
    if r.status_code not in (200, 201):
        return []
    try:
        items = r.json()
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else [items]


def search_by_name(extracted: dict) -> list[dict]:
    """Discover LinkedIn profiles for a lead via the LinkedIn search actor.

    Uses name + company + title + location filters. In "Full" mode the actor
    returns the complete profile in the same call, so each hit already carries
    match-ready content (no separate profile scrape needed).

    Returns a list of candidate dicts shaped like search results, tagged with
    _source="apify_search" so fetch.acquire_content uses the content directly.
    """
    if not (config.APIFY_API_TOKEN and config.APIFY_LINKEDIN_SEARCH_ACTOR):
        return []
    name = (extracted.get("name") or "").strip()
    if not name:
        return []
    parts = name.split()
    first, last = parts[0], (" ".join(parts[1:]) if len(parts) > 1 else "")

    base = {
        "profileScraperMode": config.APIFY_LINKEDIN_SEARCH_MODE,
        "firstName": first,
        "lastName": last,
        "maxItems": config.APIFY_LINKEDIN_SEARCH_MAXITEMS,
        "maxPages": 1,
    }

    # Only use HIGH-CONFIDENCE structured filters. Fuzzy LLM guesses (role,
    # messy location strings) hurt recall — they can exclude the real profile.
    company = (extracted.get("company") or "").strip()
    school = (extracted.get("school") or "").strip()

    # Run one search per strong signal, then merge — narrower than sending all
    # filters at once (which over-constrains), broader than name-only (noisy).
    # Each search is tagged with the filter that produced it so the matcher can
    # trust that attribute (LinkedIn's index already confirmed name + filter).
    searches: list[tuple[dict, str]] = []
    if company:
        searches.append(({**base, "currentCompanies": [company]},
                         f"This profile matched a LinkedIn search for people named "
                         f"'{name}' who currently work at '{company}'."))
    if school:
        searches.append(({**base, "schools": [_slugify_school(school)]},
                         f"This profile matched a LinkedIn search for people named "
                         f"'{name}' who studied at '{school}'."))
    if not searches:
        searches.append((base, ""))  # nothing strong to filter on -> name only

    out: dict[str, dict] = {}
    for payload, note in searches:
        for it in _run_actor(config.APIFY_LINKEDIN_SEARCH_ACTOR, payload):
            url = it.get("linkedinUrl") or it.get("url") or ""
            if not url or url in out:
                continue
            out[url] = {
                "url": url,
                "title": it.get("headline")
                or f"{it.get('firstName','')} {it.get('lastName','')}".strip(),
                "snippet": it.get("headline", ""),
                "raw_content": _format_profile(it),
                "_source": "apify_search",
                "_matched_note": note,
            }
    return list(out.values())
