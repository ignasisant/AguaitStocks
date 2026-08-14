"""Thin Telegram Bot API client — stdlib urllib, no SDK.

One bot serves every account: the token lives in TELEGRAM_BOT_TOKEN (env,
GitHub Actions) or [telegram] bot_token (secrets.toml, Streamlit Cloud); each
user's chat id lives in their prefs.json after the deep-link `/start` flow.

The linking flow relies on getUpdates polling, so this bot must NEVER have a
webhook configured (Telegram rejects getUpdates while a webhook is set).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from stocks.secrets_env import secret

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096  # Telegram hard limit per message
_cached: dict[str, str] = {}


class TelegramBlocked(Exception):
    """The user blocked the bot (403) — skip them instead of retrying."""


def bot_token() -> str:
    return secret("TELEGRAM_BOT_TOKEN", "telegram", "bot_token")


def configured() -> bool:
    return bool(bot_token())


def _call(method: str, params: dict) -> dict:
    """POST one Bot API method, return the `result` payload. Raises on not-ok."""
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    req = urllib.request.Request(API.format(token=bot_token(), method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.load(exc).get("description", "")
        except Exception:
            pass
        if exc.code == 403:
            raise TelegramBlocked(detail or "bot was blocked by the user") from exc
        raise RuntimeError(f"telegram {method} failed: {detail or exc}") from exc
    if not body.get("ok"):
        raise RuntimeError(body.get("description", f"telegram {method} failed"))
    return body.get("result", {})


def bot_username() -> str:
    """The bot's @username — secret if set, else one cached getMe round-trip."""
    name = secret("TELEGRAM_BOT_USERNAME", "telegram", "bot_username").lstrip("@")
    if name:
        return name
    if "username" not in _cached:
        _cached["username"] = str(_call("getMe", {}).get("username", ""))
    return _cached["username"]


def deep_link(code: str) -> str:
    return f"https://t.me/{bot_username()}?start={code}"


def _split(text: str) -> list[str]:
    """Split on line boundaries so no chunk exceeds Telegram's 4096 chars."""
    if len(text) <= MAX_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        line = line[:MAX_LEN]  # a single pathological line still has to fit
        if current and len(current) + 1 + len(line) > MAX_LEN:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_message(
    text: str,
    chat_id: int | str,
    parse_mode: str | None = "HTML",
    disable_web_page_preview: bool = True,
) -> None:
    """Send `text` to `chat_id`, splitting when over the 4096-char limit.

    Raises TelegramBlocked when the user blocked the bot, RuntimeError on any
    other API failure.
    """
    for chunk in _split(text):
        _call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": "true" if disable_web_page_preview else None,
            },
        )


def get_updates(timeout: int = 0) -> list[dict]:
    """Recent updates, without an offset so nothing is consumed.

    Unconfirmed updates expire server-side after 24h; leaving them pending
    means two users linking at the same moment can't eat each other's /start.
    A 409 (another getUpdates literally in flight) returns [] — the caller's
    next poll tick retries.
    """
    try:
        result = _call(
            "getUpdates",
            {"timeout": timeout, "allowed_updates": json.dumps(["message"])},
        )
        return list(result) if isinstance(result, list) else []
    except RuntimeError as exc:
        if "409" in str(exc) or "Conflict" in str(exc):
            return []
        raise
    except urllib.error.URLError:
        return []  # transient network blip — poll again next tick


def match_start(updates: list[dict], code: str) -> dict | None:
    """The chat dict of the message `/start <code>`, or None. Pure."""
    if not code:
        return None
    for update in reversed(updates):  # newest first
        msg = update.get("message") or {}
        if msg.get("text", "").strip() == f"/start {code}":
            chat = msg.get("chat") or {}
            if chat.get("id"):
                return chat
    return None
