"""Cross-channel duplicate handling.

The same person often fills in more than one form (e.g. Meta, then Website),
sometimes with a work email in one and a personal one in the other.

  SAME     — same email, same LinkedIn profile, or same phone
                                                  -> merged automatically into the
             existing lead: info combined, channel upgraded by priority
             (Meta > Website > WhatsApp); one row in the Pipedrive sheet, one
             count in the GTM report.
  DOUBTFUL — the same or a similar name (e.g. extra/middle names) but a
             different email/phone — a name alone is never trusted
                                                  -> both kept, flagged (dup_of),
             and BOTH held back from the Pipedrive sheet until a human decides on
             the Duplicates page.
"""
from __future__ import annotations

import re
import threading
import unicodedata

from . import db
from .pipeline.linkedin import canonical_linkedin

_lock = threading.Lock()

# Which channel a merged person is attributed to: lower wins.
_CHANNEL_RANK = {"Meta": 0, "Website": 1, "WhatsApp": 2}


def norm_email(e: str) -> str:
    e = (e or "").strip().lower().replace("&", "@").replace(" ", "")   # "name&gmail.com" typos
    return e if re.fullmatch(r"[^@]+@[^@]+\.[^@]+", e) else ""


def _li(url: str) -> str:
    if not url or "linkedin" not in url.lower():
        return ""
    return (canonical_linkedin(url) or "").rstrip("/").lower()


def linkedins(l: dict) -> set[str]:
    out = {_li(l.get("linkedin") or "")}
    chosen = l.get("chosen") or {}
    out.add(_li(chosen.get("url") or ""))
    return out - {""}


