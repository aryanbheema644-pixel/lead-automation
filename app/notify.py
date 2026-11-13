"""Where service alerts go: the Google Chat space #Agentalerts.

An incoming webhook on the space (Space settings → Apps & integrations →
Webhooks), pasted as GCHAT_WEBHOOK_URL on Render.
"""
from __future__ import annotations

import httpx

from . import config


def channels() -> dict:
    return {"google_chat": bool(config.GCHAT_WEBHOOK_URL)}


def send(text: str) -> dict:
    """Post to the Chat space. Returns {"google_chat": "sent" | error}."""
    if not config.GCHAT_WEBHOOK_URL:
        return {"none": "Google Chat isn't set up (GCHAT_WEBHOOK_URL)"}
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(config.GCHAT_WEBHOOK_URL, json={"text": text})
        return {"google_chat": "sent" if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:150]}"}
    except httpx.HTTPError as e:
        return {"google_chat": f"failed: {str(e)[:150]}"}
