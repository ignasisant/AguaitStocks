"""Headless chat engine (stocks.chat.engine) — shared by web panel + Telegram."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest

from stocks.chat import engine
from stocks.web import auth


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


# ------------------------------------------------- sliding window + prune


def test_touch_slides_window_and_backfills_origin(enc):
    old = int(time.time()) - 60 * 24 * 3600
    prefs = enc("anthropic", saved_ago=60 * 24 * 3600)
    assert engine.touch_byok(prefs, "anthropic") is True
    assert prefs["anthropic_key_saved_at"] >= time.time() - 5
    # legacy entry (no _key_first_at): the cap counts from its original save
    assert abs(prefs["anthropic_key_first_at"] - old) <= 5


def test_touch_throttled_within_a_day(enc):
    prefs = enc("anthropic", saved_ago=3600)
    before = dict(prefs)
    assert engine.touch_byok(prefs, "anthropic") is False
    assert prefs == before


def test_touch_refuses_expired_key(enc):
    prefs = enc("anthropic", saved_ago=engine.BYOK_TTL + 60)
    assert engine.touch_byok(prefs, "anthropic") is False


def test_absolute_cap_beats_a_slid_window(providers, enc):
    prefs = enc("anthropic")  # saved just now...
    # ...but first entered beyond the hard ceiling: no amount of use revives it
    prefs["anthropic_key_first_at"] = int(time.time()) - engine.BYOK_MAX_AGE - 60
    assert engine.decrypt_byok(prefs, "anthropic") == ""
    assert [p.id for p, _, _ in engine.attempts(prefs)] == ["free"]
    assert engine.touch_byok(prefs, "anthropic") is False


def test_prune_deletes_expired_ciphertext_only(enc):
    prefs = {**enc("anthropic", saved_ago=engine.BYOK_TTL + 60), **enc("openai")}
    assert engine.prune_byok(prefs) is True
    assert not any(k.startswith("anthropic_key") for k in prefs)
    assert prefs["openai_key_enc"]  # live key untouched
    assert engine.prune_byok(prefs) is False  # nothing left to drop


def test_maintain_slides_the_used_key_and_prunes_the_dead(enc):
    prefs = {**enc("anthropic", saved_ago=60 * 24 * 3600),
             **enc("openai", saved_ago=engine.BYOK_TTL + 60)}
    assert engine.maintain_byok(prefs, "anthropic") is True
    assert prefs["anthropic_key_saved_at"] >= time.time() - 5
    assert not any(k.startswith("openai_key") for k in prefs)


def test_maintain_without_pid_only_prunes(enc):
    prefs = enc("anthropic", saved_ago=60 * 24 * 3600)
    before = dict(prefs)
    assert engine.maintain_byok(prefs) is False
    assert prefs == before


# ------------------------------------------------------------- free quota


def test_free_daily_cap_env_override(monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "2")
    assert engine.free_daily_cap() == 2
    monkeypatch.delenv("FREE_LLM_DAILY_CAP")
    assert engine.free_daily_cap() == engine.FREE_DAILY_CAP


def test_spend_free_quota_counts_and_sweeps(monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "2")
    monkeypatch.setattr(engine, "_global_free", {"day": "", "used": 0})
    prefs = {"free_msgs::2000-01-01": 9}
    assert engine.spend_free_quota(prefs)
    assert "free_msgs::2000-01-01" not in prefs  # stale day swept
    assert engine.spend_free_quota(prefs)
    assert not engine.spend_free_quota(prefs)  # cap reached
    day_key = f"free_msgs::{time.strftime('%Y-%m-%d')}"
    assert prefs[day_key] == 2


def test_global_free_cap_backstops_across_accounts(monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "10")
    monkeypatch.setenv("FREE_LLM_GLOBAL_DAILY_CAP", "3")
    monkeypatch.setattr(engine, "_global_free", {"day": "", "used": 0})
    # Three different accounts, each well under their own cap...
    accounts = [{}, {}, {}, {}]
    spent = [engine.spend_free_quota(p) for p in accounts]
    # ...but the pot is shared: the fourth account finds it empty.
    assert spent == [True, True, True, False]
    assert accounts[3] == {}  # the refused turn charged nobody


def test_global_free_cap_resets_at_utc_midnight(monkeypatch):
    monkeypatch.setenv("FREE_LLM_GLOBAL_DAILY_CAP", "1")
    monkeypatch.setattr(
        engine, "_global_free", {"day": "1999-12-31", "used": 99}
    )
    assert engine.spend_free_quota({})  # new day -> counter forgotten


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


# ------------------------------------------------------------ thread titles


def test_title_for_strips_quotes_and_punctuation(providers):
    providers["free"].reply = '  "NVDA valuation."  '
    out = engine.title_for(providers["free"], "", "thoughts on NVDA?")
    assert out == "NVDA valuation"


def test_title_for_falls_back_to_the_message(providers):
    providers["free"].fail = True
    out = engine.title_for(providers["free"], "", "  how   is my book doing?  ")
    assert out == "how is my book doing?"


def test_title_for_uses_the_cheap_model(providers):
    engine.title_for(providers["free"], "k", "hi")
    assert providers["free"].calls[-1][1] == "cheap"


def test_autotitle_leaves_a_renamed_thread_alone(providers, tmp_path):
    from stocks.web import auth

    chat = tmp_path / "chat.json"
    auth.save_chat([{"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"}], chat)
    auth.rename_conversation(auth.active_conversation(chat)["id"], "Mine", chat)

    engine.autotitle(chat, providers["free"], "", auth.load_chat(chat))
    assert auth.active_conversation(chat)["title"] == "Mine"
    assert not providers["free"].calls  # not even asked


def test_autotitle_only_fires_on_the_opening_pair(providers, tmp_path):
    from stocks.web import auth

    chat = tmp_path / "chat.json"
    history = [{"role": "user", "content": "q1"},
               {"role": "assistant", "content": "a1"},
               {"role": "user", "content": "q2"},
               {"role": "assistant", "content": "a2"}]
    auth.save_chat(history, chat)

    engine.autotitle(chat, providers["free"], "", history)
    assert auth.active_conversation(chat)["title"] == ""


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


@pytest.fixture(autouse=True)
def no_quote_lookups(monkeypatch):
    """Live quotes are on by default in answer(); no test may hit Yahoo.

    Tests that care about the injected block override this with their own
    stub (see test_answer_injects_live_quotes)."""
    monkeypatch.setattr(engine.market, "lookup_for", lambda *a, **k: [])


def test_answer_free_happy_path(providers, paths):
    prefs = dict(BASE_PREFS)
    reply = engine.answer(prefs=prefs, message="How is my portfolio doing?",
                          **paths)
    assert reply.text == "Free answer." and reply.error is None
    assert reply.provider_id == "free"
    saved = auth.load_chat(paths["chat_path"])
    assert [m["role"] for m in saved] == ["user", "assistant"]
    # quota spent and persisted
    day_key = f"free_msgs::{time.strftime('%Y-%m-%d')}"
    assert json.loads(paths["prefs_path"].read_text())[day_key] == 1
    # free chain gets its default model and the book snapshot in the system.
    # calls[0] is the answer; the autotitle call follows it on the cheap model.
    _, model, system, _ = providers["free"].calls[0]
    assert model == "default-model"
    assert "no open positions" in system
    assert "Telegram" in system  # TELEGRAM_CONTEXT rides along
    # the brand-new thread was named from the opening question
    assert auth.active_conversation(paths["chat_path"])["title"] == "Free answer"



def test_answer_byok_first_then_free_on_failure(providers, enc, paths):
    prefs = {**BASE_PREFS, **enc("anthropic")}
    providers["anthropic"].fail = True
    reply = engine.answer(prefs=prefs, message="thoughts on my book?", **paths)
    assert reply.provider_id == "free"
    assert providers["anthropic"].calls  # attempted first, with default model
    assert providers["anthropic"].calls[0][1] == "default-model"


def test_answer_slides_the_byok_key_it_used(providers, enc, paths):
    prefs = {**BASE_PREFS, **enc("anthropic", saved_ago=60 * 24 * 3600)}
    reply = engine.answer(prefs=prefs, message="thoughts?", **paths)
    assert reply.provider_id == "anthropic"
    saved = json.loads(paths["prefs_path"].read_text())
    assert saved["anthropic_key_saved_at"] >= time.time() - 5


def test_answer_on_free_prunes_but_slides_nothing(providers, enc, paths):
    dead = enc("anthropic", saved_ago=engine.BYOK_TTL + 60)
    prefs = {**BASE_PREFS, **dead}
    reply = engine.answer(prefs=prefs, message="hola", **paths)
    assert reply.provider_id == "free"
    saved = json.loads(paths["prefs_path"].read_text())
    assert not any(k.startswith("anthropic_key") for k in saved)


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
    saved = auth.load_chat(paths["chat_path"])
    assert len(saved) == 4 and saved[2]["content"] == "follow-up"
    # the model saw the prior turns
    *_, messages = providers["free"].calls[-1]
    assert [m["content"] for m in messages] == ["old", "old answer", "follow-up"]


# --------------------------------------------------------- grounding


def test_web_enabled_follows_the_pref(monkeypatch):
    monkeypatch.setattr(engine.chat_web, "available", lambda: True)
    assert engine.web_enabled({}) is True  # default on
    assert engine.web_enabled({"chat_web": False}) is False
    monkeypatch.setattr(engine.chat_web, "available", lambda: False)
    assert engine.web_enabled({"chat_web": True}) is False


def test_plan_web_carries_the_view_context_and_prior_turns(providers, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine.chat_web, "available", lambda: True)
    monkeypatch.setattr(engine.chat_web, "plan",
                        lambda p, k, q, ctx: seen.update(q=q, ctx=ctx) or ["q"])
    history = [{"role": "user", "content": "how is NVDA?"},
               {"role": "assistant", "content": "fine"},
               {"role": "user", "content": "and today?"}]
    got = engine.plan_web({}, providers["free"], "", history,
                          context="The ticker in focus is NVDA.")
    assert got == ["q"]
    assert seen["q"] == "and today?"
    assert "Today is " in seen["ctx"]
    assert "ticker in focus is NVDA" in seen["ctx"]
    assert "how is NVDA?" in seen["ctx"]  # topic continuity


def test_ground_web_is_off_when_the_pref_is_off(providers, monkeypatch):
    monkeypatch.setattr(engine.chat_web, "collect",
                        lambda *a: pytest.fail("must not search"))
    history = [{"role": "user", "content": "news?"}]
    assert engine.ground_web({"chat_web": False}, providers["free"], "",
                             history) == []


def test_ground_web_reads_planned_and_pasted_pages(providers, monkeypatch):
    monkeypatch.setattr(engine.chat_web, "available", lambda: True)
    monkeypatch.setattr(engine.chat_web, "plan", lambda *a: ["nvda news"])
    monkeypatch.setattr(engine.chat_web, "collect",
                        lambda queries, message: [(queries, message)])
    history = [{"role": "user", "content": "read https://x.example/a"}]
    assert engine.ground_web({}, providers["free"], "", history) == [
        (["nvda news"], "read https://x.example/a")
    ]


def test_in_parallel_keeps_order_and_isolates_failures():
    def boom():
        raise RuntimeError("dead classifier")

    assert engine.in_parallel(lambda: "a", boom, lambda: "c") == ["a", None, "c"]


def test_in_parallel_runs_the_calls_at_the_same_time():
    started = []

    def slow(tag):
        started.append(tag)
        time.sleep(0.15)
        return tag

    t0 = time.monotonic()
    got = engine.in_parallel(lambda: slow("a"), lambda: slow("b"),
                             lambda: slow("c"))
    assert got == ["a", "b", "c"]
    assert time.monotonic() - t0 < 0.4  # serial would be ~0.45s


def test_in_parallel_gives_up_on_an_overrun():
    assert engine.in_parallel(lambda: time.sleep(5), timeout=0.05) == [None]


def test_answer_injects_live_quotes(providers, paths, monkeypatch):
    from stocks.chat.market import Quote

    monkeypatch.setattr(engine.market, "lookup_for",
                        lambda *a, **k: [Quote("NVDA", price=227.98,
                                               currency="USD")])
    engine.answer(prefs=dict(BASE_PREFS), message="how is NVDA?", **paths)
    _, _, _, messages = providers["free"].calls[0]  # [1] is the autotitle
    sent = messages[-1]["content"]
    assert "Live market data" in sent
    assert "last 227.98 USD" in sent
    # The stored turn keeps the user's own text, unaugmented.
    assert auth.load_chat(paths["chat_path"])[0]["content"] == "how is NVDA?"
