"""Telegram chat bot: queue drain, linking, commands (stocks.chat.bot)."""

from __future__ import annotations

import json
import time

import pytest

from stocks.chat import bot
from stocks.chat.engine import Reply
from stocks.notify.fanout import NotifyUser
from stocks.web import auth


def _update(uid: int, chat_id: int, text: str, chat_type: str = "private") -> dict:
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat_id, "type": chat_type, "username": "jane"},
            "from": {"language_code": "en"},
            "text": text,
        },
    }


def _queue(local, *updates: dict) -> None:
    qdir = local / "data" / "tg_updates"
    qdir.mkdir(parents=True, exist_ok=True)
    for u in updates:
        (qdir / f"{u['update_id']:012d}.json").write_text(json.dumps(u))


def _user(local, label: str, prefs: dict) -> NotifyUser:
    root = local / "data" / "users" / label
    root.mkdir(parents=True, exist_ok=True)
    (root / "prefs.json").write_text(json.dumps(prefs))
    (root / "watchlist.yaml").write_text("watchlist: []\n")
    return NotifyUser(
        label=label, prefs=prefs, watchlist=root / "watchlist.yaml",
        db=root / "portfolio.db", prefs_path=root / "prefs.json",
        state_path=root / "alerts_state.json",
    )


@pytest.fixture
def sent(monkeypatch):
    """Captured outgoing bot messages [(text, chat_id), ...]."""
    out: list[tuple[str, object]] = []
    monkeypatch.setattr(
        bot.telegram, "send_message",
        lambda text, chat_id, parse_mode=None: out.append((text, chat_id)),
    )
    monkeypatch.setattr(bot.telegram, "_call", lambda *a, **k: {})  # typing
    return out


