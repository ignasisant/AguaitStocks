"""Remembered BYOK keys in the assistant panel (web/chat_core.py).

The window is sliding: reading a live key pushes its expiry out, reading any
prefs at all deletes the keys that already died. Both halves matter — the
second is what stops an abandoned account from holding a decryptable provider
key in prefs.json (and in the bucket mirror) forever.
"""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from stocks.chat import engine
from stocks.web import chat_core


@pytest.fixture
def prefs_store(monkeypatch):
    """An in-memory stand-in for the account's prefs.json."""
    store: dict = {}
    saves = []

    monkeypatch.setenv("CHAT_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(chat_core.auth, "load_prefs", lambda: dict(store))

    def save(prefs, path=None):
        store.clear()
        store.update(prefs)
        saves.append(dict(prefs))

    monkeypatch.setattr(chat_core.auth, "save_prefs", save)
    monkeypatch.setattr(chat_core.st, "session_state", {}, raising=False)
    return store, saves


def test_save_stamps_both_clocks(prefs_store):
    store, _ = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    assert store["anthropic_key_saved_at"] == store["anthropic_key_first_at"]
    assert chat_core._load_saved_key("anthropic") == "sk-user"


def test_reading_a_stale_key_slides_it(prefs_store):
    store, saves = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    old = int(time.time()) - 60 * 24 * 3600
    store["anthropic_key_saved_at"] = old
    first = store["anthropic_key_first_at"]

    assert chat_core._load_saved_key("anthropic") == "sk-user"
    assert store["anthropic_key_saved_at"] > old
    assert store["anthropic_key_first_at"] == first  # the cap never moves
    assert len(saves) == 2  # the save, then the slide


def test_reading_twice_in_a_day_writes_once(prefs_store):
    store, saves = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    for _ in range(3):
        assert chat_core._load_saved_key("anthropic") == "sk-user"
    assert len(saves) == 1  # only the initial save; the slide is throttled


def test_expired_key_is_deleted_not_just_refused(prefs_store):
    store, _ = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    store["anthropic_key_saved_at"] = int(time.time()) - engine.BYOK_TTL - 60
    store["anthropic_key_first_at"] = store["anthropic_key_saved_at"]

    assert chat_core._load_saved_key("anthropic") == ""
    assert not any(k.startswith("anthropic_key") for k in store)


def test_capped_key_is_deleted_however_recently_used(prefs_store):
    store, _ = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    store["anthropic_key_first_at"] = int(time.time()) - engine.BYOK_MAX_AGE - 60

    assert chat_core._load_saved_key("anthropic") == ""
    assert not any(k.startswith("anthropic_key") for k in store)


def test_reading_one_provider_prunes_another(prefs_store):
    store, _ = prefs_store
    chat_core._save_key("anthropic", "sk-a")
    chat_core._save_key("openai", "sk-o")
    store["openai_key_saved_at"] = int(time.time()) - engine.BYOK_TTL - 60
    store["openai_key_first_at"] = store["openai_key_saved_at"]

    assert chat_core._load_saved_key("anthropic") == "sk-a"
    assert not any(k.startswith("openai_key") for k in store)


def test_forget_wipes_every_field(prefs_store):
    store, _ = prefs_store
    chat_core._save_key("anthropic", "sk-user")
    chat_core._forget_key("anthropic")
    assert not any(k.startswith("anthropic_key") for k in store)
