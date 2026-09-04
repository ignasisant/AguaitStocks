"""The drawer shows one view at a time, and its header says what is answering.

The panel used to stack: a settings expander above the conversation, a thread
popover over it, and a destructive "Clear chat" inside the scroll region next
to Regenerate. These tests pin the replacement — two header rows plus one of
three views — through the real render.
"""

from __future__ import annotations

import json
import time

import pytest
from streamlit.testing.v1 import AppTest

from stocks.web import auth, chat_core

# The panel's own body, driven directly: the drawer lives in a fragment behind
# the launcher, which AppTest cannot click.
SCRIPT = """
from stocks.web import chat_core
chat_core._panel_body()
"""


class _Provider:
    id = "free"
    label = "Aguait AI"
    needs_key = False
    models = ("m",)
    default_model = "m"
    domain = ""

    def stream(self, api_key, model, system, messages):
        yield "the answer"

    def error_key(self, exc):
        return "chat.api_error"


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
    monkeypatch.setattr(chat_core, "_offered_providers", lambda: [provider])
    monkeypatch.setattr(chat_core.llm, "PROVIDERS", {provider.id: provider})
    monkeypatch.setattr(chat_core.llm, "default_provider_id", lambda: provider.id)
    return AppTest.from_string(SCRIPT, default_timeout=30)


def _labelled(at, label):
    return [b for b in at.button if b.label == label]


def _mode_control(at):
    """The skill-mode segmented control (a button group, keyed like the pref)."""
    return next(b for b in at.get("button_group") if b.key == "panel_skills_mode")


def _seed(paths, title="Semis concentration", turns=2):
    """A named thread with `turns` messages already on it."""
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}][:turns]
    paths.chat.write_text(json.dumps({
        "version": 2, "active": "c1",
        "conversations": [{
            "id": "c1", "title": title, "title_auto": False,
            "created": "2026-09-03T08:00:00+00:00",
            "updated": "2026-09-03T08:54:00+00:00",
            "messages": messages,
        }],
    }))


def test_the_header_names_the_thread_and_the_model_not_the_word_assistant(
        app, paths):
    _seed(app_paths := paths)
    app.run()

    assert not app.exception
    labels = [b.label for b in app.button]
    assert "Semis concentration" in labels  # the open thread, not "Assistant"
    assert "Assistant" not in labels
    assert "Close" not in labels  # the text button is an icon now
    # The status strip answers "what is answering me" without opening anything.
    assert app.button(key="panel_status_model").label == "Aguait AI"
    quota = [h.body for h in app.get("html") if "ts-chat-quota" in h.body]
    assert quota and "30" in quota[0]  # the free cap, shown at rest
    assert app_paths.chat.exists()


def test_clear_chat_no_longer_rides_inside_the_message_list(app, paths):
    _seed(paths)
    app.run()

    assert not app.exception
    assert _labelled(app, "Regenerate")  # the tail keeps the safe action
    assert not _labelled(app, "Clear chat")  # the destructive one moved out


def test_the_thread_title_opens_the_list_instead_of_a_popover(app, paths):
    _seed(paths)
    app.run()

    app.button(key="panel_open_threads").click().run()

    assert not app.exception
    assert app.session_state["chat_drawer_view"] == "threads"
    # A view swap replaces the conversation: no composer while browsing.
    assert not app.chat_input
    assert app.text_input(key="panel_threads_q")
    assert _labelled(app, "New conversation")
    # The list carries the date group and the message count, which is what a
    # 380px popover had no room for.
    assert [h.body for h in app.get("html") if "ts-chat-group" in h.body]
    assert any("2 messages" in b.label for b in app.button)


def test_deleting_a_thread_takes_two_presses(app, paths):
    _seed(paths)
    app.run()
    app.button(key="panel_open_threads").click().run()

    app.button(key="panel_del_c1").click().run()  # the row's ⋮ menu entry

    assert not app.exception
    assert app.session_state["panel_deleting"] == "c1"
    assert len(auth.list_conversations(paths.chat)) == 1  # nothing gone yet
    body = " ".join(m.value for m in app.markdown)
    assert "Semis concentration" in body and "2 messages" in body

    app.button(key="panel_delyes_c1").click().run()

    assert not app.exception
    # The store replaces the last thread with a fresh empty one rather than
    # leaving the account with none.
    remaining = auth.list_conversations(paths.chat)
    assert [c["title"] for c in remaining] == [""]


