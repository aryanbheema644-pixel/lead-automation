"""Cheap, deterministic URL classification (step 5). No LLM call needed."""
from __future__ import annotations

from urllib.parse import urlparse

# domain substring -> source type
_RULES = [
    ("linkedin.com", "linkedin"),
    ("github.com", "github"),
    ("scholar.google.", "scholar"),
    ("researchgate.net", "scholar"),
    ("lattes.cnpq.br", "scholar"),
    ("instagram.com", "social"),
    ("facebook.com", "social"),
    ("tiktok.com", "social"),
    ("youtube.com", "social"),
    ("imdb.com", "imdb"),
    ("crunchbase.com", "crunchbase"),
    ("twitter.com", "social"),
    ("x.com", "social"),
    ("medium.com", "news"),
    ("substack.com", "news"),
]


def classify(url: str, company: str = "") -> str:
    host = (urlparse(url).netloc or "").lower()
    for needle, kind in _RULES:
        if needle in host:
            return kind
    # Company page heuristic: company name token appears in the domain.
    comp = "".join(ch for ch in (company or "").lower() if ch.isalnum())
    host_alnum = "".join(ch for ch in host if ch.isalnum())
    if comp and len(comp) >= 4 and comp in host_alnum:
        return "company"
    if url.lower().endswith(".pdf"):
        return "pdf"
    return "personal_or_other"
