"""Headless LLM digest highlight (stocks.notify.narrative)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import pytest

from stocks.notify import narrative
from stocks.notify.digest import DigestData


@dataclass
class FakeProvider:
    id: str
    reply: str = ""
    fail: bool = False
    is_available: bool = True
    calls: list = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def complete(self, api_key, model, system, messages) -> str:
        self.calls.append((api_key, model))
        if self.fail:
            raise RuntimeError("rate limited")
        return self.reply


def data() -> DigestData:
    return DigestData(date=date(2026, 8, 14), total=1000.0,
                      day=(10.0, 0.01), movers=[("NVDA", 0.03)])


@pytest.fixture
def providers(monkeypatch):
    from stocks.web import llm

    fakes = {
        "anthropic": FakeProvider("anthropic", reply="BYOK line."),
        "openai": FakeProvider("openai", reply="OpenAI line."),
        "gemini": FakeProvider("gemini", reply="Gemini line."),
        "free": FakeProvider("free", reply="Free line."),
    }
    monkeypatch.setattr(llm, "PROVIDERS", fakes)
    return fakes


@pytest.fixture
def enc(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CHAT_ENC_KEY", key)
    f = Fernet(key)

    def prefs_with_key(pid: str, api_key: str = "sk-user", saved_ago: int = 0) -> dict:
        return {
            f"{pid}_key_enc": f.encrypt(api_key.encode()).decode(),
            f"{pid}_key_saved_at": int(time.time()) - saved_ago,
        }

    return prefs_with_key


def test_byok_preferred_over_free(providers, enc):
    prefs = {"llm_provider": "anthropic", **enc("anthropic")}
    assert narrative.highlight(data(), prefs, "en") == "BYOK line."
    assert providers["anthropic"].calls == [("sk-user", "")]
    assert providers["free"].calls == []


def test_expired_byok_falls_to_free(providers, enc):
    prefs = enc("anthropic", saved_ago=narrative._TTL + 60)
    assert narrative.highlight(data(), prefs, "en") == "Free line."
    assert providers["anthropic"].calls == []


def test_byok_failure_falls_through_to_free(providers, enc):
    providers["anthropic"].fail = True
    prefs = enc("anthropic")
    assert narrative.highlight(data(), prefs, "en") == "Free line."
    assert providers["anthropic"].calls  # attempted first


def test_no_keys_no_free_returns_none(providers):
    providers["free"].is_available = False
    assert narrative.highlight(data(), {}, "en") is None


def test_output_sanitized_and_truncated(providers, enc):
    providers["free"].reply = "line one\n\nline   two   " + "x" * 400
    out = narrative.highlight(data(), {}, "en")
    assert "\n" not in out
    assert "line one line two" in out
    assert len(out) <= narrative.MAX_CHARS


def test_wrong_enc_key_falls_to_free(providers, enc, monkeypatch):
    prefs = enc("anthropic")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CHAT_ENC_KEY", Fernet.generate_key().decode())  # rotated
    assert narrative.highlight(data(), prefs, "en") == "Free line."
    assert providers["anthropic"].calls == []
