"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from this file's package).
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "leadsearch.db"


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# LLM (OpenRouter, OpenAI-compatible)
OPENROUTER_API_KEY = _get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_APP_URL = _get("OPENROUTER_APP_URL", "http://localhost:8000")
OPENROUTER_APP_NAME = _get("OPENROUTER_APP_NAME", "LeadSearch")

# External data providers
TAVILY_API_KEY = _get("TAVILY_API_KEY")
FIRECRAWL_API_KEY = _get("FIRECRAWL_API_KEY")
APIFY_API_TOKEN = _get("APIFY_API_TOKEN")
APIFY_LINKEDIN_ACTOR = _get("APIFY_LINKEDIN_ACTOR")
# When true, ask the actor to also find an email ($10/1k vs $4/1k on harvestapi).
APIFY_LINKEDIN_EMAIL = _get("APIFY_LINKEDIN_EMAIL", "false").lower() in ("1", "true", "yes")

# LinkedIn DISCOVERY (find the profile when no URL was provided): a search actor
# that queries LinkedIn's index by name+company+title. Far more reliable than
# hoping a web search surfaces the login-walled profile page.
APIFY_LINKEDIN_SEARCH_ACTOR = _get(
    "APIFY_LINKEDIN_SEARCH_ACTOR", "harvestapi/linkedin-profile-search-by-name"
)
# "Short" (URLs + basic data, cheapest) | "Full" (opens each profile for full
# data) | "Full + email search". "Full" gives us data to match on in one call.
APIFY_LINKEDIN_SEARCH_MODE = _get("APIFY_LINKEDIN_SEARCH_MODE", "Full")
APIFY_LINKEDIN_SEARCH_MAXITEMS = _get_int("APIFY_LINKEDIN_SEARCH_MAXITEMS", 5)

# Pipeline tuning
ACCEPT_THRESHOLD = _get_float("ACCEPT_THRESHOLD", 0.75)
REVIEW_THRESHOLD = _get_float("REVIEW_THRESHOLD", 0.45)
# LinkedIn gets ranking priority: a confident LinkedIn match (score >= review
# threshold) is ordered above non-LinkedIn candidates, even on a near-tie. This
# is ordering-only; the displayed score is unchanged. Wrong-person LinkedIn hits
# (low score) are NOT boosted, so they can't win.
LINKEDIN_BOOST = _get_float("LINKEDIN_BOOST", 0.15)
MAX_RESULTS_PER_LEAD = _get_int("MAX_RESULTS_PER_LEAD", 10)
MAX_CANDIDATES_MATCHED = _get_int("MAX_CANDIDATES_MATCHED", 5)
# How many LinkedIn profiles to match (on top of MAX_CANDIDATES_MATCHED, so
# LinkedIn never crowds out general candidates). Extra slots absorb same-name
# noise; the LLM matcher scores the wrong ones low.
MAX_LINKEDIN_CANDIDATES = _get_int("MAX_LINKEDIN_CANDIDATES", 3)
MIN_CONTENT_CHARS = _get_int("MIN_CONTENT_CHARS", 600)


def provider_status() -> dict:
    """Which integrations are configured — surfaced in the UI health panel."""
    return {
        "openrouter": bool(OPENROUTER_API_KEY),
        "model": OPENROUTER_MODEL,
        "tavily": bool(TAVILY_API_KEY),
        "firecrawl": bool(FIRECRAWL_API_KEY),
        "apify": bool(APIFY_API_TOKEN and APIFY_LINKEDIN_ACTOR),
        "thresholds": {"accept": ACCEPT_THRESHOLD, "review": REVIEW_THRESHOLD},
    }
