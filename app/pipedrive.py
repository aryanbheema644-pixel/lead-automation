"""Pipedrive sheet -> Pipedrive CRM sync (replaces the Pabbly workflow).

For every complete row of the destination ("Pipedrive") sheet:
  1. find the person by email, then phone; create one if neither matches
  2. set the person's LinkedIn Profile (+ visa / source / message) fields
  3. create the deal "<Visa>_<Name>" in the configured pipeline + stage, owned by
     the row's "Owner (AE) Id" and linked to the person
  4. on later runs, update that same deal / person when the row changes
     (title and owner on the deal; the custom fields on the person). Stage,
     activities etc. are the AEs' and are never touched.

A row whose person already has a deal in the pipeline (e.g. made by Pabbly) is
LINKED to it, not given a second one.

DRY RUN is the default: only read calls go to Pipedrive and the result is a
plan of what would happen. Writes happen only for rows a person ticked and sent
("Write on Pipe"), or for the whole sheet when PIPEDRIVE_WRITE=true; `_write`
refuses otherwise, so nothing can reach the CRM by accident.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from . import config, db, sheets

API = "https://api.pipedrive.com/v1"


class PipedriveError(RuntimeError):
    pass


# Person custom fields we fill, by their name in Pipedrive -> sheet column.
_PERSON_FIELDS = {
    "linkedin profile": "linkedin profile url",
    "visa interested in": "visa intrested in",
    "lead source": "source",
    "lead's message": "message",
}


def _hkey(header: str) -> str:
    return " ".join((header or "").lower().split())


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _same_phone(a: str, b: str) -> bool:
    """Same number, with or without the country code / leading zeros."""
    if len(a) < 7 or len(b) < 7:
        return False
    return a == b or a.endswith(b[-9:]) and b.endswith(a[-9:])


class Client:
    def __init__(self, write: bool = False):
        if not config.PIPEDRIVE_API_TOKEN:
            raise PipedriveError("PIPEDRIVE_API_TOKEN is not set")
        self.write = write
        self.calls = 0
        self._http = httpx.Client(timeout=30, params={"api_token": config.PIPEDRIVE_API_TOKEN})

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(4):
            self.calls += 1
            r = self._http.request(method, API + path, **kw)
            if r.status_code == 429:                       # rate limited: back off
                time.sleep(2 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code >= 400 or not body.get("success", False):
                raise PipedriveError(f"{method} {path}: {r.status_code} {body.get('error') or r.text[:200]}")
            return body
        raise PipedriveError(f"{method} {path}: rate limited")

    def get(self, path: str, **params) -> dict:
        return self._req("GET", path, params=params)

    def _write(self, method: str, path: str, payload: dict) -> dict:
        if not self.write:
            raise PipedriveError("Write blocked: dry run")
        return self._req(method, path, json=payload)

    def paged(self, path: str, **params) -> list[dict]:
        out, start = [], 0
        while True:
            body = self.get(path, start=start, limit=500, **params)
            out += body.get("data") or []
            more = ((body.get("additional_data") or {}).get("pagination") or {})
            if not more.get("more_items_in_collection"):
                return out
            start = more.get("next_start", start + 500)

    # --- lookups (read-only) ---
    def find_person(self, email: str, phone: str) -> dict | None:
        """The lead's existing contact: same email, else same phone number.
        Phones are compared by digits (Pipedrive may hold "+598 92…" for the
        sheet's "59892…"), so a fuzzy search is filtered to real matches. Of
        several, the oldest contact (the one made when the lead came in) wins."""
        if email:
            items = (self.get("/persons/search", term=email, fields="email",
                              exact_match="true").get("data") or {}).get("items") or []
            if items:
                return min((i["item"] for i in items), key=lambda p: p["id"])
        want = _digits(phone)
        if len(want) < 7:
            return None
        items = (self.get("/persons/search", term=want[-9:], fields="phone").get("data") or {}).get("items") or []
        same = [i["item"] for i in items
                if any(_same_phone(want, _digits(p)) for p in i["item"].get("phones") or [])]
        return min(same, key=lambda p: p["id"]) if same else None

    # --- writes (blocked unless live) ---
    def create_person(self, payload: dict) -> dict:
        return self._write("POST", "/persons", payload)["data"]

    def update_person(self, pid: int, payload: dict) -> dict:
        return self._write("PUT", f"/persons/{pid}", payload)["data"]

    def create_deal(self, payload: dict) -> dict:
        return self._write("POST", "/deals", payload)["data"]

    def update_deal(self, did: int, payload: dict) -> dict:
        return self._write("PUT", f"/deals/{did}", payload)["data"]


def _setup(c: Client) -> dict:
    """Resolve the pipeline/stage ids and the person custom-field keys by name."""
    pipes = c.get("/pipelines").get("data") or []
    pipe = next((p for p in pipes if _hkey(p["name"]) == _hkey(config.PIPEDRIVE_PIPELINE)), None)
    if not pipe:
        raise PipedriveError(f"No pipeline named {config.PIPEDRIVE_PIPELINE!r} "
                             f"(have: {', '.join(p['name'] for p in pipes)})")
    stages = c.get("/stages", pipeline_id=pipe["id"]).get("data") or []
    stage = next((s for s in stages if _hkey(s["name"]) == _hkey(config.PIPEDRIVE_STAGE)), None)
    if not stage:
        raise PipedriveError(f"No stage {config.PIPEDRIVE_STAGE!r} in {pipe['name']}")
    fields, missing = {}, []
    by_name = {_hkey(f["name"]): f for f in c.paged("/personFields")}
    for name, col in _PERSON_FIELDS.items():
        f = by_name.get(name)
        if not f:
            missing.append(name)
            continue
        opts = {_hkey(o["label"]): o["id"] for o in (f.get("options") or [])}
        fields[col] = {"key": f["key"], "name": f["name"], "type": f.get("field_type"), "options": opts}
    return {"pipeline": pipe, "stage": stage, "fields": fields, "missing_fields": missing}


def _field_value(spec: dict, value: str):
    """A sheet value in the form the person field takes; None = can't set it."""
    if not value:
        return None
    if spec["type"] in ("enum", "set"):
        return spec["options"].get(_hkey(value))     # option id, or None if no such option
    return value


def _rows() -> list[dict]:
    """Complete rows of the Pipedrive sheet as {column key: value, '_row': n}."""
    _ws, values = sheets.open_dest()
    if not values:
        return []
    cols = {}
    for i, h in enumerate(values[0]):
        cols.setdefault(_hkey(h), i)
    out = []
    for n, row in enumerate(values[1:], start=2):
        r = {k: (row[i].strip() if i < len(row) else "") for k, i in cols.items()}
        r["_row"] = n
        out.append(r)
    return out


def _row_key(r: dict) -> str:
    if r.get("email"):
        return r["email"].lower()
    return "phone:" + _digits(r.get("number", ""))


def _deal_title(r: dict) -> str:
    return f"{r.get('visa intrested in') or 'AE decide'}_{r['name']}"


def _links() -> dict[str, dict]:
    raw = db.meta_get("pd_links", "{}")
    try:
        return json.loads(raw) or {}
    except ValueError:
        return {}


def sync(live: bool = False, only: list[dict] | None = None, progress=None) -> dict:
    """Plan (dry run) or apply the sheet -> Pipedrive sync. Returns the plan:
    one entry per sheet row with what was / would be done.

    only = [{"row": n, "key": row key}, …] limits it to those rows (the ones
    ticked in the preview). The key must still match the row, so a sheet that
    changed since the preview can't make it write the wrong lead."""
    if live and only is None and not config.PIPEDRIVE_WRITE:
        raise PipedriveError("Whole-sheet sync is off: set PIPEDRIVE_WRITE=true first")
    if only is not None and not only:
        raise PipedriveError("No rows selected")
    c = Client(write=live)
    setup = _setup(c)
    pipe_id, stage_id = setup["pipeline"]["id"], setup["stage"]["id"]
    fields = setup["fields"]
    links = _links()

    # Every deal in the pipeline, indexed by its person's email/phone, so rows
    # that already have a deal (Pabbly's) get linked instead of duplicated.
    by_contact: dict[str, dict] = {}
    for d in c.paged("/deals", status="all_not_deleted"):
        if d.get("pipeline_id") != pipe_id or not isinstance(d.get("person_id"), dict):
            continue
        p = d["person_id"]
        for e in p.get("email") or []:
            if e.get("value"):
                by_contact.setdefault(e["value"].lower(), d)
        for ph in p.get("phone") or []:
            if len(_digits(ph.get("value"))) >= 7:
                by_contact.setdefault("phone:" + _digits(ph["value"])[-9:], d)

    plan, counts = [], {"create": 0, "link": 0, "update": 0, "unchanged": 0, "skipped": 0, "error": 0}
    wanted = {int(o["row"]): o.get("key", "") for o in only or []}
    rows = _rows()
    if only is not None:
        stale = [n for n, k in wanted.items()
                 if not any(r["_row"] == n and _row_key(r) == k for r in rows)]
        if stale:
            raise PipedriveError(f"The sheet changed since the preview (row {', '.join(map(str, stale))}). "
                                 "Click Preview sync again, then tick the leads again.")
        rows = [r for r in rows if r["_row"] in wanted]
    for i, r in enumerate(rows):
        if progress:
            progress(i, len(rows), r.get("name", ""))
        entry = {"row": r["_row"], "key": _row_key(r), "name": r.get("name", ""), "email": r.get("email", ""),
                 "title": "", "owner": r.get("owner name", ""), "actions": [], "notes": []}
        plan.append(entry)
        miss = [m for m, ok in (("name", r.get("name")), ("email/number", r.get("email") or r.get("number")),
                                ("owner id", r.get("owner (ae) id")), ("source", r.get("source"))) if not ok]
        if miss:
            entry["status"] = "skipped"
            entry["notes"].append("not ready: no " + ", ".join(miss))
            counts["skipped"] += 1
            continue
        try:
            _sync_row(c, r, entry, links, by_contact, fields, pipe_id, stage_id)
        except (PipedriveError, httpx.HTTPError) as e:
            entry["status"] = "error"
            entry["notes"].append(str(e))
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    if live:
        db.meta_set("pd_links", json.dumps(links))
        db.meta_set("pd_last_sync", str(time.time()))
        _log([e for e in plan if e["status"] in ("create", "link", "update", "error")])
    return {"live": live, "counts": counts, "plan": plan, "api_calls": c.calls,
            "pipeline": setup["pipeline"]["name"], "stage": setup["stage"]["name"],
            "fields": {v["name"]: v["type"] for v in fields.values()},
            "missing_fields": setup["missing_fields"], "domain": config.PIPEDRIVE_DOMAIN}


def _sync_row(c: Client, r: dict, entry: dict, links: dict, by_contact: dict,
              fields: dict, pipe_id: int, stage_id: int) -> None:
    key = _row_key(r)
    title = _deal_title(r)
    entry["title"] = title
    owner = int(r["owner (ae) id"])
    custom = {}
    for col, spec in fields.items():
        v = _field_value(spec, r.get(col, ""))
        if v is None and r.get(col) and spec["type"] in ("enum", "set"):
            entry["notes"].append(f"{spec['name']}: no option {r[col]!r} in Pipedrive")
        elif v is not None:
            custom[spec["key"]] = v
    want = {"title": title, "owner": owner, "custom": custom}

    link = links.get(key)
    if link:                                             # synced before: update what changed
        last = link.get("last") or {}
        deal_upd = {k: v for k, v in (("title", title), ("user_id", owner))
                    if v != last.get({"user_id": "owner"}.get(k, k))}
        person_upd = {k: v for k, v in custom.items() if v != (last.get("custom") or {}).get(k)}
        if not deal_upd and not person_upd:
            entry["status"] = "unchanged"
            return
        if deal_upd:
            entry["actions"].append("update deal " + ", ".join(f"{k}→{v}" for k, v in deal_upd.items()))
            if c.write:
                c.update_deal(link["deal_id"], deal_upd)
        if person_upd:
            entry["actions"].append(f"update person ({len(person_upd)} field(s))")
            if c.write:
                c.update_person(link["person_id"], person_upd)
        entry["status"] = "update"
        entry["deal_id"] = link["deal_id"]
        link["last"] = want
        return

    digits = _digits(r.get("number", ""))
    deal = by_contact.get(r.get("email", "").lower() or "-") or \
        (by_contact.get("phone:" + digits[-9:]) if len(digits) >= 7 else None)
    if deal:                                             # already has a deal (Pabbly's): link it
        # The deal is left exactly as it is (no rename, no owner change); only
        # LATER changes to the row update it. The person's fields are filled in.
        entry["status"] = "link"
        entry["deal_id"] = deal["id"]
        entry["actions"].append(f"link to existing deal #{deal['id']} “{deal.get('title')}” (left as is)")
        if custom:
            entry["actions"].append(f"set {len(custom)} person field(s)")
        if c.write:
            pid = deal["person_id"]["value"]
            if custom:
                c.update_person(pid, custom)
            links[key] = {"deal_id": deal["id"], "person_id": pid, "last": want}
        return

    person = c.find_person(r.get("email", ""), r.get("number", ""))
    if person:
        entry["actions"].append(f"use existing person #{person['id']} ({person.get('name')})")
        pid = person["id"]
        if custom:
            entry["actions"].append(f"set {len(custom)} person field(s)")
            if c.write:
                c.update_person(pid, custom)
    else:
        entry["actions"].append("create person")
        pid = None
        if c.write:
            payload = {"name": r["name"], "owner_id": owner, **custom}
            if r.get("email"):
                payload["email"] = [{"value": r["email"], "primary": True}]
            if r.get("number"):
                payload["phone"] = [{"value": r["number"], "primary": True}]
            pid = c.create_person(payload)["id"]
    entry["actions"].append(f"create deal “{title}” → {r.get('owner name') or owner}")
    entry["status"] = "create"
    if c.write:
        d = c.create_deal({"title": title, "person_id": pid, "user_id": owner,
                           "stage_id": stage_id, "status": "open", "probability": 1,
                           "visible_to": "1"})
        entry["deal_id"] = d["id"]
        links[key] = {"deal_id": d["id"], "person_id": pid, "last": want}


def _log(entries: list[dict]) -> None:
    """Append what was written to the "Written to Pipedrive" history (newest first)."""
    if not entries:
        return
    now = time.time()
    new = [{"at": now, "status": e["status"], "row": e["row"], "name": e["name"], "email": e["email"],
            "title": e["title"], "owner": e["owner"], "deal_id": e.get("deal_id"),
            "actions": e["actions"], "notes": e["notes"]} for e in entries]
    db.meta_set("pd_log", json.dumps((new + written())[:1000]))


def written() -> list[dict]:
    try:
        return json.loads(db.meta_get("pd_log", "[]")) or []
    except ValueError:
        return []


def export_state() -> dict:
    """What this app remembers about Pipedrive (row -> deal links + write history)."""
    return {"links": _links(), "written": written()}


def import_state(data: dict) -> dict:
    """Merge another app's export into this one (e.g. local -> Render). Links this
    app already has win; history entries are de-duplicated by time + deal."""
    links = _links()
    added = 0
    for k, v in (data.get("links") or {}).items():
        if k not in links and isinstance(v, dict) and v.get("deal_id"):
            links[k] = v
            added += 1
    log = written()
    seen = {(e.get("at"), e.get("deal_id")) for e in log}
    new = [e for e in data.get("written") or [] if (e.get("at"), e.get("deal_id")) not in seen]
    db.meta_set("pd_links", json.dumps(links))
    db.meta_set("pd_log", json.dumps(sorted(log + new, key=lambda e: -(e.get("at") or 0))[:1000]))
    return {"links_added": added, "history_added": len(new)}
