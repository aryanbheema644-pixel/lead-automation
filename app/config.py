"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from this file's package).
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Also load a single secrets file if one is mounted (e.g. a Render "Secret File"
# named `.env` at /etc/secrets/.env). This lets you keep ALL secrets — API keys
# AND the Google service-account JSON as a single-line value — in one file, which
# then takes precedence. Override the path with SECRET_ENV_PATH if needed.
_SECRET_ENV = os.getenv("SECRET_ENV_PATH", "/etc/secrets/.env")
if os.path.isfile(_SECRET_ENV):
    load_dotenv(_SECRET_ENV, override=True)

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


def _get_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _get(name, default).split(",") if x.strip()]


# AE lead distribution (Owner column). "Tina" = Agustina.
AE_ROUND_ROBIN = _get_list("AE_ROUND_ROBIN", "Vansh,Rahul,Agustina,Rocio")
AE_LATAM = _get_list("AE_LATAM", "Agustina,Rocio")   # LATAM round-robin (Tina/Rocio)
AE_PILOT = _get("AE_PILOT", "Rahul")                 # pilots (profession) -> Rahul
MAX_RESULTS_PER_LEAD = _get_int("MAX_RESULTS_PER_LEAD", 10)
MAX_CANDIDATES_MATCHED = _get_int("MAX_CANDIDATES_MATCHED", 5)
# How many LinkedIn profiles to match (on top of MAX_CANDIDATES_MATCHED, so
# LinkedIn never crowds out general candidates). Extra slots absorb same-name
# noise; the LLM matcher scores the wrong ones low.
MAX_LINKEDIN_CANDIDATES = _get_int("MAX_LINKEDIN_CANDIDATES", 3)
MIN_CONTENT_CHARS = _get_int("MIN_CONTENT_CHARS", 600)


# Screening console (RAG fit-evaluator, hosted by the ForRAG service).
# POST a profile JSON -> get a fit verdict. Base URL + key kept here so we can flip
# to the secured/HTTPS endpoint in one place once auth is deployed.
SCREENING_URL = _get("SCREENING_URL", "http://150.230.237.181/api/screen")
SCREENING_API_KEY = _get("SCREENING_API_KEY")          # X-API-Key header (blank for now)
SCREENING_ENABLED = _get("SCREENING_ENABLED", "true").lower() in ("1", "true", "yes")
SCREENING_TIMEOUT = _get_int("SCREENING_TIMEOUT", 60)


def screening_ready() -> bool:
    return bool(SCREENING_ENABLED and SCREENING_URL)


# Google Sheets (service-account). Read from a source sheet, write to a dest sheet.
# GOOGLE_SERVICE_ACCOUNT_JSON: the service-account key, as raw JSON or a file path.
GOOGLE_SERVICE_ACCOUNT_JSON = _get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SOURCE_SHEET_ID = _get("GOOGLE_SOURCE_SHEET_ID")   # sheet the leads come from
GOOGLE_SOURCE_TAB = _get("GOOGLE_SOURCE_TAB")             # tab name; blank = first tab
GOOGLE_DEST_SHEET_ID = _get("GOOGLE_DEST_SHEET_ID")       # sheet refined leads go to
GOOGLE_DEST_TAB = _get("GOOGLE_DEST_TAB")                 # tab name; blank = first tab


def sheets_ready() -> bool:
    return bool(GOOGLE_SERVICE_ACCOUNT_JSON)


def provider_status() -> dict:
    """Which integrations are configured — surfaced in the UI health panel."""
    return {
        "openrouter": bool(OPENROUTER_API_KEY),
        "model": OPENROUTER_MODEL,
        "tavily": bool(TAVILY_API_KEY),
        "firecrawl": bool(FIRECRAWL_API_KEY),
        "apify": bool(APIFY_API_TOKEN and APIFY_LINKEDIN_ACTOR),
        "screening": screening_ready(),
        "sheets": sheets_ready(),
        "source_sheet": bool(GOOGLE_SOURCE_SHEET_ID),
        "dest_sheet": bool(GOOGLE_DEST_SHEET_ID),
        "thresholds": {"accept": ACCEPT_THRESHOLD, "review": REVIEW_THRESHOLD},
    }
