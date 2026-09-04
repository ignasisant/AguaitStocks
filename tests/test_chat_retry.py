"""A failed answer keeps its question, and Retry replays it.

The panel used to pop the user's turn, print the provider error and stop the
script: the question vanished with the failure and the Regenerate button never
rendered, so a saturated provider cost the reader their message. These tests
run the real render through AppTest and pin the recovery.
"""

from __future__ import annotations

import time

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


@pytest.fixture
def free_pot(monkeypatch, tmp_path):
    """The free chain's two counters, isolated from the real data dir."""
    monkeypatch.setattr(chat_core.engine, "GLOBAL_FREE_FILE",
                        tmp_path / "free_llm_global.json")
    monkeypatch.setattr(chat_core.engine, "_global_free_loaded", True)
    monkeypatch.setattr(chat_core.engine, "_global_free", {"day": "", "used": 0})
    return chat_core.engine


def _units_spent(paths):
    key = f"free_msgs::{time.strftime('%Y-%m-%d')}"
    return int(auth.load_prefs(paths.prefs).get(key, 0) or 0)


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
    # The failure is a turn in the thread, not a full-width error block: the
    # question is still above it, and the notice names the provider that died.
    assert not app.error
    assert app.chat_message[0].markdown[0].value == "¿cómo va mi cartera?"
    # Any bubble, not the last: AppTest keeps the elements of the run that
    # failed alongside the ones the rerun drew.
    assert any("Gemini" in md.value
               for msg in app.chat_message for md in msg.markdown)
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
    assert not fresh.error
    assert "Gemini" in fresh.chat_message[-1].markdown[0].value
    assert auth.load_chat(paths.chat)[-1]["error"] == [
        "chat.provider_busy", "Gemini"]


# ------------------------------------------------- what a failure costs
# The free unit is spent before the provider is called — the last moment the
# turn can still be refused — so a turn that dies has taken a message the
# reader never got, and Retry would take a second one for the same question.


def test_a_dead_free_turn_gives_the_unit_back(app, paths, provider, free_pot):
    provider.id = "free"
    app.run()

    app.chat_input[0].set_value("hola").run()

    assert not app.exception
    assert _units_spent(paths) == 0
    assert free_pot._global_free["used"] == 0  # the shared pot too


def test_retry_after_a_refund_costs_one_unit_not_two(app, paths, provider,
                                                     free_pot):
    provider.id = "free"
    app.run()
    app.chat_input[0].set_value("hola").run()
    provider.broken = False

    _labelled(app, "Retry")[0].click().run()

    assert not app.exception
    assert _thread(paths)[-1] == {"role": "assistant", "content": "the answer"}
    assert _units_spent(paths) == 1  # only the answered attempt was charged


def test_an_answered_free_turn_keeps_its_unit(app, paths, provider, free_pot):
    provider.id = "free"
    provider.broken = False
    app.run()

    app.chat_input[0].set_value("hola").run()

    assert not app.exception
    assert _units_spent(paths) == 1


# --------------------------------------------- a reload while the answer runs
# Generating is the long part of a turn (routing, searches, then the stream),
# and it is the window a reader is most likely to reload in — the answer is
# visibly not finished. The question is written before that window opens, so
# what a reload finds is the same trailing unanswered turn Retry leaves
# behind, which the panel already knows how to pick up.


def test_the_question_is_on_disk_before_the_answer_starts(app, paths, provider,
                                                          monkeypatch):
    """Read from inside the stream: this is what a reload mid-answer sees.

    Held only in session state until the answer landed, a refresh during those
    seconds took the question down with the session and the thread came back
    with no trace that anything had been asked.
    """
    mid: dict = {}

    def stream(api_key, model, system, messages):
        mid["thread"] = auth.load_chat(paths.chat)
        yield "the answer"

    provider.broken = False
    monkeypatch.setattr(provider, "stream", stream)
    app.run()

    app.chat_input[0].set_value("hola").run()

    assert not app.exception
    assert [t["role"] for t in mid["thread"]] == ["user"]
    assert mid["thread"][0]["content"] == "hola"


def test_a_reload_mid_answer_finishes_the_answer(app, paths, provider):
    """The stored question generates on the next run, no retyping and no
    button: an interrupted turn resumes by being asked again, since the dead
    session's stream cannot be picked back up."""
    provider.broken = False
    auth.save_chat([{"role": "user", "content": "hola"}], paths.chat)

    app.run()  # a fresh session is what a reload is

    assert not app.exception
    assert _thread(paths) == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "the answer"},
    ]


def test_an_unclassified_crash_marks_the_turn_instead_of_wedging_it(
        app, paths, provider, monkeypatch):
    """A failure no provider recognises still raises — but the question is on
    disk now, so an unmarked turn would re-run the same crash on every load of
    the thread. Marked, the reader gets Retry instead of a dead page."""
    monkeypatch.setattr(provider, "error_key", lambda exc: None)
    app.run()

    app.chat_input[0].set_value("hola").run()

    assert app.exception  # the crash page is still the right answer here
    assert _thread(paths) == [{
        "role": "user", "content": "hola",
        "error": ["chat.api_error", "Gemini"],
    }]
