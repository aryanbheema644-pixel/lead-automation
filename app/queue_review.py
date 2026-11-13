"""Review-queue agent: reads every lead in review and leaves its suggestion
for the person who decides (the team wants maximum human review).

It changes a lead itself in one case only: the lead is plainly spam, a scam or
a test (random letters, keyboard mashing, "test test", a sales pitch or SEO /
guest-post offer instead of someone asking about a visa). Those are rejected.

Everything else stays in review. The agent writes a suggestion on the lead
("agent_note": accept / reject / unsure, with the reason, e.g. "the LinkedIn
they gave is someone else's"), which the Review page shows next to Accept and
Reject. A person makes the call.

Each lead is read once per version of the rules (review_rules.md + the prompt):
adding a rule makes it read the leads in review again.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
import time

from . import config, db
from .pipeline import llm

_RULES_FILE = pathlib.Path(__file__).with_name("review_rules.md")
_lock = threading.Lock()

_SYSTEM = """You help a person review sales leads for a US immigration (O-1 / EB-1 / EB-2 NIW visa) firm.
Each lead filled in a form (name, email, phone, company, visa, message, and a
"LinkedIn" field). An automated search then picked the online profile it thinks
is the lead's own and scored how sure it is (0-1). A person decides each lead;
you give them a short, useful suggestion, following the team's rules.

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

Then "spam": true only if the lead is plainly not a real person asking about
immigration: random-letter name and gibberish details, keyboard mashing,
"test test", or a scam / sales pitch (selling SEO, guest posts, marketing,
crypto; a bot message). A real person with a sparse, odd or off-topic message
is NOT spam.

Then "other_person": true only if the chosen profile is clearly a DIFFERENT
person from the lead (wrong name, or details that plainly contradict theirs).
Not being able to fully confirm it is not "other_person".

Then your suggestion for the person deciding:
- "accept": a real lead; the profile (if any) could well be theirs, or no
  profile but nothing wrong with the lead.
- "reject": a reason to drop it (spam/scam, a bad link that isn't theirs).
- "unsure": you can't tell.

Reply with JSON only:
{"given_link": "theirs" | "someone_else" | "unrelated" | "not_a_link" | "none" | "unclear",
 "spam": true | false,
 "other_person": true | false,
 "suggest": "accept" | "reject" | "unsure",
 "confidence": 0-1 (how sure you are),
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
    """The LLM's read of one review lead: what the code will do (reject only
    plain spam / scams / tests; otherwise leave it with a suggestion)."""
    user = "THE TEAM'S RULES (follow them):\n" + rules() + "\n\n" + _brief(lead)
    try:
        j = llm.chat_json(_SYSTEM, user, max_tokens=400)
    except Exception as e:  # noqa: BLE001 — no answer: a person decides, and we try again
        return {"action": "leave", "reason": f"Review agent couldn't read it: {str(e)[:150]}"}
    suggest = str(j.get("suggest") or j.get("verdict") or "unsure").lower()
    try:
        conf = float(j.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    spam = j.get("spam") is True
    reason = str(j.get("reason") or "").strip()
    given = str(j.get("given_link") or "").lower()
    action = "reject" if spam and conf >= 0.9 else "leave"
    return {"action": action, "suggest": suggest if suggest in ("accept", "reject", "unsure") else "unsure",
            "verdict": suggest, "given_link": given, "spam": spam,
            "other_person": j.get("other_person") is True, "confidence": conf,
            "score": float(lead.get("confidence") or 0), "reason": reason}


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
                if d["action"] == "reject":
                    db.update_lead(lead["id"], status="rejected", rejected_at=time.time(),
                                   reasoning="Rejected by review agent (spam / scam / test): " + d["reason"],
                                   **({} if lead.get("pushed") else {"owner": None}))
                elif d.get("verdict"):
                    db.update_lead(lead["id"], agent_note={"suggest": d["suggest"], "reason": d["reason"],
                                                           "confidence": d["confidence"], "at": time.time()})
                if d.get("verdict"):                  # no answer: try again next cycle
                    done[str(lead["id"])] = h
                log.insert(0, {"at": time.time(), "lead": lead["id"], "name": lead.get("name"), **d})
            out["rejected" if d["action"] == "reject" else "left"] += 1
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
