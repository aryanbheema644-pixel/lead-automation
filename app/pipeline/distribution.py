"""AE lead distribution (the Owner column).

Rules (priority order):
  1. LATAM lead (by country)      -> round-robin AE_LATAM  (Agustina/Rocio)
  2. Pilot by profession          -> AE_PILOT              (Rahul)
  3. Everyone else                -> round-robin AE_ROUND_ROBIN
LATAM wins over pilot when both apply.
"""
from __future__ import annotations

import re

from .. import config, db

# Latin-American calling codes (country-level).
_LATAM_CODES = {"52", "502", "503", "504", "505", "506", "507", "51", "54", "55",
                "56", "57", "58", "591", "593", "595", "598", "53", "592", "597"}
_DR_AREA = {"809", "829", "849"}  # Dominican Republic within +1
_LATAM_NAMES = [
    "mexico", "méxico", "brazil", "brasil", "argentina", "colombia", "chile",
    "peru", "perú", "venezuela", "ecuador", "bolivia", "paraguay", "uruguay",
    "guatemala", "honduras", "el salvador", "nicaragua", "costa rica", "panama",
    "panamá", "dominican republic", "cuba", "latin america", "latam",
    "south america", "central america",
]
_PILOT_RE = re.compile(
    r"\bpilot\b(?!\s+(?:program|study|project|test|phase|batch|wave|cohort|episode|season))",
    re.I)
_PILOT_TERMS = ("airline pilot", "commercial pilot", "first officer", "aviator",
                "airline transport pilot", "flight officer", "atpl", "cpl")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def is_latam(lead: dict) -> bool:
    ph = _digits(lead.get("phone", ""))
    if ph:
        if ph.startswith("1") and ph[1:4] in _DR_AREA:
            return True
        for code in sorted(_LATAM_CODES, key=len, reverse=True):
            if ph.startswith(code):
                return True
    ex = lead.get("extracted") or {}
    blob = f"{ex.get('location','')} {lead.get('message','')} {lead.get('visa','')}".lower()
    return any(n in blob for n in _LATAM_NAMES)


def is_pilot(lead: dict) -> bool:
    ex = lead.get("extracted") or {}
    person = (lead.get("chosen") or {}).get("person") or {}
    blob = (f"{lead.get('message','')} {ex.get('role_guess','')} "
            f"{person.get('role','')} {person.get('company','')}").lower()
    if any(t in blob for t in _PILOT_TERMS):
        return True
    return bool(_PILOT_RE.search(blob))


def assign_owner(lead: dict) -> str:
    """Pick the AE for a lead per the distribution rules (advances round-robin)."""
    if is_latam(lead) and config.AE_LATAM:
        return config.AE_LATAM[db.next_rr("latam", len(config.AE_LATAM))]
    if is_pilot(lead) and config.AE_PILOT:
        return config.AE_PILOT
    pool = config.AE_ROUND_ROBIN or ["Unassigned"]
    return pool[db.next_rr("normal", len(pool))]
