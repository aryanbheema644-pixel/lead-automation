"""Service health: credits left and a live check for every outside service.

Every HEALTH_CHECK_MINUTES (and on demand from the Services page) each service
is asked two things: does our key still work (the API check), and how much is
left on the plan (the credit check). Each gets a state:

- ok:   working, credits above its cap;
- low:  working, but credits at or below the cap the team set (time to top up);
- down: the key is rejected, the service doesn't answer, or credits are out;
- off:  not configured.

A move into low or down sends an alert (Google Chat, else email: notify.py),
again every ALERT_REPEAT_HOURS while it stays that way, and once more when it
recovers. The pipeline also reports a failed call the moment it happens
(saw_error), so an outage between checks is flagged at once.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config, db, notify

# What we track, in the order the status bar shows them.
SERVICES = {
    "openrouter": {"name": "OpenRouter", "role": "AI that reads and judges leads", "unit": "$",
                   "cap": 5.0, "topup": "https://openrouter.ai/settings/credits"},
    "tavily": {"name": "Tavily", "role": "Web search for lead profiles", "unit": "searches",
               "cap": 100, "topup": "https://app.tavily.com/billing"},
    "apify": {"name": "Apify", "role": "Opens and searches LinkedIn", "unit": "$",
              "cap": 1.0, "topup": "https://console.apify.com/billing"},
    "firecrawl": {"name": "Firecrawl", "role": "Reads web pages search can't", "unit": "credits",
                  "cap": 100, "topup": "https://www.firecrawl.dev/app/billing"},
    "pipedrive": {"name": "Pipedrive", "role": "CRM: deals and contacts", "unit": None,
                  "topup": "https://app.pipedrive.com"},
    "sheets": {"name": "Google Sheets", "role": "Source and Pipedrive sheets", "unit": None,
               "topup": "https://console.cloud.google.com"},
    "screening": {"name": "Screening", "role": "Fit evaluator (ForRAG)", "unit": None, "topup": ""},
}
ALERT_REPEAT_HOURS = 6
_lock = threading.Lock()


def _meta(key: str, default):
    try:
        return json.loads(db.meta_get(key, "") or "null") or default
    except ValueError:
        return default


def caps() -> dict:
    """The low-credit cap per service: the team's, else the default."""
    saved = _meta("svc_caps", {})
    return {k: float(saved.get(k, s["cap"])) for k, s in SERVICES.items() if s.get("cap") is not None}


def set_cap(service: str, value: float) -> dict:
    if service not in SERVICES or SERVICES[service].get("cap") is None:
        raise ValueError(f"{service} has no credit balance to cap")
    if value < 0:
        raise ValueError("The cap can't be negative")
    saved = _meta("svc_caps", {})
    saved[service] = value
    db.meta_set("svc_caps", json.dumps(saved))
    return caps()


# ── One check per service ───────────────────────────────────────────────────
def _get(url: str, **kw) -> httpx.Response:
    with httpx.Client(timeout=20) as c:
        return c.get(url, **kw)


def _err(r: httpx.Response) -> str:
    try:
        b = r.json()
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:160]}"
    msg = None
    if isinstance(b, dict):
        e = b.get("error")
        msg = e.get("message") if isinstance(e, dict) else e
        if not msg:
            d = b.get("detail")
            msg = d.get("error") if isinstance(d, dict) else d
    return f"HTTP {r.status_code}: {str(msg or r.text)[:160]}"


def _openrouter() -> dict:
    if not config.OPENROUTER_API_KEY:
        return {"configured": False}
    r = _get("https://openrouter.ai/api/v1/credits",
             headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"})
    if r.status_code != 200:
        return {"api_ok": False, "detail": _err(r)}
    d = r.json().get("data") or {}
    total, used = float(d.get("total_credits") or 0), float(d.get("total_usage") or 0)
    # Pay-as-you-go: there is no plan limit, just a balance (bought minus used).
    return {"api_ok": True, "plan": "Pay-as-you-go balance", "paid": True, "balance": True,
            "bought": round(total, 2), "used": round(used, 2), "remaining": round(total - used, 2)}


def _tavily() -> dict:
    if not config.TAVILY_API_KEY:
        return {"configured": False}
    r = _get("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"})
    if r.status_code != 200:
        return {"api_ok": False, "detail": _err(r)}
    a = r.json().get("account") or {}
    limit = a.get("plan_limit")
    used = float(a.get("plan_usage") or 0)
    paygo_left = None
    if a.get("paygo_limit"):
        paygo_left = float(a["paygo_limit"]) - float(a.get("paygo_usage") or 0)
    remaining = (float(limit) - used if limit is not None else None)
    if paygo_left:
        remaining = (remaining or 0) + paygo_left
    plan = a.get("current_plan") or ""
    return {"api_ok": True, "plan": plan, "paid": plan.lower() not in ("researcher", "free", ""),
            "limit": limit, "used": used, "remaining": remaining, "per": "this month"}


