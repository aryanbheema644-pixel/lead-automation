"""AE lead distribution (the Owner column).

Rules (priority order):
  1. LATAM lead (by country)      -> AE_LATAM  (Agustina/Rocio)
  2. Pilot by profession          -> AE_PILOT  (Rahul)
  3. Everyone else                -> AE_ROUND_ROBIN
LATAM wins over pilot when both apply.

All three share ONE cycle: every lead an AE gets uses up their turn, so a
LATAM lead to Agustina means the next normal lead skips her. Each lead goes to
the allowed AE who has had the fewest turns (ties in rotation order), so totals
stay level however many leads are LATAM.
"""
from __future__ import annotations

import json
import re
import threading

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
                "airline transport pilot", "flight officer", "airline captain",
                "flight captain", "captain pilot")
# Licence abbreviations, as whole words only ("cpl" must not match inside words).
_PILOT_LICENCE_RE = re.compile(r"\b(?:atpl|cpl)\b", re.I)
# "Captain" alone is ambiguous (army, ship, team) — count it only with aviation context.
_CAPTAIN_RE = re.compile(r"\bcaptain\b", re.I)
_AVIATION_RE = re.compile(r"\b(?:airline|airlines|aviation|airbus|boeing|a320|b737|"
                          r"flight|aircraft|flying)\b", re.I)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def is_latam(lead: dict) -> bool:
    raw = (lead.get("phone") or "").strip()
    ph = _digits(raw)
    # A number written without "+" and 10 digits or fewer is a local number (e.g.
    # US "5512345678"), not one starting with a country code — don't read "55"
    # as Brazil. International numbers without "+" are 11+ digits.
    if ph and not raw.startswith("+") and len(ph) <= 10:
        ph = ""
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
    if any(t in blob for t in _PILOT_TERMS) or _PILOT_LICENCE_RE.search(blob):
        return True
    if _CAPTAIN_RE.search(blob) and _AVIATION_RE.search(blob):
        return True
    return bool(_PILOT_RE.search(blob))


def on_leave() -> set[str]:
    """AEs unticked in "Team on duty" (lower-cased names). Stored in the DB."""
    import json
    try:
        return {n.lower() for n in json.loads(db.meta_get("ae_on_leave", "[]") or "[]")}
    except (ValueError, TypeError):
        return set()


def set_on_leave(name: str, leave: bool) -> list[str]:
    import json
    off = on_leave()
    (off.add if leave else off.discard)(name.lower())
    db.meta_set("ae_on_leave", json.dumps(sorted(off)))
    return sorted(off)


_turns_lock = threading.Lock()


def _take_turn(allowed: list[str]) -> str:
    """The AE in `allowed` with the fewest turns this cycle (ties: the next one
    in rotation order after the last pick); records the turn. Turns are kept
    relative to the AEs on duty: the lowest is 0, and an AE coming back from
    leave starts level with the others rather than owed a pile of leads."""
    order = list(dict.fromkeys(config.AE_ROUND_ROBIN + config.AE_LATAM + [config.AE_PILOT]))
    on_duty = [n for n in order if n and n.lower() not in on_leave()]
    with _turns_lock:
        try:
            state = json.loads(db.meta_get("ae_turns", "{}") or "{}")
        except ValueError:
            state = {}
        turns = {n: state.get("turns", {}).get(n, 0) for n in on_duty}
        floor = min(turns.values(), default=0)
        turns = {n: (t if n in state.get("turns", {}) else floor) for n, t in turns.items()}
        last = state.get("last", "")
        start = order.index(last) + 1 if last in order else 0
        ranked = order[start:] + order[:start]
        pick = min((n for n in ranked if n in allowed),
                   key=lambda n: turns.get(n, floor), default="")
        if pick:
            turns[pick] = turns.get(pick, floor) + 1
            low = min(turns.values())
            db.meta_set("ae_turns", json.dumps({"turns": {n: t - low for n, t in turns.items()},
                                                "last": pick}))
        return pick


def move_turn(old: str, new: str) -> None:
    """A lead was reassigned by hand: the turn goes with it."""
    with _turns_lock:
        try:
            state = json.loads(db.meta_get("ae_turns", "{}") or "{}")
        except ValueError:
            return
        t = state.get("turns", {})
        if old in t and new in t:
            t[old] -= 1
            t[new] += 1
            low = min(t.values())
            state["turns"] = {n: v - low for n, v in t.items()}
            db.meta_set("ae_turns", json.dumps(state))


def assign_owner(lead: dict) -> str:
    """Pick the AE for a lead per the distribution rules (advances round-robin).
    AEs on leave are skipped: LATAM goes to whichever LATAM AE is on duty (else
    the normal rotation), pilots to the pilot AE if on duty (else the normal
    rotation). '' when nobody is on duty — the lead then waits without an AE."""
    off = on_leave()
    working = lambda names: [n for n in names if n and n.lower() not in off]
    latam = working(config.AE_LATAM)
    if is_latam(lead) and latam:
        return _take_turn(latam)
    if is_pilot(lead) and config.AE_PILOT and config.AE_PILOT.lower() not in off:
        return _take_turn([config.AE_PILOT])
    return _take_turn(working(config.AE_ROUND_ROBIN))
