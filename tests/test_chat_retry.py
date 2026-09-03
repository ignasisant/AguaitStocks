"""A failed answer keeps its question, and Retry replays it.

The panel used to pop the user's turn, print the provider error and stop the
script: the question vanished with the failure and the Regenerate button never
rendered, so a saturated provider cost the reader their message. These tests
run the real render through AppTest and pin the recovery.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from stocks.web import auth, chat_core

# The panel's own render, driven directly — the chat lives in a fragment
# behind the launcher, which AppTest cannot click.
SCRIPT = """
from stocks.web import chat_core
chat_core.render_conversation("panel", chat_core.active_provider(), "m", "k")
"""


class _Provider:
    """A provider that fails until told otherwise."""

    id = "gemini"
    label = "Gemini"
    needs_key = False
    models = ("m",)
    default_model = "m"
    broken = True

    def stream(self, api_key, model, system, messages):
        if self.broken:
            raise RuntimeError("503 overloaded")
        yield "the answer"

    def error_key(self, exc):
        return "chat.provider_busy"


@pytest.fixture
def paths(tmp_path):
    return auth.UserPaths(
        root=tmp_path,
        watchlist=tmp_path / "watchlist.yaml",
        db=tmp_path / "portfolio.db",
        last_import=tmp_path / "last_import.json",
        prefs=tmp_path / "prefs.json",
        chat=tmp_path / "chat.json",
        bank=tmp_path / "bank.json",
        action=tmp_path / "daily_action.json",
    )


@pytest.fixture
def provider():
    return _Provider()


@pytest.fixture
def app(monkeypatch, paths, provider):
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(chat_core, "active_provider", lambda: provider)
    # No chain behind the chosen provider, and none of the per-turn lookups:
    # this is about what the panel does once the answer dies.
    monkeypatch.setattr(chat_core.engine, "attempts", lambda prefs: [])
    # One None per call handed in, whatever the panel is running concurrently
    # this release (routing, the gather, the fixed pre-flight) — the panel
    # already treats a None as "that lookup found nothing".
    monkeypatch.setattr(chat_core.engine, "in_parallel",
                        lambda *fns, **kw: [None] * len(fns))
    monkeypatch.setattr(chat_core, "_try_action", lambda *a: None)
    monkeypatch.setattr(chat_core, "_maybe_autotitle", lambda *a: False)
    return AppTest.from_string(SCRIPT, default_timeout=30)


def _labelled(at, label):
    return [b for b in at.button if b.label == label]


def _thread(paths):
    """The stored turns without their instrumentation.

    Every turn now carries a clock stamp ("ts"), an answer also what it cost
    ("took") and what it fetched ("steps"); those belong to the timing tests,
    not to these, which are about which turns survive a failure."""
    noise = ("ts", "took", "steps")
    return [{k: v for k, v in turn.items() if k not in noise}
            for turn in auth.load_chat(paths.chat)]


# AppTest keeps the elements a container held before an in-script st.rerun
# (the browser drops them on script-finished), so what disappeared is asserted
# on the stored thread rather than on the element tree.
def test_a_dead_provider_leaves_the_question_and_a_retry(app, paths):
    app.run()
    app.chat_input[0].set_value("¿cómo va mi cartera?").run()

    assert not app.exception
    assert "Gemini" in app.error[0].value  # names the provider that failed
    assert app.chat_message[0].markdown[0].value == "¿cómo va mi cartera?"
    assert _labelled(app, "Retry")
    # The question survives, with the failure pinned to it.
    assert _thread(paths) == [{
        "role": "user", "content": "¿cómo va mi cartera?",
        "error": ["chat.provider_busy", "Gemini"],
    }]


def test_retry_replays_the_same_turn_without_retyping_it(app, paths, provider):
    app.run()
    app.chat_input[0].set_value("hola").run()
    provider.broken = False  # the provider comes back

    _labelled(app, "Retry")[0].click().run()

    assert not app.exception
    assert _thread(paths) == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "the answer"},
    ]
    assert _labelled(app, "Regenerate")  # back to the normal tail


def test_the_thread_carries_a_clock_and_the_answer_its_cost(app, paths,
                                                            provider):
    """Both stamps are written by the live path, not only by the redraw."""
    provider.broken = False
    app.run()
    app.chat_input[0].set_value("hola").run()

    user, answer = auth.load_chat(paths.chat)
    assert user["ts"] and answer["ts"] >= user["ts"]
    assert answer["took"] >= 0
    assert "took" not in user  # only an answer has a cost


def test_discarding_a_failed_turn_drops_it(app, paths):
    app.run()
    app.chat_input[0].set_value("hola").run()

    _labelled(app, "Discard question")[0].click().run()

    assert not app.exception
    assert _thread(paths) == []


def test_a_new_question_supersedes_the_failed_one(app, paths, provider):
    """Two user turns in a row would reach the provider as one malformed
    exchange, so asking something else drops the unanswered turn."""
    app.run()
    app.chat_input[0].set_value("primera").run()
    provider.broken = False

    app.chat_input[0].set_value("segunda").run()

    assert not app.exception
    assert _thread(paths) == [
        {"role": "user", "content": "segunda"},
        {"role": "assistant", "content": "the answer"},
    ]


def test_a_failed_turn_reloads_as_a_retry_not_as_a_silent_replay(app, paths,
                                                                 provider):
    """A fresh session (a reload) reads the mark off disk: the question is
    there with its error, and nothing is sent until Retry is pressed."""
    app.run()
    app.chat_input[0].set_value("hola").run()
    provider.broken = False

    fresh = AppTest.from_string(SCRIPT, default_timeout=30)
    fresh.run()

    assert not fresh.exception
    assert "Gemini" in fresh.error[0].value
    assert auth.load_chat(paths.chat)[-1]["error"] == [
        "chat.provider_busy", "Gemini"]
