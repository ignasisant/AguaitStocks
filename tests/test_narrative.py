"""Headless LLM lines for the notification jobs (stocks.notify.narrative)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import pytest

from stocks.chat import engine
from stocks.notify import narrative
from stocks.notify.alerts import AlertHit
from stocks.notify.digest import DigestData


@dataclass
class FakeProvider:
    id: str
    reply: str = ""
    fail: bool = False
    is_available: bool = True
    calls: list = field(default_factory=list)
    systems: list = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def complete(self, api_key, model, system, messages) -> str:
        self.calls.append((api_key, model))
        self.systems.append(system)
        if self.fail:
            raise RuntimeError("rate limited")
        return self.reply


@pytest.fixture(autouse=True)
def free_pot(monkeypatch, tmp_path):
    """Isolate the process-wide free pot — these jobs now spend it for real."""
    monkeypatch.setattr(engine, "GLOBAL_FREE_FILE", tmp_path / "free_llm_global.json")
    monkeypatch.setattr(engine, "_global_free", {"day": "", "used": 0})
    monkeypatch.setattr(engine, "_global_free_loaded", True)


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


# ------------------------------------------------------------ free-tier pot


def test_free_attempt_spends_the_global_pot(providers, monkeypatch):
    monkeypatch.setenv("FREE_LLM_GLOBAL_DAILY_CAP", "1")
    assert narrative.highlight(data(), {}, "en") == "Free line."
    assert engine._global_free["used"] == 1
    # Pot empty: the next digest ships computed-only instead of draining the
    # operator's shared keys.
    assert narrative.highlight(data(), {}, "en") is None
    assert providers["free"].calls == [("", "")]


def test_byok_never_touches_the_pot(providers, enc, monkeypatch):
    monkeypatch.setenv("FREE_LLM_GLOBAL_DAILY_CAP", "0")
    prefs = {"llm_provider": "anthropic", **enc("anthropic")}
    assert narrative.highlight(data(), prefs, "en") == "BYOK line."
    assert engine._global_free["used"] == 0  # the user's own billing


# ---------------------------------------------------------- anti-repetition


def test_recent_highlights_go_into_the_prompt(providers):
    narrative.highlight(data(), {}, "en", recent=["Yesterday NVDA led."])
    assert "Yesterday NVDA led." in providers["free"].systems[0]


def test_repeated_line_is_dropped_not_rerolled(providers):
    providers["free"].reply = "NVDA drove the day, up 3 percent."
    out = narrative.highlight(
        data(), {}, "en", recent=["NVDA drove the day, up 5 percent."]
    )
    assert out is None
    assert len(providers["free"].calls) == 1  # dropped, never re-asked


def test_fresh_line_survives_the_guard(providers):
    providers["free"].reply = "Earnings land next week for two holdings."
    out = narrative.highlight(data(), {}, "en", recent=["NVDA drove the day."])
    assert out == "Earnings land next week for two holdings."


# ------------------------------------------------------------- alerts note


def hits() -> list[AlertHit]:
    return [
        AlertHit("NVDA", "above", "closed above 190", value=192.0),
        AlertHit("ASML", "rsi_above", "RSI 72", value=72.0),
    ]


def test_alerts_line_narrates_the_fired_rules(providers):
    providers["free"].reply = "Both are semis extending a run."
    assert narrative.alerts_line(hits(), {}, "en") == "Both are semis extending a run."
    body = providers["free"].systems[0]
    assert "alerts just fired" in body
    assert "Never recommend buying" in body  # no advice out of a price trigger


def test_alerts_line_without_hits_never_calls(providers):
    assert narrative.alerts_line([], {}, "en") is None
    assert providers["free"].calls == []


def test_alerts_line_degrades_to_none(providers):
    providers["free"].fail = True
    assert narrative.alerts_line(hits(), {}, "en") is None


def test_alerts_payload_is_capped(providers):
    many = [AlertHit(f"T{i}", "above", "hit", value=float(i)) for i in range(20)]
    narrative.alerts_line(many, {}, "en")
    _, messages = narrative._alerts_prompt(many, "en")
    import json

    facts = json.loads(messages[0]["content"])
    assert len(facts["alerts"]) == narrative.ALERTS_SHOWN
    assert facts["total_fired"] == 20
