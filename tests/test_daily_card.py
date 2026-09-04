"""The daily action card as the dashboard renders it (stocks.web.daily_ui).

Runs the real render through AppTest: the card is the first AI surface a user
meets, it costs a model call, and the rules that keep it cheap and safe —
generate once a day, reuse the stored copy, escape the model's text, never
take the page down — only exist end to end.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from stocks.chat import daily, engine
from stocks.web import auth, daily_ui

SCRIPT = """
import streamlit as st

from stocks.web import daily_ui

slot = daily_ui.reserve()
daily_ui.render(
    slot, tbl=None, hist=None, currency="EUR", day_change=(120.0, 0.012),
    holdings=st.session_state.get("holdings", []),
    closes=st.session_state.get("closes", {}),
)
"""

# Every figure here is one the page's facts actually carry (day_change below):
# the card's audit rejects a reply that quotes anything else, which is the
# point of test_a_card_quoting_a_figure_the_book_lacks_never_reaches_the_page.
REPLY = {
    "headline": "Nvidia carries the day",
    "bullets": ["Book +1.2% today — check NVDA's weight", "ASML reports in 4 days"],
    "focus": ["NVDA"],
}


@dataclass
class FakeProvider:
    id: str = "free"
    reply: str = json.dumps(REPLY)
    calls: list = field(default_factory=list)  # (system, facts) per attempt

    def available(self) -> bool:
        return True

    def complete(self, api_key, model, system, messages) -> str:
        self.calls.append((system, json.loads(messages[-1]["content"])))
        return self.reply


@pytest.fixture
def stored() -> dict:
    return {}


@pytest.fixture
def free(monkeypatch, tmp_path):
    from stocks.web import llm

    monkeypatch.setattr(engine, "GLOBAL_FREE_FILE", tmp_path / "pot.json")
    monkeypatch.setattr(engine, "_global_free", {"day": "", "used": 0})
    monkeypatch.setattr(engine, "_global_free_loaded", True)
    provider = FakeProvider()
    monkeypatch.setattr(llm, "PROVIDERS", {"free": provider})
    return provider


@pytest.fixture
def page(monkeypatch, free, stored):
    """The card with a signed-in account whose files live in memory."""
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)
    monkeypatch.setattr(auth, "load_prefs", lambda *a, **k: {})
    monkeypatch.setattr(auth, "save_prefs", lambda *a, **k: None)
    monkeypatch.setattr(auth, "load_profile", lambda *a, **k: {})
    monkeypatch.setattr(auth, "load_action", lambda *a, **k: dict(stored))
    monkeypatch.setattr(
        auth, "save_action", lambda card, *a, **k: stored.update(card)
    )
    # The chips would otherwise reach for logos and company names.
    monkeypatch.setattr(daily_ui, "ticker_cell", lambda t, **kw: f"<b>{t}</b>")
    at = AppTest.from_string(SCRIPT, default_timeout=30)
    at.session_state["active_lang"] = "en"
    return at


def _card(at) -> str:
    return "".join(el.body for el in at.get("html"))


def test_a_card_quoting_a_figure_the_book_lacks_never_reaches_the_page(
    page, free, stored
):
    """A provider that invents a percentage is a miss, not a card: the reader
    gets the computed figures instead, and nothing false is stored."""
    free.reply = json.dumps({
        "headline": "Nvidia carries the day",
        "bullets": ["NVDA +37.4% today", "ASML reports in 4 days"],
        "focus": ["NVDA"],
    })
    page.run()
    assert not page.exception
    body = _card(page)
    assert "37.4" not in body and "Nvidia carries the day" not in body
    assert "+1.20%" in body  # the computed card, straight from the book
    assert not stored


def test_the_card_is_generated_once_and_then_read_from_the_store(page, free, stored):
    page.run()
    assert not page.exception
    body = _card(page)
    assert "Nvidia carries the day" in body
    assert "Book +1.2% today" in body
    assert daily.action_day(datetime.now()).isoformat() == stored["day"]
    assert len(free.calls) == 1

    # A rerun (any widget, any navigation) must not buy a second briefing.
    page.run()
    assert len(free.calls) == 1
    assert "Nvidia carries the day" in _card(page)


def test_regenerating_redraws_the_card_in_place(page, free):
    """The refresh button reruns the card's fragment, not the page: the old
    card must never be left cleared while a provider is called."""
    page.run()
    assert len(free.calls) == 1
    free.reply = json.dumps(
        {"headline": "Second look", "bullets": ["one", "two"], "focus": []}
    )
    page.button(key="daily_action_refresh").click().run()
    assert not page.exception
    assert len(free.calls) == 2
    body = _card(page)
    assert "Second look" in body and "Nvidia carries the day" not in body
    # Still interactive afterwards — the buttons live inside the fragment.
    assert {b.key for b in page.button} == {"daily_action_ask", "daily_action_refresh"}


def test_the_prompt_carries_the_figure_the_page_is_showing(page, free):
    """The card sits under the KPI tiles: quoting a different "today" from the
    one beside it would read as a bug in the numbers, not in the prose."""
    page.run()
    system, facts = free.calls[0]
    assert facts["day"] == {"amount": 120.0, "pct": 1.2}
    assert facts["currency"] == "EUR"
    assert "TopStocks" in system


def test_model_text_is_escaped(page, free, stored):
    free.reply = json.dumps(
        {"headline": "<img src=x onerror=alert(1)>", "bullets": ["a & b", "c"]}
    )
    page.run()
    body = _card(page)
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body and "a &amp; b" in body


def test_a_dead_provider_still_fills_the_card(page, free, monkeypatch):
    monkeypatch.setattr(
        FakeProvider, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    page.run()
    assert not page.exception
    body = _card(page)
    assert "Portfolio +1.20% today" in body  # the computed fallback, verbatim
    assert page.caption[0].value.startswith("The assistant is unavailable")


def test_the_computed_card_is_not_stored(page, free, stored, monkeypatch):
    """Only a model briefing is worth a bucket write — and storing the
    fallback would pin it for the rest of the day."""
    monkeypatch.setattr(
        FakeProvider, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    page.run()
    assert stored == {}


def test_a_stale_stored_card_is_replaced(page, free, stored):
    stored.update(
        daily.DailyAction(
            day="2020-01-01", headline="Ancient", bullets=["old"], lang="en"
        ).to_dict()
    )
    page.run()
    assert "Ancient" not in _card(page)
    assert len(free.calls) == 1


def test_guests_get_no_card(page, free, monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda: False)
    page.run()
    assert not page.exception
    assert not _card(page)
    assert not free.calls


def test_a_broken_store_never_takes_the_dashboard_down(page, free, monkeypatch):
    logged = []
    monkeypatch.setattr(
        auth, "load_action", lambda *a, **k: (_ for _ in ()).throw(OSError("bucket"))
    )
    monkeypatch.setattr(daily_ui.obs, "warn", lambda name, **kw: logged.append(name))
    page.run()
    assert not page.exception
    assert not _card(page)
    # ...and it says so in the log: a card that silently stops appearing is a
    # bug nobody can see.
    assert logged == ["daily_action.render_failed"]


def test_the_users_own_alert_reaches_the_prompt_and_the_card(page, free):
    """The wiring that makes this an action card: a level the user set on a
    watchlist entry, compared against the close the page already fetched."""
    from stocks.config import Alert, Holding

    free.reply = json.dumps({
        "headline": "ASML hit your exit",
        "bullets": ["Review ASML: your 300 exit fired at 280", "Nothing else"],
        "focus": ["ASML"],
    })
    page.session_state["holdings"] = [
        Holding("ASML", alerts=[Alert("below", price=300.0)])
    ]
    page.session_state["closes"] = {"ASML": [305.0, 280.0]}
    page.run()
    _system, facts = free.calls[0]
    assert facts["actions"][0] == {
        "kind": "alert_hit", "ticker": "ASML", "rule": "below", "level": 300.0,
        "price": 280.0, "held": False, "gap_pct": 6.67,
    }
    assert "ASML hit your exit" in _card(page)


def test_without_a_model_the_card_still_states_the_action(page, free, monkeypatch):
    from stocks.config import Alert, Holding

    monkeypatch.setattr(
        FakeProvider, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    page.session_state["holdings"] = [
        Holding("ASML", alerts=[Alert("below", price=300.0)])
    ]
    page.session_state["closes"] = {"ASML": [305.0, 280.0]}
    page.run()
    body = _card(page)
    assert "your below alert at 300.00 fired" in body
    assert "Portfolio +1.20% today" not in body  # an action outranks the figure

def test_on_a_phone_the_actions_stack_with_short_labels(page, free, monkeypatch):
    """A 390px card cannot hold two side-by-side buttons whose labels wrap —
    the pair reads as one smudge and neither is a comfortable target."""
    monkeypatch.setattr(daily_ui, "is_mobile", lambda: True)
    page.run()
    assert [b.label for b in page.button] == ["Ask", "Regenerate"]
    assert page.caption[0].value.startswith("AI analysis from your own figures")
    # Still the same card, and still interactive.
    assert "Nvidia carries the day" in _card(page)
    page.button(key="daily_action_refresh").click().run()
    assert not page.exception and len(free.calls) == 2


def test_the_desktop_card_keeps_the_long_labels(page, free):
    page.run()
    assert [b.label for b in page.button] == ["Ask the assistant", "Regenerate"]
    assert page.caption[0].value.startswith("Written by the assistant")


def test_the_sheet_carries_the_phone_block_last(page, free):
    """The mobile rules override the base ones, so they must come last in the
    stylesheet (same discipline as app.py's trailing DS mobile block)."""
    page.run()
    sheet = next(e.body for e in page.get("html") if "ag-daily-headline" in e.body
                 and "style" in e.body)
    assert "@media (max-width: 640px)" in sheet
    assert sheet.index("@media (max-width: 640px)") > sheet.index(".ag-daily-chips a")


# ------------------------------------------------------- the wait itself
# Generation happens once a day and used to hold the script run open for as
# long as a provider took (daily.TIMEOUT_S, 25s): the dashboard was painted but
# the run was still open, so nothing on it could be clicked. These cover the
# bounded wait that replaced it.


def test_a_slow_provider_does_not_hold_the_page(page, free, stored, monkeypatch):
    """Past GRACE_S the model goes to a thread, the computed briefing paints
    with a line saying so, and the real card is picked up on a later run."""
    monkeypatch.setattr(daily_ui, "GRACE_S", 0.05)
    gate = threading.Event()
    answer = free.complete

    def blocking(api_key, model, system, messages):
        gate.wait(10)
        return answer(api_key, model, system, messages)

    monkeypatch.setattr(free, "complete", blocking)
    page.run()
    assert not page.exception
    body = _card(page)
    assert "Nvidia carries the day" not in body     # not written yet
    assert "Portfolio +1.20% today" in body         # the computed card stands
    assert "still writing" in body                  # and says it will improve
    assert page.caption[0].value.startswith("Today's figures straight")
    assert not stored                               # nothing stored yet either

    gate.set()
    page.session_state["daily_action_job"]["thread"].join(10)
    page.run()
    assert not page.exception
    assert "Nvidia carries the day" in _card(page)
    assert stored["headline"] == "Nvidia carries the day"
    assert len(free.calls) == 1                     # and only one call bought


def test_a_slow_regenerate_clears_the_card_it_replaces(page, free, monkeypatch):
    """The reader asked for a new briefing, so the old one goes on the click:
    leaving it up — or substituting the computed card for it — has them
    reading a card they just dismissed. The page must still come back, with
    the writing placeholder and a refresh button that cannot be clicked
    again into a second thread."""
    page.run()
    monkeypatch.setattr(daily_ui, "GRACE_S", 0.05)
    gate = threading.Event()
    answer = free.complete
    monkeypatch.setattr(
        free, "complete",
        lambda *a: (gate.wait(10), answer(*a))[1],
    )
    page.button(key="daily_action_refresh").click().run()
    assert not page.exception
    body = _card(page)
    assert "Nvidia carries the day" not in body      # the card it replaces
    assert "Portfolio +1.20% today" not in body      # nor a stand-in for it
    assert "Rewriting today" in body and "topstocks-sk" in body
    assert page.button(key="daily_action_refresh").disabled
    assert not page.caption                          # nothing yet to disclaim

    free.reply = json.dumps(
        {"headline": "Second look", "bullets": ["one", "two"], "focus": []}
    )
    gate.set()
    page.session_state["daily_action_job"]["thread"].join(10)
    page.run()
    body = _card(page)
    assert "Second look" in body and "Nvidia carries the day" not in body
    assert not page.button(key="daily_action_refresh").disabled


def test_the_resolved_card_survives_a_rerun_without_the_store(page, free, stored):
    """The session keeps the card it resolved: a rerun that can no longer read
    the store (a bucket blip, a fragment rerun holding the parent's stale
    `stored`) must redraw it, not fall back to the computed one."""
    page.run()
    stored.clear()
    page.run()
    assert "Nvidia carries the day" in _card(page)
    assert len(free.calls) == 1


# ------------------------------------------------------------ placeholder

RESERVE_SCRIPT = """
from stocks.web import daily_ui

daily_ui.reserve()
"""


def _reserve_page(monkeypatch, card: dict) -> AppTest:
    monkeypatch.setattr(auth, "load_action", lambda *a, **k: dict(card))
    at = AppTest.from_string(RESERVE_SCRIPT, default_timeout=30)
    at.session_state["active_lang"] = "en"
    return at


def test_the_placeholder_names_the_wait_it_covers(monkeypatch):
    """With no card for today the slot covers a model call — an anonymous
    shimmer over a multi-second wait reads as a page that broke."""
    at = _reserve_page(monkeypatch, {}).run()
    assert not at.exception
    body = "".join(el.body for el in at.get("html"))
    assert "Writing today" in body
    assert "takes a few seconds" in body
    assert "topstocks-sk" in body  # the shimmer still holds the card's shape


def test_the_placeholder_stays_quiet_when_today_is_already_stored(monkeypatch):
    """Nothing is being written — the slot is up for one paint, and a line
    about the assistant would flash on every page load."""
    at = _reserve_page(monkeypatch, {
        "day": daily.action_day(datetime.now()).isoformat(),
        "headline": "Stored", "bullets": ["one"], "lang": "en",
    }).run()
    assert not at.exception
    body = "".join(el.body for el in at.get("html"))
    assert "Writing today" not in body
    assert "topstocks-sk" in body


def test_the_phone_bullet_marker_is_a_literal_glyph():
    """A CSS escape written inside a Python string is a Python escape first:
    "\\25B8" is the octal \\25 plus "B8", which shipped an invisible control
    character and a purple "B8" as the bullet on every phone."""
    assert 'content: "▸"' in daily_ui._CSS
    assert "\x15" not in daily_ui._CSS


def test_the_phone_sheet_gives_the_two_actions_a_surface():
    """A tertiary button is a label with no fill: stacked full-width on a
    phone the pair reads as two stray captions, not as the card's controls."""
    block = daily_ui._CSS.split("@media (max-width: 640px)")[1]
    for key in ("daily_action_ask", "daily_action_refresh"):
        assert f".st-key-{key} button" in block
    assert "min-height: 44px" in block  # the DS touch target


def test_the_style_block_has_no_left_angle_bracket():
    """DOMPurify silently drops a whole style block whose text holds one — no
    error, no console warning. It shipped: a comment in the phone block said
    "st-key-" plus an angle-bracketed placeholder, and production painted the
    card with none of its own chrome (badge, tight list, chips, buttons)."""
    body = daily_ui._CSS.split("<style>")[1].split("</style>")[0]
    assert "<" not in body