def _name_tokens(n: str) -> list[str]:
    n = unicodedata.normalize("NFKD", n or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    return [t for t in re.split(r"[^a-z]+", n) if len(t) > 1]


def names_equal(a: str, b: str) -> bool:
    """Same full name ignoring case/accents/punctuation ('Parag dave' = 'Parag Dave').
    Needs at least two name words — a lone first name is too weak."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    return len(ta) >= 2 and ta == tb


def names_match(a: str, b: str) -> bool:
    """'Parag Dave' ~ 'parag dave'; 'Juan Carlos Chirinos' ~ 'Juan Carlos Chirinos
    Martinez'. Needs at least two name words — a lone first name is too weak."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    short, long_ = sorted((ta, tb), key=len)
    return len(short) >= 2 and set(short) <= set(long_)


def phone_key(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 8 else ""


def _candidates(lead: dict) -> list[dict]:
    """Leads this one could duplicate: everything else that isn't itself merged away."""
    return [o for o in db.list_leads() if o["id"] != lead["id"] and o.get("status") != "duplicate"]


def find_match(lead: dict) -> tuple[str, dict | None, str]:
    """('certain'|'possible'|'', other_lead, reason). Prefers the oldest match."""
    others = sorted(_candidates(lead), key=lambda o: o["id"])
    em, lis = norm_email(lead.get("email", "")), linkedins(lead)
    ph, name = phone_key(lead.get("phone", "")), lead.get("name", "")
    for o in others:
        if em and norm_email(o.get("email", "")) == em:
            return "certain", o, "same email"
        if lis and lis & linkedins(o):
            return "certain", o, "same LinkedIn profile"
        if ph and phone_key(o.get("phone", "")) == ph:
            return "certain", o, "same phone number"
    for o in others:
        if names_equal(name, o.get("name", "")):
            return "possible", o, "same name, different email/phone"
        if names_match(name, o.get("name", "")):
            return "possible", o, "similar name, different email/phone"
    return "", None, ""


def _keep_order(a: dict, b: dict) -> tuple[dict, dict]:
    """(kept, merged_away). Keep the one already in the Pipedrive sheet; if both
    are, keep the higher-priority channel (Meta > Website > WhatsApp); else the
    earlier lead."""
    if bool(a.get("pushed")) != bool(b.get("pushed")):
        return (a, b) if a.get("pushed") else (b, a)
    if a.get("pushed"):
        ra, rb = _CHANNEL_RANK.get(a.get("channel") or "", 9), _CHANNEL_RANK.get(b.get("channel") or "", 9)
        if ra != rb:
            return (a, b) if ra < rb else (b, a)
    return (a, b) if a["id"] < b["id"] else (b, a)


def merge(primary_id: int, dup_id: int, reason: str) -> None:
    """Fold lead `dup_id` into `primary_id` and retire it as status 'duplicate'."""
    with _lock:
        p, d = db.get_lead(primary_id), db.get_lead(dup_id)
        if not p or not d or p["id"] == d["id"]:
            return
        fields: dict = {}
        for f in ("email", "phone", "company", "visa", "linkedin", "other_link", "link_raw", "lead_date"):
            if not (p.get(f) or "").strip() and (d.get(f) or "").strip():
                fields[f] = d[f]
        # Channel priority for the merged lead: Meta > Website > WhatsApp > others
        # (founder's rule). Drives the Source column and the GTM channel row.
        if _CHANNEL_RANK.get(d.get("channel") or "", 9) < _CHANNEL_RANK.get(p.get("channel") or "", 9):
            fields["channel"] = d["channel"]
        notes = []
        if d.get("email") and p.get("email") and norm_email(d["email"]) != norm_email(p["email"]):
            notes.append(f"Other email: {d['email']}")
        if d.get("phone") and p.get("phone") and phone_key(d["phone"]) != phone_key(p["phone"]):
            notes.append(f"Other phone: {d['phone']}")
        msg = (d.get("message") or "").strip()
        if msg and msg not in (p.get("message") or ""):
            notes.append(f"[{d.get('channel') or 'Other'} form] {msg}")
        if notes:
            base = (p.get("message") or "").strip()
            fields["message"] = (base + "\n\n" if base else "") + "\n".join(notes)
        # The duplicate verified the person better -> the merged lead takes it over.
        if d.get("status") == "accepted" and p.get("status") in ("rejected", "review") and d.get("chosen"):
            for f in ("chosen", "candidates", "screening", "screened", "confidence", "reasoning", "extracted"):
                fields[f] = d.get(f)
            fields["status"] = "accepted"
        # The first lead never found a LinkedIn but the duplicate provided one ->
        # re-run the first one on it (fast path: scrape + screen).
        elif (fields.get("linkedin") and not linkedins(p) and d.get("status") in ("queued", "duplicate")
              and p.get("status") in ("accepted", "review", "rejected", "error")):
            fields["status"] = "queued"
        fields["merged_from"] = list(p.get("merged_from") or []) + [d["id"]]
        if p.get("dup_of") == d["id"]:
            fields.update(dup_of=None, dup_reason=None)
        db.update_lead(p["id"], **fields)
        db.update_lead(d["id"], status="duplicate", stage="merged", merged_into=p["id"],
                       dup_of=None, dup_reason=f"Merged into #{p['id']} — {reason}",
                       owner=d.get("owner") if d.get("pushed") else None)
        # Anything flagged against the retired lead now points at the merged one.
        for o in db.list_leads():
            if o.get("dup_of") == d["id"]:
                db.update_lead(o["id"], dup_of=p["id"])


def check_new(lead_id: int) -> str:
    """Right after a lead comes in: merge it (certain) or flag it (possible)."""
    lead = db.get_lead(lead_id)
    if not lead:
        return ""
    kind, other, reason = find_match(lead)
    if kind == "certain":
        merge(other["id"], lead_id, reason)
    elif kind == "possible":
        db.update_lead(lead_id, dup_of=other["id"], dup_reason=reason)
    return kind


def check_processed(lead_id: int) -> bool:
    """After the pipeline found a LinkedIn: same profile as an earlier lead ->
    merge this one into it (also settles an open 'possible' flag)."""
    lead = db.get_lead(lead_id)
    if not lead or lead.get("status") == "duplicate" or not linkedins(lead):
        return False
    lis = linkedins(lead)
    for o in sorted(_candidates(lead), key=lambda o: o["id"]):
        if lis & linkedins(o):
            first, second = _keep_order(o, lead)
            merge(first["id"], second["id"], "same LinkedIn profile")
            return True
    return False


def resolve(lead_id: int, same: bool) -> None:
    """Human decision on a flagged pair."""
    lead = db.get_lead(lead_id)
    if not lead or not lead.get("dup_of"):
        return
    other = db.get_lead(lead["dup_of"])
    if same and other:
        first, second = _keep_order(other, lead)
        merge(first["id"], second["id"], f"confirmed same person ({lead.get('dup_reason') or 'flagged'})")
    else:
        db.update_lead(lead_id, dup_of=None, dup_reason="Checked: different people")


def scan_existing() -> dict:
    """One-time pass over leads that existed before duplicate handling."""
    out = {"merged": 0, "flagged": 0}
    for l in sorted(db.list_leads(), key=lambda x: x["id"]):
        cur = db.get_lead(l["id"])
        if not cur or cur.get("status") == "duplicate" or cur.get("dup_of"):
            continue
        earlier = {**cur}
        kind, other, reason = find_match(earlier)
        if not other or other["id"] > cur["id"]:
            continue                                   # only match against earlier leads
        if kind == "certain":
            first, second = _keep_order(other, cur)
            merge(first["id"], second["id"], reason)
            out["merged"] += 1
        elif kind == "possible":
            db.update_lead(cur["id"], dup_of=other["id"], dup_reason=reason)
            out["flagged"] += 1
    return out