def _apify() -> dict:
    if not config.APIFY_API_TOKEN:
        return {"configured": False}
    r = _get("https://api.apify.com/v2/users/me/limits", params={"token": config.APIFY_API_TOKEN})
    if r.status_code != 200:
        return {"api_ok": False, "detail": _err(r)}
    d = r.json().get("data") or {}
    limit = (d.get("limits") or {}).get("maxMonthlyUsageUsd")
    used = float((d.get("current") or {}).get("monthlyUsageUsd") or 0)
    plan, paid = "", None
    try:
        me = _get("https://api.apify.com/v2/users/me", params={"token": config.APIFY_API_TOKEN})
        p = ((me.json().get("data") or {}).get("plan") or {}) if me.status_code == 200 else {}
        plan = p.get("id") or ""
        paid = plan.upper() not in ("FREE", "")
    except httpx.HTTPError:
        pass
    return {"api_ok": True, "plan": plan, "paid": paid, "limit": limit, "used": round(used, 2),
            "remaining": round(float(limit) - used, 2) if limit is not None else None, "per": "this month"}


def _firecrawl() -> dict:
    if not config.FIRECRAWL_API_KEY:
        return {"configured": False}
    r = _get("https://api.firecrawl.dev/v2/team/credit-usage",
             headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"})
    if r.status_code != 200:
        return {"api_ok": False, "detail": _err(r)}
    d = r.json().get("data") or {}
    limit = d.get("planCredits")
    left = d.get("remainingCredits")
    return {"api_ok": True, "plan": f"{limit} credits a month" if limit else "", "paid": None,
            "limit": limit, "remaining": left,
            "used": (limit - left) if limit is not None and left is not None else None,
            "per": "until " + (d.get("billingPeriodEnd") or "")[:10] if d.get("billingPeriodEnd") else ""}


def _pipedrive() -> dict:
    if not config.PIPEDRIVE_API_TOKEN:
        return {"configured": False}
    from . import pipedrive
    try:
        pipedrive.Client(write=False).get("/users/me")
    except pipedrive.PipedriveError as e:
        return {"api_ok": False, "detail": str(e)[:200]}
    return {"api_ok": True, "plan": "Subscription (no credits)"}


def _sheets() -> dict:
    if not config.sheets_ready():
        return {"configured": False}
    from . import sheets
    try:
        cli = sheets._client()
        for sid in filter(None, (config.GOOGLE_SOURCE_SHEET_ID, config.GOOGLE_DEST_SHEET_ID)):
            cli.open_by_key(sid).title       # noqa: B018 — opening proves access
    except Exception as e:  # noqa: BLE001 — any failure = can't reach the sheets
        return {"api_ok": False, "detail": str(e)[:200]}
    return {"api_ok": True, "plan": "Google service account (free)"}


def _screening() -> dict:
    if not config.screening_ready():
        return {"configured": False}
    r = _get(config.SCREENING_URL)          # a GET on the POST-only endpoint: 405 = it answers
    if r.status_code >= 500:
        return {"api_ok": False, "detail": _err(r)}
    return {"api_ok": True, "plan": "Our own server"}


_CHECKS = {"openrouter": _openrouter, "tavily": _tavily, "apify": _apify, "firecrawl": _firecrawl,
           "pipedrive": _pipedrive, "sheets": _sheets, "screening": _screening}


def _state(res: dict, cap: float | None) -> str:
    if res.get("configured") is False:
        return "off"
    if not res.get("api_ok"):
        return "down"
    rem = res.get("remaining")
    if rem is not None and float(rem) <= 0:
        return "down"
    if cap is not None and rem is not None and float(rem) <= cap:
        return "low"
    return "ok"


def check(name: str) -> dict:
    s = SERVICES[name]
    try:
        res = _CHECKS[name]()
    except httpx.HTTPError as e:
        res = {"api_ok": False, "detail": f"Not answering: {type(e).__name__}"}
    except Exception as e:  # noqa: BLE001 — a broken check is reported, never raised
        res = {"api_ok": False, "detail": f"Check failed: {str(e)[:160]}"}
    cap = caps().get(name)
    out = {"key": name, "name": s["name"], "role": s["role"], "unit": s["unit"], "cap": cap,
           "topup": s["topup"], "checked_at": time.time(), **res}
    out["state"] = _state(res, cap)
    if out["state"] == "down" and res.get("api_ok") and not res.get("detail"):
        out["detail"] = "Out of credits"
    return out


def check_all(alert: bool = True) -> dict:
    """Check every service now, store it for the page, alert on changes."""
    with _lock:
        with ThreadPoolExecutor(len(SERVICES)) as ex:     # all at once: ~3s, not ~10s
            results = dict(zip(SERVICES, ex.map(check, SERVICES)))
        db.meta_set("svc_status", json.dumps({"at": time.time(), "services": results}))
        if alert:
            _alerts(results)
        return status()


def restate() -> dict:
    """Re-judge the last check against the current caps (after a cap change),
    without calling any service again; alerts if that moves a state."""
    with _lock:
        st = _meta("svc_status", {"at": 0, "services": {}})
        c = caps()
        for k, r in st["services"].items():
            if k in c:
                r["cap"] = c[k]
                if r.get("state") != "down" or r.get("api_ok"):
                    r["state"] = _state(r, c[k])
        db.meta_set("svc_status", json.dumps(st))
        _alerts(st["services"])
        return status()


def status() -> dict:
    """The last check (for the status bar), plus where alerts go."""
    st = _meta("svc_status", {"at": 0, "services": {}})
    st["caps"] = caps()
    st["channels"] = notify.channels()
    st["alerts"] = _meta("svc_alert_log", [])[:30]
    st["every_minutes"] = config.HEALTH_CHECK_MINUTES
    return st


def saw_error(name: str, detail: str) -> None:
    """A pipeline call to `name` just failed in a way that means the service is
    down or out (bad key, no credits). Mark it and alert now, not at the next check."""
    if name not in SERVICES:
        return
    try:
        with _lock:
            st = _meta("svc_status", {"at": 0, "services": {}})
            cur = st["services"].get(name) or {"key": name, "name": SERVICES[name]["name"],
                                               "role": SERVICES[name]["role"], "unit": SERVICES[name]["unit"],
                                               "topup": SERVICES[name]["topup"]}
            if cur.get("state") == "down" and time.time() - (cur.get("checked_at") or 0) < 300:
                return                                         # already flagged a moment ago
            cur.update(state="down", api_ok=False, detail=f"Failed during lead processing: {detail[:180]}",
                       checked_at=time.time())
            st["services"][name] = cur
            db.meta_set("svc_status", json.dumps(st))
            _alerts({name: cur})
    except Exception:  # noqa: BLE001 — reporting must never break the pipeline
        pass


# ── Alerts ──────────────────────────────────────────────────────────────────
def _fmt_amount(r: dict, v) -> str:
    if v is None:
        return "?"
    if r.get("unit") == "$":
        return f"${float(v):,.2f}"
    return f"{float(v):,.0f} {r.get('unit') or ''}".strip()


def _line(r: dict) -> str:
    if r["state"] == "ok":
        left = f" ({_fmt_amount(r, r.get('remaining'))} left)" if r.get("remaining") is not None else ""
        return f"✅ {r['name']} is working again{left}."
    if r["state"] == "low":
        of = f" of {_fmt_amount(r, r.get('limit'))}" if r.get("limit") is not None else ""
        return (f"🟡 {r['name']} is running low: {_fmt_amount(r, r.get('remaining'))} left{of} "
                f"(alert cap {_fmt_amount(r, r.get('cap'))}). "
                f"Top up before it runs out: {r.get('topup')}")
    why = r.get("detail") or "not working"
    return (f"🔴 {r['name']} is DOWN ({r['role'][0].lower() + r['role'][1:]}): {why}. Leads will pile up or go "
            f"to review until it's fixed. Fix: {r.get('topup') or 'check the key on Render'}")


def _alerts(results: dict) -> None:
    sent = _meta("svc_alerted", {})               # service -> {"state", "at"}
    log = _meta("svc_alert_log", [])
    now, lines = time.time(), []
    for k, r in results.items():
        st, prev = r.get("state"), sent.get(k) or {}
        if st in ("low", "down"):
            if prev.get("state") != st or now - prev.get("at", 0) >= ALERT_REPEAT_HOURS * 3600:
                lines.append(_line(r))
                sent[k] = {"state": st, "at": now}
        elif st == "ok" and prev.get("state") in ("low", "down"):
            line = _line(r)
            if prev.get("state") == "low":
                line = line.replace("is working again", "is back above its alert cap")
            lines.append(line)
            sent[k] = {"state": "ok", "at": now}
    if not lines:
        db.meta_set("svc_alerted", json.dumps(sent))
        return
    text = "LeadSearch service alert\n" + "\n".join(lines)
    delivered = notify.send(text)
    log.insert(0, {"at": now, "text": text, "delivered": delivered})
    db.meta_set("svc_alert_log", json.dumps(log[:200]))
    db.meta_set("svc_alerted", json.dumps(sent))


# ── Background loop ─────────────────────────────────────────────────────────
_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    _stop.wait(30)                                   # let the app finish starting
    while not _stop.is_set():
        try:
            check_all()
        except Exception:  # noqa: BLE001 — keep checking next time
            pass
        _stop.wait(max(5, config.HEALTH_CHECK_MINUTES) * 60)


def start() -> None:
    global _thread
    if _thread is None and config.HEALTH_CHECK_MINUTES > 0:
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()
