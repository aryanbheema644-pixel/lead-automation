"""LinkedIn review agent: opens every LinkedIn link in the Pipedrive sheet and
checks it's the lead's own profile, so nobody has to click through them by hand.

A link is good only when the page opens AND is the lead's own: the names clearly
match, or else the LLM judges it the same person (names in the sheet are messy).
When the LLM is unsure nothing changes and the row is listed for a person. Links
that aren't LinkedIn profiles (a personal site, Instagram, …) are skipped. A
bad link (dead, someone else's) gets its lead re-checked
(orchestrator.process_lead(recheck=True)): the profile is replaced by a verified
one or cleared, and the sheet follows. Nothing is written to Pipedrive — the
changed rows are listed so a person can press Write on Pipe.

Runs after a Push (manual, a cycle that added rows, or the 5am/8pm Pipe run) on
the rows that are new or whose link changed: links already confirmed are
remembered (meta "li_reviewed": email -> link). The button checks every row.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, sheets
from .pipeline import llm, orchestrator
from .pipeline.linkedin import _format_profile, canonical_linkedin, fetch_linkedin_raw

_lock = threading.Lock()


def _hkey(h: str) -> str:
    return " ".join((h or "").lower().split())


def _key(email: str, phone: str) -> str:
    return (email or "").strip().lower() or (phone or "").strip()


def status() -> dict:
    try:
        return json.loads(db.meta_get("li_review", "{}") or "{}")
    except json.JSONDecodeError:
        return {}


def is_running() -> bool:
    return _lock.locked()


def _clear_match(name: str, raw: dict) -> bool:
    """Fast pre-check: every part of the lead's name (2+ parts) is on the profile.
    Anything less — one name, a nickname, an email in the name field — goes to the LLM."""
    mine = orchestrator._name_tokens(name)
    theirs = orchestrator._name_tokens(f"{raw.get('firstName', '')} {raw.get('lastName', '')}")
    return len(mine) >= 2 and all(any(a.startswith(b) or b.startswith(a) for b in theirs) for a in mine)


def _lead_ctx(r: list, cell) -> dict:
    """What the LLM gets from the sheet row (the email's local part and the phone's
    country code often hold the real name and country)."""
    return {"name": cell(r, "name"), "email": cell(r, "email"), "phone": cell(r, "number"),
            "visa": cell(r, "visa intrested in"), "message": cell(r, "message")[:1500]}


def _check(url: str, lead: dict) -> dict:
    """Open one link and judge it. verdict: good | bad | unsure; why: dead |
    other_person | unsure. The page must open (Apify); the LLM
    judges identity unless the names clearly match."""
    raw = fetch_linkedin_raw(canonical_linkedin(url))
    if not raw:
        return {"verdict": "bad", "why": "dead"}
    who = f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip()
    if _clear_match(lead.get("name", ""), raw):
        return {"verdict": "good", "who": who}
    try:
        j = llm.same_person(lead, _format_profile(raw))
    except Exception as e:  # noqa: BLE001 — no judgement = a person decides
        j = {"same_person": "unsure", "confidence": 0, "reason": f"LLM check failed: {str(e)[:120]}"}
    out = {"who": who, "reason": j["reason"], "confidence": j["confidence"]}
    if j["same_person"] is True and j["confidence"] >= 0.7:
        return {**out, "verdict": "good"}
    if j["same_person"] is False and j["confidence"] >= 0.6:
        return {**out, "verdict": "bad", "why": "other_person"}
    return {**out, "verdict": "unsure", "why": "unsure"}


def run(only_new: bool = False, sync=None) -> dict:
    """Review the sheet's LinkedIn links. only_new: skip links already confirmed
    (the automation cycle). sync: called once if any lead changed, to bring the
    sheet up to date. Returns (and stores) the report; never two runs at once."""
    if not _lock.acquire(blocking=False):
        return status()
    rep = {"running": True, "started": time.time(), "only_new": only_new,
           "checked": 0, "good": 0, "changed": [], "left": []}
    db.meta_set("li_review", json.dumps(rep))
    try:
        ws, rows = sheets.open_dest()
        cols = {}
        for i, h in enumerate(rows[0] if rows else []):
            cols.setdefault(_hkey(h), i)
        ci = {k: cols.get(k) for k in ("name", "email", "number", "linkedin profile url",
                                       "visa intrested in", "message")}
        if ci["linkedin profile url"] is None:
            raise sheets.SheetsError("The Pipedrive sheet has no 'Linkedin Profile URL' column")
        cell = lambda r, k: (r[ci[k]].strip() if ci[k] is not None and ci[k] < len(r) else "")

        done = json.loads(db.meta_get("li_reviewed", "{}") or "{}")
        todo = []
        for n, r in enumerate(rows[1:], start=2):
            url = cell(r, "linkedin profile url")
            key = _key(cell(r, "email"), cell(r, "number"))
            if (key and canonical_linkedin(url).startswith("https://www.linkedin.com/in/")
                    and not (only_new and done.get(key) == canonical_linkedin(url))):
                todo.append((n, r, url, key))

        with ThreadPoolExecutor(4) as ex:
            verdicts = list(ex.map(lambda t: _check(t[2], _lead_ctx(t[1], cell)), todo))
        rep["checked"] = len(todo)

        leads = {}
        for l in db.list_leads():
            leads.setdefault(_key(l.get("email"), l.get("phone")), []).append(l)
        fixes = []                                   # sheet cells the sync won't rewrite
        for (n, r, url, key), v in zip(todo, verdicts):
            if v["verdict"] == "good":
                rep["good"] += 1
                done[key] = canonical_linkedin(url)
                continue
            item = {"row": n, "name": cell(r, "name"), "email": cell(r, "email"), "old": url,
                    "why": v["why"], "who": v.get("who", ""), "reason": v.get("reason", "")}
            if v["verdict"] == "unsure":
                item["note"] = "Not sure it's the same person — nothing changed; please check."
                rep["left"].append(item)
                done[key] = canonical_linkedin(url)   # asked once; the button asks again
                continue
            lead = next((l for l in leads.get(key, []) if l.get("status") == "accepted"), None)
            if not lead:
                item["note"] = "No accepted lead in LeadSearch for this row, so it was left for a person."
                rep["left"].append(item)
                done[key] = canonical_linkedin(url)   # not reopened each cycle; the button still does
                continue
            item["lead_id"] = lead["id"]
            orchestrator.process_lead(lead["id"], recheck=True)
            fresh = db.get_lead(lead["id"]) or {}
            new = canonical_linkedin(((fresh.get("chosen") or {}).get("url") or "").strip())
            item["new"] = new if "linkedin.com" in new else ""
            if canonical_linkedin(url) == item["new"]:
                item["note"] = "The re-check landed on the same link; left for a person."
                rep["left"].append(item)
                done[key] = canonical_linkedin(url)
                continue
            rep["changed"].append(item)
            done[key] = item["new"]
            # The sync rewrites a cell only when LeadSearch's value changed; a
            # link typed into the sheet by hand would otherwise stay.
            snap = fresh.get("pushed_row") if isinstance(fresh.get("pushed_row"), dict) else {}
            if snap.get("linkedin profile url", "") == item["new"]:
                fixes.append((n, ci["linkedin profile url"] + 1, item["new"]))
        db.meta_set("li_reviewed", json.dumps(done))
        if rep["changed"] and sync:
            sync()
        if fixes:
            sheets.write_cells(ws, fixes)
    except Exception as e:  # noqa: BLE001 — shown on the page; the next run retries
        rep["error"] = str(e)[:300]
    finally:
        rep["running"] = False
        rep["finished"] = time.time()
        db.meta_set("li_review", json.dumps(rep))
        _lock.release()
    return rep


def start(sync=None, only_new: bool = False) -> bool:
    """Review in the background: every row (the button) or only new/changed rows
    (after a Push). False if one is running."""
    if is_running():
        return False
    threading.Thread(target=run, kwargs={"sync": sync, "only_new": only_new}, daemon=True).start()
    return True
