"""Candidate actions for the daily card (stocks.chat.signals).

These are the triggers the card is allowed to talk about, so what matters is
that each one only fires when the book really justifies it: an alert level the
user set, a loss with a booked gain behind it, a print close enough to decide
before. Everything here is pure — no frame is fetched, no clock is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import pytest

from stocks.chat import signals
from stocks.config import Alert, Holding
from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale

TODAY = date(2026, 9, 3)


@dataclass
class Event:
    ticker: str
    date: date
    days_until: int


def positions(**over) -> pd.DataFrame:
    """Two positions: NVDA up and heavy, ASML deep under cost."""
    frame = pd.DataFrame(
        {
            "shares": [10.0, 20.0],
            "ccy": ["USD", "EUR"],
            "cost": [3000.0, 8000.0],
            "value": [6000.0, 5600.0],
            "pnl": [3000.0, -2400.0],
            "pnl_pct": [1.0, -0.30],
            "weight": [0.52, 0.48],
        },
        index=["NVDA", "ASML"],
    )
    for col, values in over.items():
        frame[col] = values
    return frame


def sale(gain: float, sell_date: str = "2026-03-10") -> RealizedSale:
    return RealizedSale(
        ticker="MSFT", buy_date="2024-01-05", sell_date=sell_date, quantity=5,
        cost=1000.0, proceeds=1000.0 + gain, currency="EUR",
    )


# ------------------------------------------------------------ user's alerts


def test_a_fired_price_alert_is_the_top_action():
    holdings = [Holding("ASML", alerts=[Alert("below", price=300.0)])]
    out = signals.candidates(
        holdings=holdings, closes={"ASML": [305.0, 280.0]}, today=TODAY
    )
    assert [s.kind for s in out] == [signals.ALERT_HIT]
    assert out[0].data["level"] == 300.0 and out[0].data["price"] == 280.0
    assert out[0].data["rule"] == "below"


def test_an_alert_within_reach_is_a_softer_action():
    holdings = [Holding("ASML", alerts=[Alert("below", price=300.0)])]
    out = signals.candidates(
        holdings=holdings, closes={"ASML": [320.0, 306.0]}, today=TODAY
    )
    assert out[0].kind == signals.ALERT_NEAR
    assert out[0].data["gap_pct"] == pytest.approx(2.0)


def test_a_distant_alert_says_nothing():
    holdings = [Holding("ASML", alerts=[Alert("below", price=200.0)])]
    out = signals.candidates(
        holdings=holdings, closes={"ASML": [320.0, 306.0]}, today=TODAY
    )
    assert out == []


def test_history_based_alerts_are_left_to_the_notification_path():
    """drawdown/RSI/SMA rules need a full price history — a fetch this card
    does not make."""
    holdings = [Holding("ASML", alerts=[Alert("drawdown", pct=20.0)])]
    out = signals.candidates(
        holdings=holdings, closes={"ASML": [320.0, 306.0]}, today=TODAY
    )
    assert out == []


def test_an_unpriced_ticker_raises_nothing():
    holdings = [Holding("ZZZZ", alerts=[Alert("below", price=10.0)])]
    assert signals.candidates(holdings=holdings, closes={}, today=TODAY) == []


# ---------------------------------------------------------------- the ledger


def test_a_deep_drawdown_asks_for_a_thesis_review():
    out = signals.candidates(tbl=positions(), today=TODAY)
    kinds = {s.kind: s for s in out}
    assert kinds[signals.DRAWDOWN].ticker == "ASML"
    assert kinds[signals.DRAWDOWN].data["pnl_pct"] == -30.0


def test_concentration_fires_on_the_heavy_name_only():
    book = positions(weight=[0.52, 0.28])
    out = [s for s in signals.candidates(tbl=book, today=TODAY)
           if s.kind == signals.CONCENTRATION]
    assert [s.ticker for s in out] == ["NVDA"]
    assert out[0].data["weight_pct"] == 52.0


def test_harvest_needs_a_booked_gain_to_offset():
    """An open loss on its own is a fact the P/L column already shows; it
    becomes an action only against a realised gain."""
    assert not [
        s for s in signals.candidates(tbl=positions(), realized=[], today=TODAY)
        if s.kind == signals.HARVEST
    ]
    out = [
        s for s in signals.candidates(
            tbl=positions(), realized=[sale(900.0)],
            jurisdiction=tax.get("ES"), today=TODAY,
        )
        if s.kind == signals.HARVEST
    ]
    assert len(out) == 1 and out[0].ticker == "ASML"
    assert out[0].data == {
        "loss": 2400.0, "gain_ytd": 900.0, "offset": 900.0, "pnl_pct": -30.0,
        "currency": "EUR", "jurisdiction": "ES", "repurchase_window": "2m",
    }


def test_a_net_realised_loss_is_not_a_harvest_case():
    out = signals.candidates(
        tbl=positions(), realized=[sale(900.0), sale(-1500.0)],
        jurisdiction=tax.get("ES"), today=TODAY,
    )
    assert not [s for s in out if s.kind == signals.HARVEST]


def test_a_trivial_loss_is_not_worth_an_action():
    small = positions(pnl=[3000.0, -80.0])
    out = signals.candidates(
        tbl=small, realized=[sale(900.0)], jurisdiction=tax.get("ES"), today=TODAY
    )
    assert not [s for s in out if s.kind == signals.HARVEST]


def test_the_tax_year_boundary_follows_the_jurisdiction():
    """A 10 May 2026 disposal is the 2026/27 year in the UK and 2026 in Spain,
    so the same book offers different gains to offset."""
    may = [sale(900.0, "2026-05-10")]
    assert signals.realized_this_year(may, tax.get("UK"), TODAY) == 900.0
    assert signals.realized_this_year(may, tax.get("ES"), TODAY) == 900.0
    april = [sale(900.0, "2026-04-01")]  # before 6 April: last UK year
    assert signals.realized_this_year(april, tax.get("UK"), TODAY) == 0.0
    assert signals.realized_this_year(april, tax.get("ES"), TODAY) == 900.0


def test_no_jurisdiction_falls_back_to_the_calendar_year():
    assert signals.realized_this_year([sale(500.0)], None, TODAY) == 500.0
    assert signals.realized_this_year([sale(500.0, "2025-12-30")], None, TODAY) == 0.0


# ------------------------------------------------------------ calendar, price


def test_a_print_on_a_held_name_is_a_decision_date():
    out = [s for s in signals.candidates(
        tbl=positions(), earnings=[Event("ASML", date(2026, 9, 5), 2)], today=TODAY
    ) if s.kind == signals.EARNINGS]
    assert out[0].data["in_days"] == 2
    # Sooner sorts higher: tomorrow's print outranks Friday's.
    both = signals.candidates(
        tbl=positions(),
        earnings=[Event("ASML", date(2026, 9, 7), 4), Event("NVDA", date(2026, 9, 4), 1)],
        today=TODAY,
    )
    prints = [s.ticker for s in both if s.kind == signals.EARNINGS]
    assert prints == ["NVDA", "ASML"]


def test_a_print_on_a_name_you_do_not_hold_is_only_news():
    out = signals.candidates(
        tbl=positions(), earnings=[Event("TSLA", date(2026, 9, 5), 2)], today=TODAY
    )
    assert not [s for s in out if s.kind == signals.EARNINGS]


def test_a_watchlist_name_at_its_low_is_an_entry_case():
    out = [s for s in signals.candidates(
        tbl=positions(), extremes=[("TSLA", 180.0, "low", -0.012)], today=TODAY
    ) if s.kind == signals.LOW_52W]
    assert out[0].ticker == "TSLA" and out[0].data["gap_pct"] == 1.2


def test_a_held_name_at_its_low_is_left_to_the_drawdown_line():
    out = signals.candidates(
        tbl=positions(), extremes=[("ASML", 180.0, "low", -0.01)], today=TODAY
    )
    assert not [s for s in out if s.kind == signals.LOW_52W]


def test_a_52_week_high_is_not_an_action():
    out = signals.candidates(
        tbl=positions(), extremes=[("TSLA", 180.0, "high", None)], today=TODAY
    )
    assert out == [s for s in out if s.kind != signals.LOW_52W]


# ------------------------------------------------------------------ the set


def test_urgency_order_and_cap():
    out = signals.candidates(
        holdings=[Holding("ASML", alerts=[Alert("below", price=300.0)])],
        closes={"ASML": [305.0, 280.0]},
        tbl=positions(),
        realized=[sale(900.0)],
        jurisdiction=tax.get("ES"),
        earnings=[Event("ASML", date(2026, 9, 5), 2)],
        today=TODAY,
        limit=3,
    )
    assert [s.kind for s in out] == [
        signals.ALERT_HIT, signals.HARVEST, signals.EARNINGS
    ]


def test_an_empty_book_raises_no_actions():
    assert signals.candidates(tbl=pd.DataFrame(), today=TODAY) == []
