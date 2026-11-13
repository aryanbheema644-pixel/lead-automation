"""The Render server LeadSearch runs on: memory, uptime, restarts, deploys.

Read from inside the server itself (no key needed):
- memory used vs the instance's limit (the container's cgroup; Starter = 512 MB).
  Render restarts the server when it goes over, which is what its "exceeded its
  memory limit" email is about. Above RENDER_MEMORY_ALERT_PCT it shows amber;
- uptime, and every start since (record_start). A start without a new commit
  behind it is a restart Render did on its own, most often memory;
- the commit that's live (RENDER_GIT_COMMIT, set by Render).

With RENDER_API_KEY set (Render → Account settings → API keys), it also reads
the latest deploy's status, so a failed deploy shows up here.

A server that is completely down can't report on itself: Render's own emails
(or an outside uptime monitor) cover that case.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time

import httpx

from . import config, db

_started = time.time()


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def memory() -> dict:
    """{used_mb, limit_mb, pct, source}: the container's memory if we can see
    it (Linux cgroup), else this process's own."""
    used = limit = None
    src = "container"
    cur = _read("/sys/fs/cgroup/memory.current")               # cgroup v2
    if cur is not None:
        used = int(cur)
        mx = _read("/sys/fs/cgroup/memory.max")
        limit = int(mx) if mx and mx != "max" else None
    else:                                                      # cgroup v1
        cur = _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if cur is not None:
            used = int(cur)
            mx = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
            limit = int(mx) if mx and int(mx) < 1 << 50 else None
    if used is None:                                           # not in a container (laptop)
        src = "process"
        status = _read("/proc/self/status") or ""
        rss = next((int(l.split()[1]) * 1024 for l in status.splitlines() if l.startswith("VmRSS:")), None)
        if rss is None:                                        # macOS: peak RSS, bytes
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss = r if sys.platform == "darwin" else r * 1024
        used = rss
    if limit is None:
        limit = config.RENDER_MEMORY_MB * 1024 * 1024
    mb = lambda b: round(b / 1048576)
    return {"used_mb": mb(used), "limit_mb": mb(limit), "pct": round(100 * used / limit, 1), "source": src}


def _commit() -> str:
    return (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]


def record_start() -> None:
    """Called once at startup: remember when and on which commit we started,
    and how much memory the previous run was using just before it stopped."""
    try:
        starts = json.loads(db.meta_get("server_starts", "[]") or "[]")
        last = json.loads(db.meta_get("mem_last", "{}") or "{}")
    except ValueError:
        starts, last = [], {}
    starts.insert(0, {"at": _started, "commit": _commit(),
                      "prev_pct": last.get("pct"), "prev_peak": last.get("peak"), "prev_at": last.get("at")})
    db.meta_set("server_starts", json.dumps(starts[:100]))
    threading.Thread(target=_sample, daemon=True).start()


_peak = 0.0


def _sample() -> None:
    """Every minute, note memory (and this run's peak), so after a restart we
    can tell whether the server was near its limit when it went down."""
    global _peak
    while True:
        try:
            m = memory()
            _peak = max(_peak, m["pct"])
            db.meta_set("mem_last", json.dumps({"at": time.time(), "pct": m["pct"], "peak": _peak}))
        except Exception:  # noqa: BLE001 — sampling must never break the app
            pass
        time.sleep(60)


def _kind(s: dict, prev: dict | None) -> str:
    """deploy | memory | other: why the server (re)started."""
    if not prev or not s.get("commit") or s.get("commit") != prev.get("commit"):
        return "deploy"                         # new code went live
    near = max(s.get("prev_pct") or 0, 0)
    if near >= MEMORY_RESTART_PCT or (s.get("prev_peak") or 0) >= 95:
        return "memory"                         # it was near the limit just before
    return "other"                              # settings change, manual restart, Render maintenance


MEMORY_RESTART_PCT = 75


def _restarts(window_h: float = 24) -> dict:
    """Starts in the window, by kind, and when the last memory restart was."""
    try:
        starts = json.loads(db.meta_get("server_starts", "[]") or "[]")
    except ValueError:
        starts = []
    since = time.time() - window_h * 3600
    out = {"total": 0, "deploy": 0, "memory": 0, "other": 0, "last_memory": None}
    for i, s in enumerate(starts):
        if s["at"] < since:
            break
        k = _kind(s, starts[i + 1] if i + 1 < len(starts) else None)
        out["total"] += 1
        out[k] += 1
        if k == "memory" and out["last_memory"] is None:
            out["last_memory"] = s["at"]
    return out


def _ago(sec: float) -> str:
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60} min"
    if sec < 86400:
        return f"{sec // 3600} h {sec % 3600 // 60} min"
    return f"{sec // 86400} d {sec % 86400 // 3600} h"


def _deploy() -> dict | None:
    sid = os.environ.get("RENDER_SERVICE_ID")
    if not (config.RENDER_API_KEY and sid):
        return None
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"https://api.render.com/v1/services/{sid}/deploys", params={"limit": 1},
                      headers={"Authorization": f"Bearer {config.RENDER_API_KEY}"})
        if r.status_code != 200:
            return {"error": f"Render API HTTP {r.status_code}"}
        d = (r.json() or [{}])[0].get("deploy") or {}
        return {"status": d.get("status"), "commit": ((d.get("commit") or {}).get("id") or "")[:7],
                "finished": d.get("finishedAt") or d.get("updatedAt")}
    except (httpx.HTTPError, ValueError, IndexError) as e:
        return {"error": str(e)[:120]}


def check() -> dict:
    """The health row for the Render server (shape as health.check's)."""
    m = memory()
    rs = _restarts()
    up = time.time() - _started
    out = {"api_ok": True, "memory": m, "uptime": _ago(up), "commit": _commit(), "restarts_24h": rs,
           "plan": f"Up {_ago(up)}" + (f" · commit {_commit()}" if _commit() else "")}
    notes, warn = [], []
    if m["pct"] >= config.RENDER_MEMORY_ALERT_PCT:
        warn.append(f"Memory at {m['pct']:.0f}% ({m['used_mb']} of {m['limit_mb']} MB): Render restarts "
                    "the server at 100%")
    if rs["last_memory"] and time.time() - rs["last_memory"] < 6 * 3600:
        warn.append(f"The server restarted {_ago(time.time() - rs['last_memory'])} ago while memory "
                    "was near its limit: Render most likely restarted it for running out of memory")
    parts = [f"{rs[k]} {label}" for k, label in (("deploy", "deploy"), ("other", "settings/manual"),
                                                    ("memory", "memory")) if rs[k]]
    if parts:
        notes.append("Restarts in the last 24 h: " + ", ".join(parts))
    dep = _deploy()
    if dep and dep.get("status") in ("build_failed", "update_failed", "canceled", "pre_deploy_failed"):
        warn.append(f"Latest deploy {dep.get('commit') or ''} {dep['status'].replace('_', ' ')}: "
                    "the server is still on the previous version")
    elif dep and dep.get("status"):
        notes.append(f"Latest deploy: {dep['status'].replace('_', ' ')}")
    out["deploy"] = dep
    out["warn"] = warn
    if warn:
        out["detail"] = " · ".join(warn)
    out["note"] = " · ".join(notes)
    return out