def test_settings_replaces_the_thread_and_owns_the_destructive_action(
        app, paths):
    _seed(paths)
    app.run()

    app.button(key="panel_gear").click().run()

    assert not app.exception
    assert app.session_state["chat_drawer_view"] == "settings"
    assert not app.chat_input
    assert app.button(key="panel_prov_free")  # provider tiles, not a segment
    assert _labelled(app, "Delete this conversation")
    assert _labelled(app, "Back to thread")


def test_the_rail_toggles_internet_access_beside_the_input(app, paths,
                                                           monkeypatch):
    monkeypatch.setattr(chat_core.chat_web, "available", lambda: True)
    _seed(paths)
    app.run()

    assert app.button(key="panel_rail_web").label == "Internet"
    app.button(key="panel_rail_web").click().run()

    assert not app.exception
    assert auth.load_prefs(paths.prefs)["chat_web"] is False


def test_a_width_preset_stores_a_css_length_the_handle_can_restore(app, paths):
    _seed(paths)
    app.run()

    app.button(key="panel_w_wide").click().run()

    assert not app.exception
    assert app.session_state["chat_width"] == "wide"
    # The preset writes the same --chat-w the drag handle drives, as a CSS
    # length: a bare number would come back from localStorage as 720px of
    # nothing.
    emitted = [h.body for h in app.get("html") if "chatPanelWidth" in h.body]
    assert emitted and "720px" in emitted[0]


def test_the_empty_thread_teaches_what_the_assistant_can_do(app, paths):
    """A first visit used to be a blank scroll region and a placeholder."""
    app.run()

    assert not app.exception
    # The intro and the capability list are one HTML write: the artboard's own
    # glyphs and rows, not st.markdown with icon directives.
    body = " ".join(h.body for h in app.get("html"))
    assert "Analyse what you hold" in body
    assert "Import statements" in body  # the drop target, said in words
    # And drawn: the input has always accepted a statement and never showed it.
    assert [h.body for h in app.get("html") if "ts-chat-drop" in h.body]
    # Three suggestions, and with no ledger they are the ones a fresh account
    # can actually answer — the watchlist set, not "summarise my portfolio".
    starters = [b for b in app.button if b.key.startswith("panel_chat.starter_")]
    assert len(starters) == 3
    assert not any("portfolio" in b.label.lower() for b in starters)
    assert [h.body for h in app.get("html") if "START WITH" in h.body]


def test_a_first_run_offers_the_free_path_before_the_key_form(
        app, monkeypatch, provider):
    """The keyless chain existed; the setup screen never offered it."""
    byok = _Provider()
    byok.id, byok.label, byok.needs_key = "anthropic", "Claude", True
    byok.key_placeholder, byok.console_url = "sk-...", "https://example.test"
    monkeypatch.setattr(chat_core, "active_provider", lambda: byok)
    monkeypatch.setattr(chat_core, "_offered_providers", lambda: [provider, byok])
    monkeypatch.setattr(chat_core.llm, "PROVIDERS",
                        {provider.id: provider, byok.id: byok})
    monkeypatch.setattr(chat_core.llm, "default_provider_id", lambda: byok.id)
    monkeypatch.setattr(chat_core, "active_key", lambda p: "")
    app.run()

    assert not app.exception
    free = app.button(key="panel_use_free")
    assert free.label == "Use the free assistant"
    # The key form is still there, under the divider — not instead of it.
    assert [h.body for h in app.get("html") if "ts-chat-or" in h.body]
    assert app.text_input(key="panel_key_anthropic")
    # And it commits explicitly: it used to save on any rerun that found text
    # in the box, leaving the screen with no visible way to finish.
    assert "Save and start" in [b.label for b in app.button]


