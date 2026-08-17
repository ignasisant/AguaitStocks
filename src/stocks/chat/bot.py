"""Telegram chat bot: drain the webhook update queue and answer each message.

Inbound path: Telegram webhook → Cloudflare Worker (workers/telegram-webhook)
→ one JSON object per update in the bucket under data/tg_updates/ → a
repository_dispatch wakes the GitHub Actions job → `stocks telegram-chat`
calls drain(). The Worker only stores and dispatches; everything stateful
happens here, from a bare checkout, exactly like the digest/alerts crons.

Accounts are matched by chat id against every user's prefs.json
(fanout.iter_all_users). `/start <code>` completes the Profile page's linking
flow: the page writes a pending tg_link_code into the user's prefs, this job
matches it and writes telegram_chat_id back. Answers come from the shared
chat engine (stocks/chat/engine.py) — same persona, portfolio context,
skills, web search, provider resolution and free-tier quota as the web panel,
appended to the same chat.json thread.

Failure discipline: every update is deleted from the queue whether it
succeeded or not (a poison update must never wedge the queue); its error goes
to the run log. One update's failure never touches the others. Without a
bucket (local dev) the queue is read from the local data/tg_updates/ dir so
--dry-run works offline.
"""

from __future__ import annotations

import json
import time

from stocks import storage
from stocks.chat import engine
from stocks.config import DATA_DIR, PROJECT_ROOT
from stocks.notify import fanout, telegram

QUEUE_PREFIX = "data/tg_updates/"
LINK_TTL = 600  # seconds a pending link code stays valid — matches profile.py


def queue_key(update_id: int) -> str:
    """Zero-padded so lexicographic key order == chronological order."""
    return f"{QUEUE_PREFIX}{int(update_id):012d}.json"


def _read_queue_object(key: str) -> bytes | None:
    if storage.enabled():
        return storage.read_key(key)
    try:
        return (PROJECT_ROOT / key).read_bytes()
    except OSError:
        return None


def _delete_queue_object(key: str) -> None:
    if storage.enabled():
        storage.delete_key(key)
    else:
        (PROJECT_ROOT / key).unlink(missing_ok=True)


def _queue_keys() -> list[str]:
    if storage.enabled():
        return sorted(storage.list_keys(QUEUE_PREFIX))
    local = DATA_DIR / "tg_updates"
    return sorted(
        f"{QUEUE_PREFIX}{p.name}" for p in local.glob("*.json")
    ) if local.is_dir() else []


def load_queue() -> list[tuple[str, dict]]:
    """Pending (key, update) pairs, oldest first, deduped by update_id.

    Unparseable objects are deleted and skipped — they can never succeed."""
    out: list[tuple[str, dict]] = []
    seen: set[int] = set()
    for key in _queue_keys():
        raw = _read_queue_object(key)
        if raw is None:
            continue
        try:
            update = json.loads(raw)
            assert isinstance(update, dict)
        except (ValueError, AssertionError):
            _delete_queue_object(key)
            continue
        uid = update.get("update_id")
        if isinstance(uid, int) and uid in seen:
            _delete_queue_object(key)
            continue
        if isinstance(uid, int):
            seen.add(uid)
        out.append((key, update))
    return out


# ------------------------------------------------------------------ replies


def _send(text: str, chat_id, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] -> {chat_id}: {text[:200]}")
        return
    telegram.send_message(text, chat_id, parse_mode=None)


def _typing(chat_id) -> None:
    """Best-effort typing indicator — the Actions cold start is ~30-90s."""
    try:
        telegram._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ------------------------------------------------------------------ linking


def _match_link_code(code: str, users: list[fanout.NotifyUser]):
    """The user whose prefs hold this pending, unexpired link code, or None."""
    if not code:
        return None
    now = time.time()
    for user in users:
        if (user.prefs.get("tg_link_code") == code
                and now - user.prefs.get("tg_link_ts", 0) <= LINK_TTL):
            return user
    return None


def _complete_link(user: fanout.NotifyUser, chat: dict, dry_run: bool) -> str:
    from stocks.web import auth
    from stocks.web.i18n import translate

    if dry_run:
        return f"dry: would link {user.label} to chat {chat.get('id')}"
    prefs = user.prefs
    prefs["telegram_chat_id"] = chat["id"]
    prefs["telegram_username"] = chat.get("username") or ""
    prefs["telegram_linked_at"] = int(time.time())
    prefs.pop("tg_link_code", None)
    prefs.pop("tg_link_ts", None)
    auth.save_prefs(prefs, user.prefs_path)
    _send(translate("notify.tg_linked", user.lang), chat["id"], dry_run)
    return f"linked {user.label}"


