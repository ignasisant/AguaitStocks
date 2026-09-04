"""The assistant's opening suggestions (chat_core._render_starters).

An account that has imported nothing still has a watchlist, and the assistant
is the one surface that works fully without a ledger — live quotes,
fundamentals and the earnings calendar all need no import. An empty thread used
to open on a bare input box, which asks the reader to guess what the thing can
do. These pin what the suggestions promise: they name the account's own
tickers, they go through the real turn pipeline rather than a shortcut, and
they get out of the way once the conversation has started.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from stocks.web import auth, chat_core

SCRIPT = """
from stocks.web import chat_core
chat_core.render_conversation("panel", chat_core.active_provider(), "m", "k")
"""

WATCHLIST = """\
watchlist:
  - ticker: MSFT
    name: Microsoft
  - ticker: NVDA
    name: Nvidia
    favorite: true
  - ticker: XOM
    name: Exxon Mobil
"""


class _Provider:
    id = "gemini"
    label = "Gemini"
    needs_key = False
    models = ("m",)
    default_model = "m"

    def stream(self, api_key, model, system, messages):
        yield "the answer"

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
def app(monkeypatch, paths):
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "watchlist_path", lambda: paths.watchlist)
    monkeypatch.setattr(chat_core, "active_provider", lambda: _Provider())
    monkeypatch.setattr(chat_core.engine, "attempts", lambda prefs: [])
    monkeypatch.setattr(chat_core.engine, "in_parallel",
                        lambda *fns, **kw: [None] * len(fns))
    monkeypatch.setattr(chat_core, "_try_action", lambda *a: None)
    monkeypatch.setattr(chat_core, "_maybe_autotitle", lambda *a: False)
    return AppTest.from_string(SCRIPT, default_timeout=30)


def _starters(at):
    return [b for b in at.button if b.key.startswith("panel_chat.starter_")]


def test_an_empty_thread_opens_with_one_chip_per_suggestion(app):
    app.run()
    assert not app.exception
    assert len(_starters(app)) == len(chat_core._STARTERS)


def test_the_suggestions_name_the_accounts_own_tickers(app):
    app.run()
    labels = " ".join(b.label for b in _starters(app))
    # Favorite first, then watchlist order — the two the copy interpolates.
    assert "NVDA" in labels
    assert "MSFT" in labels


def test_a_watchlist_too_short_to_fill_the_copy_still_gets_chips(app, paths):
    paths.watchlist.write_text("watchlist: []\n")
    app.run()
    assert not app.exception
    # A suggestion is only ever a prefilled question, so a generic pair beats
    # hiding the row on an account that has not added anything yet.
    assert len(_starters(app)) == len(chat_core._STARTERS)


def test_clicking_a_suggestion_asks_it_through_the_real_turn_pipeline(app, paths):
    app.run()
    chip = _starters(app)[0]
    asked = chip.label
    chip.click().run()

    assert not app.exception
    thread = auth.load_chat(paths.chat)
    # Stored as a normal user turn, and answered by the provider — not
    # special-cased anywhere between the click and the reply.
    assert [t["role"] for t in thread] == ["user", "assistant"]
    assert thread[0]["content"] == asked
    assert thread[1]["content"] == "the answer"


def test_the_suggestions_are_gone_once_the_thread_has_a_turn(app):
    app.run()
    _starters(app)[0].click().run()
    # A fresh run, not the click's own: AppTest keeps the elements a container
    # held before an in-script st.rerun, and the chip's handler reruns.
    app.run()
    assert not _starters(app)


def test_typed_text_wins_over_a_pending_suggestion(app, paths):
    """Both can land on one run — the seed is popped, so it never re-fires."""
    app.run()
    app.session_state[chat_core._seed_key("panel")] = "the suggestion"
    app.chat_input[0].set_value("my own question").run()

    assert not app.exception
    thread = auth.load_chat(paths.chat)
    assert thread[0]["content"] == "my own question"
    assert chat_core._seed_key("panel") not in app.session_state
