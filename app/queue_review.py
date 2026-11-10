"""Review-queue agent: decides the leads waiting in review, so nobody has to.

Every cycle it looks at each lead in review and, following the rules in
review_rules.md (the team adds to that list over time):

- reject: the lead gave a link that has nothing to do with them (someone
  else's profile, an unrelated site, junk), or the lead is spam / a test;
- accept: the profile chosen for the lead is theirs and its score is good
  (at least REVIEW_AGENT_ACCEPT_SCORE): it gets its AE like a reviewer's Accept;
- leave: anything unclear stays in review for a person.

The LLM only proposes; the code accepts only with a good score and a confident
call, and rejects only on a confident call. Each lead is decided once per
version of the rules (meta "rq_done": lead id -> rules hash), so adding a rule
makes it look at the leads still in review again. Every decision is logged
(meta "rq_log") with the reason.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
import time

from . import config, db
from .pipeline import distribution, llm

_RULES_FILE = pathlib.Path(__file__).with_name("review_rules.md")
_lock = threading.Lock()

_SYSTEM = """You review sales leads for a US immigration (O-1 / EB-1 / EB-2 NIW visa) firm.
Each lead filled in a form (name, email, phone, company, visa, message, and a
"LinkedIn" field). An automated search then picked the online profile it thinks
is the lead's own and scored how sure it is (0-1). These leads were left for a
person to decide; you decide instead, following the team's rules.

First, classify what the lead put in the LinkedIn field ("given_link"):
- "theirs": a link to a page about them: their LinkedIn, their own site,
  portfolio, company, Instagram, a page that names them (even if broken or typed badly).
- "someone_else": a link to a profile or page about a DIFFERENT person.
- "unrelated": a link (a real URL) to something that has nothing to do with them
  (an unrelated site, a mail inbox, a search page, a random page).
- "not_a_link": text that isn't a link: their name, an email address, a job
  description, a company name, "N/A", "I don't have it", a word or two.
- "none": blank.
- "unclear": you can't tell.
Only "someone_else" and "unrelated" are bad links. Writing text instead of a
link is NOT a bad link.

Then "spam": true only if the whole lead is plainly fake or a test: a
random-letter name and gibberish details, keyboard mashing, "test test".

Then the verdict:
- "reject": the team's rules say to reject it (a bad link, or spam).
- "accept": the chosen profile is clearly the lead's own (name and details line
  up, no sign it is someone else) and nothing in the rules says otherwise.
- "leave": anything else, or when you're not sure. When in doubt, leave.

