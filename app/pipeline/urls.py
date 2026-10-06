"""Deterministic URL clean-up, applied to every profile link we store or push.

The LLM (llm.clean_links) repairs things only judgment can fix — typos like
"instagram.co", "@handle (Instagram)", spaces inside a URL. This then makes the
result uniform: https scheme, lowercase host, tracking parameters stripped,
no trailing slash, and LinkedIn in its canonical /in/<slug> form.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .linkedin import canonical_linkedin

# Query parameters that only track the click/share, never identify the page.
_TRACKING = {"fbclid", "gclid", "igsh", "igshid", "si", "ref", "ref_", "ref_src",
             "trk", "mibextid", "rdid", "share_id", "feature", "_rdc", "_rdr"}
_TRIM = " .,;)]'\"<>"


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING


def canonical_url(url: str) -> str:
    """Normalize a profile URL; '' if it isn't a usable URL at all."""
    u = (url or "").strip().strip(_TRIM)
    if not u:
        return ""
    if re.search(r"linked[il1]n\.com", u, re.I):
        return canonical_linkedin(u)
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    host = p.netloc.lower()
    if "." not in host or " " in host:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _is_tracking(k)])
    return urlunsplit(("https", host, p.path.rstrip("/"), query, ""))
