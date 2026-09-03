"""The dashboard's daily action card, headless (stocks.chat.daily).

Everything the card promises is here: it turns over once a day at the local
cutoff, it only ever states numbers it was given, an unusable completion is a
miss rather than a broken card, and a page with no model still gets a
briefing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
import pytest

from stocks.chat import daily, engine, signals


@dataclass
class FakeProvider:
    id: str
    reply: str = ""
    fail: bool = False
    is_available: bool = True
    calls: list = field(default_factory=list)
    systems: list = field(default_factory=list)
    sent: list = field(default_factory=list)  # the facts each attempt was given

    def available(self) -> bool:
        return self.is_available

    def complete(self, api_key, model, system, messages) -> str:
        self.calls.append((api_key, model))
        self.systems.append(system)
        self.sent.append(json.loads(messages[-1]["content"]))
        if self.fail:
            raise RuntimeError("rate limited")
        return self.reply


@pytest.fixture(autouse=True)
def free_pot(monkeypatch, tmp_path):
    """Isolate the process-wide free pot — generation spends it for real."""
    monkeypatch.setattr(engine, "GLOBAL_FREE_FILE", tmp_path / "free_llm_global.json")
    monkeypatch.setattr(engine, "_global_free", {"day": "", "used": 0})
    monkeypatch.setattr(engine, "_global_free_loaded", True)


@pytest.fixture
def providers(monkeypatch):
    from stocks.web import llm

    fakes = {"free": FakeProvider("free", reply=json.dumps(REPLY))}
    monkeypatch.setattr(llm, "PROVIDERS", fakes)
    return fakes


REPLY = {
    "headline": "Nvidia carries the day",
    "bullets": [
        "NVDA +3.2% today, 30% of the book — check the concentration",
        "ASML reports in 4 days",
    ],
    "focus": ["NVDA", "TSLA"],  # TSLA is not in the facts — must be dropped
}

DAY = date(2026, 9, 3)


@dataclass
class Event:
    ticker: str
    date: date
    days_until: int


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "value": [3000.0, 7000.0],
            "cost": [2500.0, 6000.0],
            "pnl_pct": [0.20, 0.166],
            "weight": [0.30, 0.70],
            "day_pct": [0.032, -0.004],
        },
        index=["NVDA", "ASML"],
    )


def history() -> pd.DataFrame:
    idx = pd.date_range("2026-08-01", periods=34, freq="D")
    return pd.DataFrame(
        {"NVDA": [2800.0 + i * 6 for i in range(34)],
         "ASML": [6800.0 + i * 6 for i in range(34)]},
        index=idx,
    )


ACTIONS = [
    signals.Signal(signals.ALERT_HIT, "NVDA", 90, {
        "rule": "below", "level": 120.0, "price": 118.4, "gap_pct": 1.3,
        "held": True,
    }),
    signals.Signal(signals.HARVEST, "ASML", 75, {
        "loss": 2400.0, "gain_ytd": 900.0, "offset": 900.0, "pnl_pct": -30.0,
        "currency": "EUR", "jurisdiction": "ES", "repurchase_window": "2m",
    }),
    signals.Signal(signals.EARNINGS, "ASML", 73, {"in_days": 2,
                                                  "date": "2026-09-05"}),
]


def facts(actions=ACTIONS) -> dict:
    return daily.build_facts(
        frame(),
        history(),
        currency="EUR",
        earnings=[Event("ASML", date(2026, 9, 7), 4)],
        extremes=[("NVDA", 120.0, "high", None)],
        signals=actions,
        today=DAY,
    )


# ------------------------------------------------------------ the day turns


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(0, date(2026, 9, 2)), (8, date(2026, 9, 2)), (9, DAY), (23, DAY)],
)
def test_action_day_turns_over_at_the_cutoff(hour, expected):
    assert daily.action_day(datetime(2026, 9, 3, hour, 30)) == expected


def test_stored_card_is_fresh_only_for_its_day_and_language():
    card = daily.DailyAction(day=DAY.isoformat(), headline="h", bullets=["a"], lang="es")
    assert daily.is_fresh(card, DAY, "es")
    assert not daily.is_fresh(card, date(2026, 9, 4), "es")
    assert not daily.is_fresh(card, DAY, "en")  # switched language -> regenerate
    assert not daily.is_fresh(None, DAY, "es")


# ------------------------------------------------------------------ facts


def test_facts_carry_the_book_the_page_is_showing():
    f = facts()
    assert f["total_value"] == 10000.0
    assert f["unrealised_pl_pct"] == pytest.approx(17.65, abs=0.01)
    assert [w["ticker"] for w in f["top_weights"]] == ["ASML", "NVDA"]
    # Movers rank by absolute move, so the day's story is first whichever way
    # it went.
    assert f["movers_today"][0] == {"ticker": "NVDA", "pct": 3.2, "weight_pct": 30.0}
    assert f["earnings_soon"] == [
        {"ticker": "ASML", "date": "2026-09-07", "in_days": 4}
    ]
    assert f["at_52w"] == [{"ticker": "NVDA", "kind": "high", "distance_pct": None}]
    assert f["week"]["pct"] is not None and f["month"]["pct"] is not None
    json.dumps(f)  # no NaN, no numpy scalars — this goes into a prompt


def test_day_override_wins_over_the_basket():
    """Home resolves "today" off-session from the quotes; the card must quote
    the same figure as the tile above it, not the close-to-close basket."""
    f = daily.build_facts(frame(), history(), day=(-120.0, -0.012), today=DAY)
    assert f["day"] == {"amount": -120.0, "pct": -1.2}


def test_facts_survive_an_empty_book():
    f = daily.build_facts(pd.DataFrame(), None, today=DAY)
    assert f == {"date": "2026-09-03", "currency": "EUR"}


def test_far_out_earnings_are_left_out():
    f = daily.build_facts(
        frame(), None, earnings=[Event("ASML", date(2026, 12, 1), 89)], today=DAY
    )
    assert "earnings_soon" not in f


# ------------------------------------------------------------------ parsing


def test_parse_accepts_a_fenced_reply_and_drops_unknown_tickers():
    raw = "Sure!\n```json\n" + json.dumps(REPLY) + "\n```"
    card = daily.parse(raw, day=DAY, lang="en", known={"NVDA", "ASML"})
    assert card.headline == "Nvidia carries the day"
    assert len(card.bullets) == 2
    assert card.focus == ["NVDA"]  # TSLA was never in the facts
    assert card.day == DAY.isoformat() and card.from_model


def test_parse_strips_bullet_glyphs_and_clips_long_lines():
    raw = json.dumps({"headline": "x" * 200, "bullets": ["- one", "• " + "y" * 400]})
    card = daily.parse(raw, day=DAY, lang="en")
    assert card.bullets[0] == "one"
    assert len(card.bullets[1]) == daily.BULLET_CHARS
    assert len(card.headline) == daily.HEADLINE_CHARS


@pytest.mark.parametrize(
    "raw",
    [
        "I cannot help with that.",
        json.dumps({"headline": "h", "bullets": []}),
        json.dumps({"headline": "h", "bullets": ["only one"]}),
        json.dumps(["not", "an", "object"]),
        "",
    ],
)
def test_unusable_replies_are_rejected(raw):
    assert daily.parse(raw, day=DAY, lang="en") is None


def test_headline_falls_back_to_the_first_bullet():
    raw = json.dumps({"bullets": ["first line", "second line"]})
    assert daily.parse(raw, day=DAY, lang="en").headline == "first line"


# ------------------------------------------------------------- generation


def test_generate_returns_a_card_and_prompts_with_the_facts(providers):
    prefs = {}
    card = daily.generate(prefs, {}, facts(), "es", DAY)
    assert card.headline == "Nvidia carries the day"
    assert card.lang == "es" and card.source == "llm"
    system = providers["free"].systems[0]
    assert "Spanish" in system
    assert "RULES" in system  # the shared chat guardrails ride along


def test_generate_rejects_junk_and_gives_up(providers):
    providers["free"].reply = "no JSON here"
    assert daily.generate({}, {}, facts(), "en", DAY) is None


def test_generate_survives_a_dead_provider(providers):
    providers["free"].fail = True
    assert daily.generate({}, {}, facts(), "en", DAY) is None


def test_generate_spends_the_account_allowance(providers, monkeypatch):
    monkeypatch.setenv("FREE_LLM_DAILY_CAP", "1")
    prefs = {}
    assert daily.generate(prefs, {}, facts(), "en", DAY)
    day = time.strftime("%Y-%m-%d")
    assert prefs[f"free_msgs::{day}"] == 1
    # Allowance gone: the card degrades to computed rather than draining the
    # operator's shared keys.
    assert daily.generate(prefs, {}, facts(), "en", DAY) is None
    assert len(providers["free"].calls) == 1


def test_previous_headlines_are_shown_to_the_next_call(providers):
    daily.generate({}, {}, facts(), "en", DAY, recent=["Yesterday's line"])
    assert "Yesterday's line" in providers["free"].systems[0]


def test_remembered_chains_the_last_headlines():
    stored = daily.DailyAction(
        day="2026-09-02", headline="older", bullets=["b"], recent=["older", "oldest"]
    )
    assert daily.remembered(stored, "newest") == ["newest", "older", "oldest"]
    assert daily.remembered(None, "first") == ["first"]
    assert len(daily.remembered(stored, "newest")) <= daily.RECENT_KEPT


# -------------------------------------------------------- computed fallback


def test_computed_card_states_the_actions_without_a_model():
    card = daily.computed(facts(), "en", DAY)
    assert card.source == "computed" and not card.from_model
    assert daily.MIN_BULLETS <= len(card.bullets) <= daily.MAX_BULLETS
    body = " ".join([card.headline, *card.bullets])
    # Each line is a trigger, not a figure: the alert the user set, the tax
    # arithmetic with its repurchase rule, the print to decide before.
    assert "your below alert at 120.00 fired" in body
    assert "would offset €900 of the €900" in body
    assert body.count("your below alert") == 1  # the headline is not repeated
    assert "repurchasing within two months blocks the loss" in body.lower()
    assert "reports in 2 days" in body
    assert card.focus[:2] == ["NVDA", "ASML"]


def test_a_book_with_nothing_triggered_says_so():
    """The honest version of "no action today" — never a figure dressed up as
    a decision."""
    card = daily.computed(facts(actions=[]), "en", DAY)
    assert card.headline == "Nothing needs a decision today"
    assert any("+0.12%" in b for b in card.bullets)  # the day's move, as context


def test_the_actions_lead_the_prompt(providers):
    daily.generate({}, {}, facts(), "en", DAY)
    sent = providers["free"].sent[0]
    assert [a["kind"] for a in sent["actions"]] == [
        signals.ALERT_HIT, signals.HARVEST, signals.EARNINGS
    ]
    assert sent["actions"][0]["level"] == 120.0


def test_computed_card_is_localized():
    es = daily.computed(facts(), "es", DAY)
    en = daily.computed(facts(), "en", DAY)
    assert es.bullets != en.bullets
    assert "recomprar en dos meses" in " ".join(es.bullets)


def test_computed_card_is_never_empty():
    card = daily.computed({"date": DAY.isoformat(), "currency": "EUR"}, "en", DAY)
    assert card.bullets  # a brand-new account still gets a line


# --------------------------------------------------------------- storage


def test_card_round_trips_through_its_dict():
    card = daily.parse(json.dumps(REPLY), day=DAY, lang="en", known={"NVDA"})
    again = daily.DailyAction.from_dict(card.to_dict())
    assert again == card


@pytest.mark.parametrize(
    "raw", [None, {}, {"day": "2026-09-03"}, {"bullets": ["a"]}, "not a dict"]
)
def test_unusable_stored_cards_read_as_nothing_stored(raw):
    assert daily.DailyAction.from_dict(raw) is None
