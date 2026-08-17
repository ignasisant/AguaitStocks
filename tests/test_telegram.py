"""Telegram Bot API client (stocks.notify.telegram) and the deliver bridge."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from stocks.notify import deliver, telegram


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(monkeypatch):
    """Capture Bot API calls; respond ok with an empty result by default."""
    calls: list[dict] = []
    responses: list[object] = []

    def fake_urlopen(req, timeout=0):
        params = dict(
            p.split("=", 1) for p in req.data.decode().split("&") if "=" in p
        )
        import urllib.parse

        calls.append(
            {
                "url": req.full_url,
                "params": {k: urllib.parse.unquote_plus(v) for k, v in params.items()},
            }
        )
        if responses:
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return FakeResponse(json.dumps(nxt).encode())
        return FakeResponse(b'{"ok": true, "result": {}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    api = type("Api", (), {"calls": calls, "responses": responses})
    return api


def http_error(code: int, description: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"ok": False, "description": description}).encode()
    return urllib.error.HTTPError("u", code, description, {}, io.BytesIO(body))


# ------------------------------------------------------------ send_message


def test_send_message_payload(api):
    telegram.send_message("<b>hi</b>", chat_id=42)
    (call,) = api.calls
    assert "botTOKEN/sendMessage" in call["url"]
    assert call["params"]["chat_id"] == "42"
    assert call["params"]["text"] == "<b>hi</b>"
    assert call["params"]["parse_mode"] == "HTML"


def test_send_message_plain_text_omits_parse_mode(api):
    telegram.send_message("hi", chat_id=1, parse_mode=None)
    assert "parse_mode" not in api.calls[0]["params"]


def test_send_message_splits_over_4096_on_lines(api):
    long = "\n".join(f"line {i} " + "x" * 100 for i in range(60))
    assert len(long) > telegram.MAX_LEN
    telegram.send_message(long, chat_id=1)
    assert len(api.calls) == 2
    for call in api.calls:
        assert len(call["params"]["text"]) <= telegram.MAX_LEN
    rejoined = "\n".join(c["params"]["text"] for c in api.calls)
    assert rejoined == long


def test_send_message_blocked_raises_typed(api):
    api.responses.append(http_error(403, "Forbidden: bot was blocked by the user"))
    with pytest.raises(telegram.TelegramBlocked):
        telegram.send_message("hi", chat_id=1)


def test_send_message_api_not_ok_raises(api):
    api.responses.append({"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(RuntimeError, match="chat not found"):
        telegram.send_message("hi", chat_id=1)


# ------------------------------------------------------------- get_updates


def test_get_updates_conflict_returns_empty(api):
    api.responses.append(http_error(409, "Conflict: terminated by other getUpdates"))
    assert telegram.get_updates() == []


def test_get_updates_network_blip_returns_empty(api):
    api.responses.append(urllib.error.URLError("timed out"))
    assert telegram.get_updates() == []


# ------------------------------------------------------------- match_start


def _update(text, chat_id=7, username="jane"):
    return {"message": {"text": text, "chat": {"id": chat_id, "username": username}}}


def test_match_start_finds_code():
    chat = telegram.match_start([_update("/start abc123")], "abc123")
    assert chat == {"id": 7, "username": "jane"}


def test_match_start_prefers_newest():
    updates = [_update("/start code", chat_id=1), _update("/start code", chat_id=2)]
    assert telegram.match_start(updates, "code")["id"] == 2


def test_match_start_rejects_wrong_or_empty_code():
    updates = [_update("/start abc123"), {"message": {"chat": {"id": 9}}}]
    assert telegram.match_start(updates, "other") is None
    assert telegram.match_start(updates, "") is None


# ------------------------------------------------------------ deep link etc.


def test_deep_link_uses_secret_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@AguaitBot")
    assert telegram.deep_link("xyz") == "https://t.me/AguaitBot?start=xyz"


def test_configured_tracks_token(monkeypatch):
    import os

    # Isolate from a developer's local .streamlit/secrets.toml — secret()
    # falls back to it when the env var is unset.
    monkeypatch.setattr(telegram, "secret",
                        lambda env, *a, **k: os.environ.get(env, ""))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert not telegram.configured()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    assert telegram.configured()


# ---------------------------------------------------------------- deliver()


def test_deliver_env_path_unchanged(api, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    status = deliver.deliver(["AAPL: above 200"], subject="Stock alerts")
    assert status["telegram"] == "sent"
    (call,) = api.calls
    assert call["params"]["chat_id"] == "99"
    assert call["params"]["text"] == "Stock alerts\nAAPL: above 200"
    assert "parse_mode" not in call["params"]
