"""Deliver alert/reminder messages to real channels.

Channels are opt-in via environment (.env): a channel with no config is simply
skipped, so `stocks alerts --deliver` degrades to console-only until you wire
one up. No secrets live in code.

  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  Email    : SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
             ALERT_EMAIL_TO, ALERT_EMAIL_FROM (default SMTP_USER)
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def email_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("ALERT_EMAIL_TO")
    )


def configured_channels() -> list[str]:
    channels = ["console"]
    if telegram_configured():
        channels.append("telegram")
    if email_configured():
        channels.append("email")
    return channels


def _send_telegram(text: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = urllib.parse.urlencode(
        {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}
    ).encode()
    req = urllib.request.Request(TELEGRAM_API.format(token=token), data=payload)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "telegram send failed"))
    return "sent"


def _send_email(subject: str, text: str) -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("ALERT_EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_EMAIL_TO"]
    msg.set_content(text)
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)
    return "sent"


def deliver(lines: list[str], subject: str = "Stock alerts") -> dict[str, str]:
    """Send `lines` through every configured channel. Returns {channel: status}.

    Console always fires. A channel that raises is reported as 'error: ...'
    rather than aborting the others.
    """
    status: dict[str, str] = {}
    if not lines:
        return status
    text = "\n".join(lines)

    print(f"── {subject} ──")
    for line in lines:
        print(line)
    status["console"] = "sent"

    if telegram_configured():
        try:
            status["telegram"] = _send_telegram(f"{subject}\n{text}")
        except Exception as exc:
            status["telegram"] = f"error: {exc}"
    if email_configured():
        try:
            status["email"] = _send_email(subject, text)
        except Exception as exc:
            status["email"] = f"error: {exc}"
    return status