Reply with JSON only:
{"given_link": "theirs" | "someone_else" | "unrelated" | "not_a_link" | "none" | "unclear",
 "spam": true | false,
 "verdict": "reject" | "accept" | "leave",
 "confidence": 0-1 (how sure you are of the verdict),
 "reason": "one or two plain sentences a sales manager would understand"}"""


def rules() -> str:
    try:
        return _RULES_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def _rules_hash() -> str:
    return hashlib.sha1((rules() + _SYSTEM).encode()).hexdigest()[:12]


def _meta(key: str, default):
    try:
        return json.loads(db.meta_get(key, "") or "null") or default
    except ValueError:
        return default


def _brief(lead: dict) -> str:
    chosen = lead.get("chosen") or {}
    cands = sorted(lead.get("candidates") or [], key=lambda c: -(float(c.get("score") or 0)))[:4]
    lines = [
        "THE LEAD, as they filled it in:",
        f"  name: {lead.get('name') or ''}",
        f"  email: {lead.get('email') or ''}",
        f"  phone: {lead.get('phone') or ''}",
        f"  company: {lead.get('company') or ''}",
        f"  visa: {lead.get('visa') or ''}",
        f"  message: {(lead.get('message') or '')[:800]}",
        f"  what they put in the LinkedIn field: {lead.get('link_raw') or '(blank)'}",
        "",
        f"CHOSEN PROFILE (score {float(lead.get('confidence') or 0):.2f}): "
        + (f"{chosen.get('url')}  — {(chosen.get('reasoning') or '')[:700]}" if chosen else "none found"),
        f"SEARCH NOTES: {(lead.get('reasoning') or '')[:900]}",
        "OTHER CANDIDATES:",
    ]
    lines += [f"  - {c.get('url')} (score {float(c.get('score') or 0):.2f}): {(c.get('reasoning') or '')[:250]}"
              for c in cands if c.get("url") != chosen.get("url")] or ["  (none)"]
    return "\n".join(lines)


def judge(lead: dict) -> dict:
    """The LLM's call on one review lead, made safe: what the code will do."""
    user = "THE TEAM'S RULES (follow them):\n" + rules() + "\n\n" + _brief(lead)
    try:
        j = llm.chat_json(_SYSTEM, user, max_tokens=400)
    except Exception as e:  # noqa: BLE001 — no answer means a person decides
        return {"action": "leave", "reason": f"Review agent couldn't decide: {str(e)[:150]}"}
    verdict = str(j.get("verdict", "leave")).lower()
    try:
        conf = float(j.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    score = float(lead.get("confidence") or 0)
    reason = str(j.get("reason") or "").strip()
    given = str(j.get("given_link") or "").lower()
    raw = (lead.get("link_raw") or "").strip()
    # A bad link must be an actual link (not an email or a few words).
    is_url = "@" not in raw and " " not in raw and bool(re.search(r"https?:|www\.|\.[a-z]{2,}/|/in/", raw, re.I))
    bad_link = given in ("someone_else", "unrelated") and is_url
    spam = j.get("spam") is True
    action = "leave"
    if verdict == "reject" and ((bad_link and conf >= 0.8) or (spam and conf >= 0.9)):
        action = "reject"
    elif verdict == "reject":
        reason = "Not rejected: only a link that isn't theirs, or a spam lead, is. " + reason
    elif verdict == "accept" and conf >= 0.7 and lead.get("chosen") \
            and score >= config.REVIEW_AGENT_ACCEPT_SCORE:
        action = "accept"
    elif verdict == "accept":
        reason = (f"Looks like theirs, but the score ({score:.2f}) or the call isn't strong "
                  f"enough to accept without a person. " + reason)
    return {"action": action, "verdict": verdict, "given_link": given, "spam": spam,
            "confidence": conf, "score": score, "reason": reason}


def run(live: bool = True, only_new: bool = True) -> dict:
    """Decide the leads in review. live=False only plans. Returns counts and the plan."""
    out = {"accepted": 0, "rejected": 0, "left": 0, "plan": []}
    if not config.REVIEW_AGENT_QUEUE or not _lock.acquire(blocking=False):
        return out
    try:
        h = _rules_hash()
        done = _meta("rq_done", {})
        log = _meta("rq_log", [])
        for lead in db.list_leads("review"):
            if only_new and done.get(str(lead["id"])) == h:
                continue
            lead = db.get_lead(lead["id"]) or lead          # with candidates
            d = judge(lead)
            out["plan"].append({"lead": lead["id"], "name": lead.get("name"), **d})
            if live:
                if d["action"] == "accept":
                    fields = {} if lead.get("owner") else {"owner": distribution.assign_owner(lead)}
                    db.update_lead(lead["id"], status="accepted",
                                   reasoning="Accepted by review agent: " + d["reason"] + " "
                                             + (lead.get("reasoning") or ""), **fields)
                elif d["action"] == "reject":
                    db.update_lead(lead["id"], status="rejected", rejected_at=time.time(),
                                   reasoning="Rejected by review agent: " + d["reason"],
                                   **({} if lead.get("pushed") else {"owner": None}))
                done[str(lead["id"])] = h
                log.insert(0, {"at": time.time(), "lead": lead["id"], "name": lead.get("name"), **d})
            out[{"accept": "accepted", "reject": "rejected"}.get(d["action"], "left")] += 1
        if live:
            db.meta_set("rq_done", json.dumps(done))
            db.meta_set("rq_log", json.dumps(log[:2000]))
            db.meta_set("rq_last", json.dumps({k: out[k] for k in ("accepted", "rejected", "left")}
                                              | {"at": time.time()}))
        return out
    finally:
        _lock.release()


def last() -> dict:
    return {"last": _meta("rq_last", {}), "log": _meta("rq_log", [])[:200], "rules": rules()}