def test_an_answer_carries_two_chrome_rows_not_five(app, paths):
    """Lens and cost above, sources and clock below — and nothing else."""
    paths.chat.write_text(json.dumps({
        "version": 2, "active": "c1",
        "conversations": [{
            "id": "c1", "title": "AVGO", "title_auto": False,
            "created": "2026-09-03T08:00:00+00:00",
            "updated": "2026-09-03T08:54:00+00:00",
            "messages": [
                {"role": "user", "content": "avgo?", "ts": 1_788_000_000},
                {"role": "assistant", "content": "It beat and guided low.",
                 "ts": 1_788_000_018, "took": 18.0,
                 "skills": ["earnings-review"],
                 "steps": [{"tool": "search_web", "arg": "AVGO", "out": "6"},
                           {"tool": "get_quotes", "arg": "AVGO"}],
                 "web": [{"url": "https://reuters.com/a"},
                         {"url": "https://sec.gov/b"}]},
            ],
        }],
    }))
    app.run()

    assert not app.exception
    html = " ".join(h.body for h in app.get("html"))
    assert "ts-chat-lens" in html  # why the answer leans the way it does
    assert "ts-chat-clock" in html
    # The trace and the domains are counted, not listed: two popovers, and no
    # six-hostname caption under the prose.
    triggers = [pop.proto.popover.label for pop in app.get("popover")]
    assert "2 steps · 18.0s" in triggers
    assert "2 sources" in triggers
    assert not any("reuters.com" in c.value for c in app.caption)


# ------------------------------------------------------- the free counter
# The strip sits above the turn that spends the unit it states, and the two
# caps behind that unit fail differently.


@pytest.fixture
def live_turn(monkeypatch, tmp_path):
    """A turn that reaches the provider, with both free counters isolated."""
    monkeypatch.setattr(chat_core.engine, "GLOBAL_FREE_FILE",
                        tmp_path / "free_llm_global.json")
    monkeypatch.setattr(chat_core.engine, "_global_free_loaded", True)
    monkeypatch.setattr(chat_core.engine, "_global_free", {"day": "", "used": 0})
    # No chain behind the chosen provider and none of the per-turn lookups:
    # these tests are about the counter, not about what an answer is made of.
    monkeypatch.setattr(chat_core.engine, "attempts", lambda prefs: [])
    monkeypatch.setattr(chat_core.engine, "in_parallel",
                        lambda *fns, **kw: [None] * len(fns))
    monkeypatch.setattr(chat_core, "_try_action", lambda *a: None)
    monkeypatch.setattr(chat_core, "_maybe_autotitle", lambda *a: False)
    return chat_core.engine


def test_the_counter_states_what_the_turn_just_spent(app, paths, live_turn):
    """It used to print the count from before the message the reader had just
    sent, and keep printing it until some later rerun repainted the panel."""
    _seed(paths)
    app.run()

    app.chat_input[0].set_value("¿y ahora?").run()

    assert not app.exception
    quota = [h.body for h in app.get("html")
             if "ts-chat-quota" in h.body and "/30" in h.body]
    assert quota and "29/30" in quota[-1]


def test_a_spent_shared_pot_is_not_reported_as_your_own_cap(app, paths,
                                                            live_turn):
    """The two caps fail differently: this account's allowance resets
    tomorrow, the shared pot is everyone's and may be back within the hour.
    Naming the account cap for a process-wide refusal tells the reader they
    spent 30 messages they never sent."""
    live_turn._global_free.update(day=time.strftime("%Y-%m-%d"),
                                  used=live_turn.free_global_daily_cap())
    _seed(paths)
    app.run()

    app.chat_input[0].set_value("¿y ahora?").run()

    assert not app.exception
    body = " ".join(m.value for m in app.markdown)
    assert "shared limit" in body
    assert "30 free messages" not in body  # not this reader's own allowance
    # And the counter agrees with the refusal instead of contradicting it.
    quota = [h.body for h in app.get("html")
             if "ts-chat-quota" in h.body and "/30" in h.body]
    assert quota and "0/30" in quota[-1]


def test_the_skill_chip_names_the_mode_the_picker_is_showing(app, paths):
    """The chip that opens the picker used to lag a rerun behind it.

    It renders above the popover body, so a label read off prefs showed the
    mode the picker had not written yet: press Auto and the control went
    highlighted over a chip that still said "Off".
    """
    _seed(paths)
    paths.prefs.write_text(json.dumps({"chat_skills_mode": "off"}))
    app.run()
    def chips():
        return [pop.proto.popover.label for pop in app.get("popover")]

    assert "Skills: Off" in chips()

    _mode_control(app).set_value("auto").run()

    assert not app.exception
    assert "Skills: Auto" in chips()  # the same rerun, not the next one
    assert json.loads(paths.prefs.read_text())["chat_skills_mode"] == "auto"