@pytest.fixture
def local(monkeypatch, tmp_path, sent):
    """Local queue (storage disabled) rooted at tmp_path."""
    from stocks import storage

    monkeypatch.setattr(storage, "_cached", {"config": None})
    monkeypatch.setattr(bot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def users(local, monkeypatch):
    linked = _user(local, "jane_ab12cd34", {"telegram_chat_id": 111,
                                            "language": "es"})
    pending = _user(local, "bob_ef56ab78", {"tg_link_code": "codeword",
                                            "tg_link_ts": int(time.time())})
    monkeypatch.setattr(bot.fanout, "iter_all_users",
                        lambda: [linked, pending])
    return {"linked": linked, "pending": pending}


# ------------------------------------------------------------------- queue


def test_queue_key_zero_padded_chronological():
    assert bot.queue_key(42) == "data/tg_updates/000000000042.json"
    assert bot.queue_key(9) < bot.queue_key(10) < bot.queue_key(100)


def test_load_queue_orders_dedupes_and_drops_poison(local):
    _queue(local, _update(2, 1, "b"), _update(1, 1, "a"))
    qdir = local / "data" / "tg_updates"
    (qdir / "000000000003.json").write_text("{not json")
    (qdir / "000000000004.json").write_text(json.dumps(_update(1, 1, "dupe")))

    out = bot.load_queue()
    assert [u["message"]["text"] for _, u in out] == ["a", "b"]
    assert not (qdir / "000000000003.json").exists()  # poison deleted
    assert not (qdir / "000000000004.json").exists()  # dupe deleted


# ------------------------------------------------------------------- drain


def test_drain_answers_linked_user(local, users, sent, monkeypatch):
    seen = {}

    def fake_answer(**kwargs):
        seen.update(kwargs)
        return Reply(text="hola cartera", provider_id="free")

    monkeypatch.setattr(bot.engine, "answer", fake_answer)
    _queue(local, _update(10, 111, "¿cómo va mi cartera?"))

    status = bot.drain()
    assert status == {bot.queue_key(10): "answered jane_ab12cd34 via free"}
    assert ("hola cartera", 111) in sent
    assert seen["lang"] == "es"
    assert seen["chat_path"] == users["linked"].chat_path
    assert not list((local / "data" / "tg_updates").glob("*.json"))  # consumed


def test_drain_unknown_chat_gets_link_hint(local, users, sent):
    _queue(local, _update(11, 999, "hello?"))
    status = bot.drain()
    assert status[bot.queue_key(11)] == "not linked"
    text, chat_id = sent[0]
    assert chat_id == 999 and "isn't linked" in text


def test_drain_ignores_groups_and_non_text(local, users, sent):
    _queue(local, _update(12, 111, "hey", chat_type="group"),
           {"update_id": 13, "message": {"chat": {"id": 111}}})
    status = bot.drain()
    assert status[bot.queue_key(12)] == "ignored: group chat"
    assert status[bot.queue_key(13)] == "ignored: no text"
    assert sent == []


def test_drain_start_code_links_account(local, users, sent):
    _queue(local, _update(14, 222, "/start codeword"))
    status = bot.drain()
    assert status[bot.queue_key(14)] == "linked bob_ef56ab78"
    saved = json.loads(users["pending"].prefs_path.read_text())
    assert saved["telegram_chat_id"] == 222
    assert saved["telegram_username"] == "jane"
    assert "tg_link_code" not in saved
    assert any(chat_id == 222 and "Telegram" in text
               for text, chat_id in sent)


def test_drain_start_expired_or_wrong_code(local, users, monkeypatch):
    users["pending"].prefs["tg_link_ts"] = int(time.time()) - bot.LINK_TTL - 1
    _queue(local, _update(15, 222, "/start codeword"),
           _update(16, 333, "/start nope"))
    status = bot.drain()
    assert status[bot.queue_key(15)] == "link failed"
    assert status[bot.queue_key(16)] == "link failed"
    saved = json.loads(users["pending"].prefs_path.read_text())
    assert "telegram_chat_id" not in saved


def test_drain_clear_resets_shared_thread(local, users):
    users["linked"].chat_path.write_text(json.dumps(
        [{"role": "user", "content": "x"}]))
    _queue(local, _update(17, 111, "/clear"))
    status = bot.drain()
    assert status[bot.queue_key(17)] == "cleared jane_ab12cd34"
    assert auth.load_chat(users["linked"].chat_path) == []


def test_drain_engine_error_reported_and_queue_consumed(local, users, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("engine down")

    monkeypatch.setattr(bot.engine, "answer", boom)
    _queue(local, _update(18, 111, "hi"), _update(19, 999, "hello?"))
    status = bot.drain()
    assert status[bot.queue_key(18)].startswith("error: engine down")
    assert status[bot.queue_key(19)] == "not linked"  # others still processed
    assert not list((local / "data" / "tg_updates").glob("*.json"))


def test_drain_error_reply_uses_locale(local, users, sent, monkeypatch):
    monkeypatch.setattr(
        bot.engine, "answer",
        lambda **kw: Reply(error="chat.free_cap"),
    )
    _queue(local, _update(20, 111, "hi"))
    status = bot.drain()
    assert status[bot.queue_key(20)] == "jane_ab12cd34: chat.free_cap"
    text, chat_id = sent[0]
    assert chat_id == 111 and str(bot.engine.free_daily_cap()) in text


def test_dry_run_sends_and_deletes_nothing(local, users, sent, monkeypatch):
    monkeypatch.setattr(
        bot.engine, "answer",
        lambda **kw: pytest.fail("dry run must not call the engine"),
    )
    _queue(local, _update(21, 111, "hi"), _update(22, 222, "/start codeword"))
    status = bot.drain(dry_run=True)
    assert status[bot.queue_key(21)].startswith("dry: would answer")
    assert status[bot.queue_key(22)].startswith("dry: would link")
    assert sent == []
    assert len(list((local / "data" / "tg_updates").glob("*.json"))) == 2
    saved = json.loads(users["pending"].prefs_path.read_text())
    assert "telegram_chat_id" not in saved
