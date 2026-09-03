"""The daily action card as the dashboard renders it (stocks.web.daily_ui).

Runs the real render through AppTest: the card is the first AI surface a user
meets, it costs a model call, and the rules that keep it cheap and safe —
generate once a day, reuse the stored copy, escape the model's text, never
take the page down — only exist end to end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from stocks.chat import daily, engine
from stocks.web import auth, daily_ui

SCRIPT = """
import streamlit as st

from stocks.web import daily_ui, skeletons

slot = skeletons.reserve("text", border=True, title=True, lines=4)
daily_ui.render(
    slot, tbl=None, hist=None, currency="EUR", day_change=(120.0, 0.012),
    holdings=st.session_state.get("holdings", []),
    closes=st.session_state.get("closes", {}),
)
"""

REPLY = {
    "headline": "Nvidia carries the day",
    "bullets": ["NVDA +3.2% today", "ASML reports in 4 days"],
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


def test_the_card_is_generated_once_and_then_read_from_the_store(page, free, stored):
    page.run()
    assert not page.exception
    body = _card(page)
    assert "Nvidia carries the day" in body
    assert "NVDA +3.2% today" in body
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