# ------------------------------------------------------------------- drain


def handle_update(update: dict, users: list[fanout.NotifyUser],
                  by_chat: dict[str, fanout.NotifyUser],
                  restored: set[str], dry_run: bool) -> str:
    """Process one Telegram update; returns a status line for the run log."""
    from stocks.web import auth
    from stocks.web.i18n import translate

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return "ignored: no text"
    if chat.get("type", "private") != "private":
        return "ignored: group chat"

    user = by_chat.get(str(chat_id))
    # Language: the linked account's pref, else the sender's Telegram client
    # language for pre-link replies.
    lang = user.lang if user else (
        (msg.get("from") or {}).get("language_code") or "en")

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        pending = _match_link_code(code, users)
        if pending is not None:
            result = _complete_link(pending, chat, dry_run)
            if not dry_run:
                by_chat[str(chat_id)] = pending
            return result
        if user is not None:  # already linked; bare /start or stale code
            _send(translate("notify.chat_help", lang), chat_id, dry_run)
            return "help (linked)"
        _send(translate("notify.link_failed" if code else "notify.chat_not_linked",
                        lang), chat_id, dry_run)
        return "link failed" if code else "not linked"

    if text.startswith("/help"):
        _send(translate("notify.chat_help" if user else "notify.chat_not_linked",
                        lang), chat_id, dry_run)
        return "help"

    if user is None:
        _send(translate("notify.chat_not_linked", lang), chat_id, dry_run)
        return "not linked"

    if text.startswith("/clear"):
        if dry_run:
            return f"dry: would clear {user.label}'s thread"
        auth.save_chat([], user.chat_path)
        _send(translate("notify.chat_cleared", lang), chat_id, dry_run)
        return f"cleared {user.label}"

    if dry_run:
        return f"dry: would answer {user.label}: {text[:80]}"

    # The bare checkout has no chat.json — pull the account's thread down
    # once per run before the engine appends to it.
    if user.label not in restored:
        restored.add(user.label)
        storage.restore(user.chat_path)
    _typing(chat_id)
    reply = engine.answer(
        prefs=user.prefs, prefs_path=user.prefs_path, chat_path=user.chat_path,
        watchlist=user.watchlist, db=user.db, message=text, lang=user.lang,
    )
    if reply.error:
        _send(translate(reply.error, lang, cap=engine.free_daily_cap(),
                        provider=""), chat_id, dry_run)
        return f"{user.label}: {reply.error}"
    _send(reply.text, chat_id, dry_run)
    return f"answered {user.label} via {reply.provider_id}"


def drain(dry_run: bool = False) -> dict[str, str]:
    """Process the whole queue; returns {key: status} for the job log.

    Re-lists after each pass so messages that arrive mid-run are answered in
    the same run. Every processed key is deleted (even on failure) so a
    poison update can't wedge the queue; dry-run deletes nothing and runs one
    pass only.
    """
    from stocks.web.i18n import translate

    users = fanout.iter_all_users()
    by_chat = {
        str(u.prefs["telegram_chat_id"]): u
        for u in users if u.prefs.get("telegram_chat_id")
    }
    status: dict[str, str] = {}
    restored: set[str] = set()
    while True:
        batch = [(k, u) for k, u in load_queue() if k not in status]
        if not batch:
            break
        for key, update in batch:
            try:
                status[key] = handle_update(update, users, by_chat,
                                            restored, dry_run)
            except telegram.TelegramBlocked:
                status[key] = "blocked"
            except Exception as exc:  # noqa: BLE001 — per-update isolation
                status[key] = f"error: {exc}"
                try:  # best-effort: tell the user something went wrong
                    chat_id = ((update.get("message") or {})
                               .get("chat") or {}).get("id")
                    if chat_id and not dry_run:
                        _send(translate("notify.chat_error", "en"),
                              chat_id, dry_run)
                except Exception:
                    pass
            finally:
                if not dry_run:
                    _delete_queue_object(key)
            time.sleep(0.2)  # stay far below Telegram's global send rate
        if dry_run:
            break
    return status
