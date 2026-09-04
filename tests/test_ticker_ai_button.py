"""The ticker header's "Analyze with AI" button (ticker.py + chat_core.ask).

The button is a handoff: the page holds the symbol, the assistant holds the
book and the profile, and the only thing travelling between them is one
question. So what's pinned here is the handoff itself — the panel opens, the
drawer is on the thread (not the thread list a reader left it on), and the
question goes through the same turn pipeline a typed one does — plus the copy,
since a coin and a fund each get their own question and a missing placeholder
would ship a literal "{ticker}" to the model.

The page itself is not run: importing ticker.py executes it, and the header
alone costs a watchlist, a ledger replay and a logo lookup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from stocks.web import auth, chat_core
from stocks.web.i18n import translate

PAGE = Path("src/stocks/web/app_pages/ticker.py")

SCRIPT = """
import streamlit as st
from stocks.web import chat_core

if st.button("analyze", key="go"):
    chat_core.ask("Analyze Apple (AAPL)")
"""


def test_the_button_opens_the_panel_with_the_question_pending():
    at = AppTest.from_string(SCRIPT, default_timeout=10)
    at.run()
    # Left where a reader might have left it: a seed sitting behind the thread
    # list would fire whenever they next came back to the conversation.
    at.session_state["chat_drawer_view"] = "threads"
    at.button(key="go").click().run()

    assert not at.exception
    assert at.session_state["chat_panel_open"] is True
    assert at.session_state["chat_drawer_view"] == "thread"
    assert at.session_state[chat_core._seed_key("panel")] == "Analyze Apple (AAPL)"


# ------------------------------------------------------------------ pipeline

WATCHLIST = "watchlist:\n  - ticker: AAPL\n    name: Apple\n"

CONVERSATION = """
from stocks.web import chat_core
chat_core.render_conversation("panel", chat_core.active_provider(), "m", "k")
"""


class _Provider:
    id = "gemini"
    label = "Gemini"
    needs_key = False
    models = ("m",)
    default_model = "m"

    def stream(self, api_key, model, system, messages):
        yield "the analysis"

    def error_key(self, exc):
        return "chat.provider_busy"


@pytest.fixture
def paths(tmp_path):
    p = auth.UserPaths(
        root=tmp_path,
        watchlist=tmp_path / "watchlist.yaml",
        db=tmp_path / "portfolio.db",
        last_import=tmp_path / "last_import.json",
        prefs=tmp_path / "prefs.json",
        chat=tmp_path / "chat.json",
        bank=tmp_path / "bank.json",
        action=tmp_path / "daily_action.json",
    )
    p.watchlist.write_text(WATCHLIST)
    return p


@pytest.fixture
def panel(monkeypatch, paths):
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "watchlist_path", lambda: paths.watchlist)
    monkeypatch.setattr(chat_core, "active_provider", lambda: _Provider())
    monkeypatch.setattr(chat_core.engine, "attempts", lambda prefs: [])
    monkeypatch.setattr(chat_core.engine, "in_parallel",
                        lambda *fns, **kw: [None] * len(fns))
    monkeypatch.setattr(chat_core, "_try_action", lambda *a: None)
    monkeypatch.setattr(chat_core, "_maybe_autotitle", lambda *a: False)
    return AppTest.from_string(CONVERSATION, default_timeout=30)


def test_the_pending_question_is_asked_as_a_normal_turn(panel, paths):
    asked = translate("ticker.ai_prompt", "en", ticker="AAPL", name="Apple")
    panel.session_state[chat_core._seed_key("panel")] = asked
    panel.run()

    assert not panel.exception
    thread = auth.load_chat(paths.chat)
    # A stored user turn answered by the provider — nothing special-cased
    # between the header button and the reply.
    assert [t["role"] for t in thread] == ["user", "assistant"]
    assert thread[0]["content"] == asked
    assert thread[1]["content"] == "the analysis"


# ---------------------------------------------------------------------- copy


@pytest.mark.parametrize("lang", ["en", "es"])
@pytest.mark.parametrize(
    "key",
    ["ticker.ai_prompt", "ticker.ai_prompt_fund", "ticker.ai_prompt_crypto"],
)
def test_every_question_names_the_symbol_it_asks_about(lang, key):
    asked = translate(key, lang, ticker="AAPL", name="Apple")
    assert "AAPL" in asked and "Apple" in asked
    assert "{" not in asked  # an unfilled placeholder would reach the model


def test_a_coin_and_a_fund_are_not_asked_the_company_question():
    company = translate("ticker.ai_prompt", "en", ticker="X", name="X")
    fund = translate("ticker.ai_prompt_fund", "en", ticker="X", name="X")
    coin = translate("ticker.ai_prompt_crypto", "en", ticker="X", name="X")
    assert len({company, fund, coin}) == 3


def test_the_header_hands_the_question_to_the_assistant():
    """The wiring, since the page is too expensive to run here: the button
    sits in the header row and its only job is chat_core.ask."""
    src = PAGE.read_text()
    assert "_ai_analyze_button(row)" in src
    assert "chat_core.ask(tr(_ai_prompt_key(), ticker=ticker, name=label))" in src