def test_the_mode_control_keeps_a_selection_when_pressed_again(app, paths):
    """A segmented control clears on a second press of the live option.

    Left alone that leaves the row blank while the chip still names a mode, so
    the picker seeds the widget and the saved mode snaps back.
    """
    _seed(paths)
    paths.prefs.write_text(json.dumps({"chat_skills_mode": "manual"}))
    app.run()
    app.session_state["panel_skills_mode"] = None  # what the deselect posts

    app.run()

    assert not app.exception
    assert _mode_control(app).value == "manual"
    assert "Skills: Manual" in [p.proto.popover.label for p in app.get("popover")]


def test_a_legacy_unitless_stored_width_gets_its_unit_back():
    """The panel lost its width entirely for anyone who had ever dragged it.

    Every build before the width presets stored `chatPanelWidth` as a bare
    integer. Handing that back verbatim makes `min(380, 100vw)` invalid, which
    drops the width declaration — and a fixed panel with no width stretches
    across the whole page.
    """
    js = chat_core._RESIZE_JS
    assert "raw + 'px'" in js  # the legacy value is normalized on restore
    assert "px + 'px'" in js  # and a drag now persists a length, not a number
    assert "/^[0-9]+(px|vw)$/" in js  # anything else is ignored, not applied


def test_a_book_changes_the_opening_line_and_the_suggestions(app, paths,
                                                             monkeypatch):
    """With a ledger the opening screen talks about the ledger.

    The artboard's copy names positions and offers portfolio questions, which
    are three questions a fresh account cannot answer — so the count decides
    which set is drawn.
    """
    monkeypatch.setattr(chat_core, "_position_count", lambda: 31)
    app.run()

    assert not app.exception
    assert "31 positions" in " ".join(h.body for h in app.get("html"))
    labels = [b.label for b in app.button
              if b.key.startswith("panel_chat.starter_")]
    assert labels == ["Summarise my portfolio in five lines",
                      "Where am I too concentrated?",
                      "What reports earnings this week"]


# The launcher/close pair is driven through the two helpers render_side_panel
# calls, not through the panel body: the launcher lives in app.py's shell,
# which AppTest cannot stand up here.
OPEN_SCRIPT = """
import streamlit as st
from stocks.web import chat_core

is_open = chat_core._panel_is_open()
chat_core._remember_open(is_open)
st.write("open" if is_open else "closed")
"""


@pytest.fixture
def shell(monkeypatch, paths):
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    return AppTest.from_string(OPEN_SCRIPT, default_timeout=30)


def test_a_reload_reopens_a_drawer_that_was_left_open(shell, paths):
    """A reload is a new session, so the flag has to come off disk.

    Held in session state alone the panel collapsed back to the launcher icon
    on every refresh, mid-conversation and with nothing saying the thread was
    still there.
    """
    paths.prefs.write_text(json.dumps({"chat_panel_open": True}))

    shell.run()

    assert not shell.exception
    assert shell.markdown[0].value == "open"


def test_a_fresh_account_starts_behind_the_launcher(shell, paths):
    """No stored flag means closed — the drawer covers the page, so it may
    never be what an account sees first."""
    shell.run()

    assert not shell.exception
    assert shell.markdown[0].value == "closed"
    assert auth.load_prefs(paths.prefs)["chat_panel_open"] is False


def test_closing_the_drawer_survives_the_next_reload(shell, paths):
    """The close icon writes through, or the stored open flag would reopen the
    panel the reader just dismissed."""
    paths.prefs.write_text(json.dumps({"chat_panel_open": True}))
    shell.run()

    shell.session_state["chat_panel_open"] = False  # the close icon's write
    shell.run()

    assert not shell.exception
    assert shell.markdown[0].value == "closed"
    assert auth.load_prefs(paths.prefs)["chat_panel_open"] is False


def test_a_settled_drawer_does_not_rewrite_prefs_every_run(shell, paths,
                                                           monkeypatch):
    """save_prefs mirrors to the bucket, and this runs on every script run —
    so an unchanged flag must not touch the file at all."""
    paths.prefs.write_text(json.dumps({"chat_panel_open": True}))
    writes = []
    monkeypatch.setattr(auth, "save_prefs",
                        lambda prefs, path=None: writes.append(prefs))

    shell.run()
    shell.run()

    assert not shell.exception
    assert writes == []


def test_an_unnamed_thread_reads_as_a_placeholder_in_the_header(app, paths):
    """A thread with no title yet is greyed; a named one is not."""
    app.run()
    assert app.button(key="panel_open_threads").label.startswith(":gray[")

    _seed(paths, title="Semis concentration")
    app.run()
    assert app.button(key="panel_open_threads").label == "Semis concentration"
