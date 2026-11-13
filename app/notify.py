"""Where service alerts go: a Google Chat space, else email.

Google Chat: an incoming webhook on a space (Space settings → Apps &
integrations → Webhooks → Add). Paste its URL as GCHAT_WEBHOOK_URL on Render.
No Google Cloud project or OAuth is needed.

Email (optional, used too if set): SMTP_HOST, SMTP_PORT (587), SMTP_USER,
SMTP_PASSWORD (for Gmail: an app password), ALERT_EMAIL_TO (comma-separated).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from . import config


def channels() -> dict:
    return {"google_chat": bool(config.GCHAT_WEBHOOK_URL),
            "email": bool(config.SMTP_HOST and config.ALERT_EMAIL_TO),
            "email_to": config.ALERT_EMAIL_TO if config.SMTP_HOST else ""}


def _chat(text: str) -> str:
    with httpx.Client(timeout=20) as c:
        r = c.post(config.GCHAT_WEBHOOK_URL, json={"text": text})
    return "" if r.status_code == 200 else f"Google Chat HTTP {r.status_code}: {r.text[:150]}"


def _email(text: str) -> str:
    msg = EmailMessage()
    msg["Subject"] = text.splitlines()[1][:120] if len(text.splitlines()) > 1 else "LeadSearch alert"
    msg["From"] = config.ALERT_EMAIL_FROM or config.SMTP_USER
    msg["To"] = config.ALERT_EMAIL_TO
    msg.set_content(text)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    return ""


def send(text: str) -> dict:
    """Send to every channel set up. Returns {channel: "sent" | error}."""
    out = {}
    for name, ready, fn in (("google_chat", bool(config.GCHAT_WEBHOOK_URL), _chat),
                            ("email", bool(config.SMTP_HOST and config.ALERT_EMAIL_TO), _email)):
        if not ready:
            continue
        try:
            out[name] = fn(text) or "sent"
        except Exception as e:  # noqa: BLE001 — report it on the page, keep going
            out[name] = f"failed: {str(e)[:150]}"
    return out or {"none": "No alert channel set up (GCHAT_WEBHOOK_URL or SMTP_*)"}
