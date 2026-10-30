"""Decision logic (step 8), with LinkedIn given priority.

Returns (status, confidence, reasoning, chosen_candidate).

Priority order:
  1. A confident LinkedIn match           -> accept it.
  2. A moderately-confident LinkedIn       -> review, LinkedIn chosen.
  3. A LinkedIn DISCOVERED via targeted    -> review, LinkedIn chosen (human
     search but not content-confirmed          verifies) rather than silently
                                               accepting a weaker source.
  4. No usable LinkedIn                     -> fall back to the best source.

Only a page that was actually opened can be ACCEPTED: a LinkedIn profile its
data was scraped for (Apify), any other page whose content was fetched. A
candidate judged from a search snippet alone (the link may be dead) goes to
review at most.
"""
from __future__ import annotations

from typing import Optional

from .. import config


def _sc(c: dict) -> float:
    try:
        return float(c.get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _opened(c: dict) -> bool:
    src = c.get("content_source")
    if c.get("source_type") == "linkedin":
        return src in ("apify", "apify_search")
    return src in ("apify", "apify_search", "tavily", "firecrawl")


_UNOPENED = "The page couldn't be opened, so this needs checking. "


def decide(candidates: list[dict]) -> tuple[str, float, str, Optional[dict]]:
    # Not finding someone online isn't a reason to drop the lead — sparse leads
    # (just a name, email and phone) often have no findable profile. Those go to
    # review; only a human rejects a lead.
    if not candidates:
        return "review", 0.0, ("No online profile found from the details given — "
                               "check manually (accept as-is, or reject)."), None

    lis = [c for c in candidates if c.get("source_type") == "linkedin"]
    best_li = max(lis, key=_sc) if lis else None
    best_any = max(candidates, key=_sc)

    # 1) Confident LinkedIn wins outright — if its profile could be opened.
    if best_li and _sc(best_li) >= config.ACCEPT_THRESHOLD and _opened(best_li):
        return "accepted", _sc(best_li), best_li.get("reasoning", ""), best_li

    # 2) Moderately-confident (or unopened) LinkedIn -> review, with it as the profile.
    if best_li and _sc(best_li) >= config.REVIEW_THRESHOLD:
        note = "" if _opened(best_li) else _UNOPENED
        return "review", _sc(best_li), note + best_li.get("reasoning", ""), best_li

    # 3) A LinkedIn was discovered via targeted (name-matched) search but not
    #    content-confirmed -> send to review with the LinkedIn to verify, instead
    #    of quietly accepting a weaker non-LinkedIn source.
    discovered = [
        c for c in lis
        if c.get("content_source") == "apify_search"
        and (c.get("signals") or {}).get("name")
    ]
    if discovered:
        d = max(discovered, key=_sc)
        return (
            "review",
            _sc(best_any),
            "A LinkedIn profile was discovered for this name via targeted search "
            "but could not be auto-confirmed against the lead — please verify. "
            f"Best non-LinkedIn source scored {_sc(best_any):.2f}.",
            d,
        )

    # 4) No usable LinkedIn -> fall back to the best source by threshold.
    if _sc(best_any) >= config.ACCEPT_THRESHOLD and _opened(best_any):
        return "accepted", _sc(best_any), best_any.get("reasoning", ""), best_any
    if _sc(best_any) >= config.REVIEW_THRESHOLD:
        note = "" if _opened(best_any) else _UNOPENED
        return "review", _sc(best_any), note + best_any.get("reasoning", ""), best_any
    return ("review", _sc(best_any),
            "No confident match online (best was %.2f) — check the candidates, or accept "
            "as-is / reject. %s" % (_sc(best_any), best_any.get("reasoning", "") or ""), None)
