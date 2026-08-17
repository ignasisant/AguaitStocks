"""Headless chat engine (stocks.chat.engine) — shared by web panel + Telegram."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest

from stocks.chat import engine


@dataclass
class FakeProvider:
    id: str
    reply: str = "Answer."
    fail: bool = False
    is_available: bool = True
    classifier_model: str = "cheap"
    default_model: str = "default-model"
    calls: list = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def complete(self, api_key, model, system, messages) -> str:
        self.calls.append((api_key, model, system, messages))
        if self.fail:
            raise RuntimeError("rate limited")
        return self.reply


@pytest.fixture
def providers(monkeypatch):
    from stocks.web import llm

    fakes = {
        "anthropic": FakeProvider("anthropic", reply="BYOK answer."),
        "openai": FakeProvider("openai"),
        "gemini": FakeProvider("gemini"),
        "free": FakeProvider("free", reply="Free answer."),
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


# ---------------------------------------------------------------- persona


def test_persona_default_when_unset():
    assert "aggressive long-term (5y+)" in engine.persona({"set": False})


def test_persona_from_profile():
    out = engine.persona({
        "set": True, "risk": "balanced", "horizon": "1_3y",
        "focus": ["tech", "em"], "constraints": ["eur"], "notes": "prefers ETFs",
    })
    assert "a balanced investor" in out
    assert "1–3 year" in out
    assert "technology and growth stocks and emerging markets" in out
    assert "reason and report in EUR" in out
    assert "prefers ETFs" in out


# ----------------------------------------------------------------- recent


def test_recent_trims_and_strips():
    history = [{"role": "assistant", "content": "hello", "skills": ["tech"]}]
    history += [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i),
         "skills": ["tech"], "web": [{"title": "t", "url": "u"}]}
        for i in range(engine.MAX_CONTEXT_MSGS + 5)
    ]
    out = engine.recent(history)
    assert len(out) <= engine.MAX_CONTEXT_MSGS
    assert out[0]["role"] == "user"
    assert all(set(m) == {"role", "content"} for m in out)


# ---------------------------------------------------------- system prompt


def test_system_prompt_layers():
    prof = {"set": False}
    out = engine.system_prompt(prof, "CONTEXT-BLOCK", ["tech"])
    assert "aggressive long-term" in out
    assert "CONTEXT-BLOCK" in out
    assert "Apply these analysis frameworks" in out
    assert "Apply these analysis frameworks" not in engine.system_prompt(prof, "x", [])


# --------------------------------------------------------------- attempts


def test_attempts_preferred_byok_then_free(providers, enc):
    prefs = {"llm_provider": "gemini", **enc("gemini"), **enc("anthropic"),
             "gemini_model": "g-pro"}
    atts = engine.attempts(prefs)
    assert [(p.id, m) for p, _, m in atts] == [
        ("gemini", "g-pro"), ("anthropic", ""), ("free", "")]


def test_attempts_expired_key_skipped(providers, enc):
    prefs = enc("anthropic", saved_ago=engine.BYOK_TTL + 60)
    assert [p.id for p, _, _ in engine.attempts(prefs)] == ["free"]


def test_attempts_empty_when_no_free(providers):
    providers["free"].is_available = False
    assert engine.attempts({}) == []


# ------------------------------------------------------------- free quota


def test_free_daily_cap_env_override(monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "2")
    assert engine.free_daily_cap() == 2
    monkeypatch.delenv("FREE_LLM_DAILY_CAP")
    assert engine.free_daily_cap() == engine.FREE_DAILY_CAP


def test_spend_free_quota_counts_and_sweeps(monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "2")
    prefs = {"free_msgs::2000-01-01": 9}
    assert engine.spend_free_quota(prefs)
    assert "free_msgs::2000-01-01" not in prefs  # stale day swept
    assert engine.spend_free_quota(prefs)
    assert not engine.spend_free_quota(prefs)  # cap reached
    day_key = f"free_msgs::{time.strftime('%Y-%m-%d')}"
    assert prefs[day_key] == 2


# ------------------------------------------------------------ skill routing


def test_resolve_skills_modes(providers):
    p = providers["free"]
    history = [{"role": "user", "content": "tech question"}]
    assert engine.resolve_skills({"chat_skills_mode": "off"}, p, "", history) == []
    manual = {"chat_skills_mode": "manual", "chat_skills": ["tech", "nope"]}
    assert engine.resolve_skills(manual, p, "", history) == ["tech"]


def test_resolve_skills_auto_falls_back_to_previous(providers):
    p = providers["free"]
    p.fail = True  # router call fails -> reuse the previous answer's lens
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "skills": ["macro"]},
        {"role": "user", "content": "q2"},
    ]
    assert engine.resolve_skills({}, p, "", history) == ["macro"]


# ------------------------------------------------------------------ answer


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "watchlist.yaml").write_text("watchlist: []\n")
    return {
        "prefs_path": tmp_path / "prefs.json",
        "chat_path": tmp_path / "chat.json",
        "watchlist": tmp_path / "watchlist.yaml",
        "db": tmp_path / "portfolio.db",  # never created -> watchlist fallback
    }


BASE_PREFS = {"chat_skills_mode": "off", "chat_web": False}


def test_answer_free_happy_path(providers, paths):
    prefs = dict(BASE_PREFS)
    reply = engine.answer(prefs=prefs, message="How is my portfolio doing?",
                          **paths)
    assert reply.text == "Free answer." and reply.error is None
    assert reply.provider_id == "free"
    saved = json.loads(paths["chat_path"].read_text())
    assert [m["role"] for m in saved] == ["user", "assistant"]
    # quota spent and persisted
    day_key = f"free_msgs::{time.strftime('%Y-%m-%d')}"
    assert json.loads(paths["prefs_path"].read_text())[day_key] == 1
    # free chain gets its default model and the book snapshot in the system
    _, model, system, _ = providers["free"].calls[-1]
    assert model == "default-model"
    assert "no open positions" in system
    assert "Telegram" in system  # TELEGRAM_CONTEXT rides along


def test_answer_byok_first_then_free_on_failure(providers, enc, paths):
    prefs = {**BASE_PREFS, **enc("anthropic")}
    providers["anthropic"].fail = True
    reply = engine.answer(prefs=prefs, message="thoughts on my book?", **paths)
    assert reply.provider_id == "free"
    assert providers["anthropic"].calls  # attempted first, with default model
    assert providers["anthropic"].calls[0][1] == "default-model"


def test_answer_cap_reached_saves_nothing(providers, paths, monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "0")
    reply = engine.answer(prefs=dict(BASE_PREFS), message="hola", **paths)
    assert reply.error == "chat.free_cap"
    assert not paths["chat_path"].exists()


def test_answer_no_providers(providers, paths):
    providers["free"].is_available = False
    reply = engine.answer(prefs=dict(BASE_PREFS), message="hola", **paths)
    assert reply.error == "chat.free_exhausted"
    assert not paths["chat_path"].exists()


def test_answer_all_fail_is_api_error(providers, enc, paths):
    providers["free"].is_available = False
    providers["anthropic"].fail = True
    prefs = {**BASE_PREFS, **enc("anthropic")}
    reply = engine.answer(prefs=prefs, message="hola", **paths)
    assert reply.error == "chat.api_error"
    assert not paths["chat_path"].exists()


def test_answer_appends_to_existing_thread(providers, paths):
    paths["chat_path"].write_text(json.dumps(
        [{"role": "user", "content": "old"},
         {"role": "assistant", "content": "old answer"}]))
    engine.answer(prefs=dict(BASE_PREFS), message="follow-up", **paths)
    saved = json.loads(paths["chat_path"].read_text())
    assert len(saved) == 4 and saved[2]["content"] == "follow-up"
    # the model saw the prior turns
    *_, messages = providers["free"].calls[-1]
    assert [m["content"] for m in messages] == ["old", "old answer", "follow-up"]
