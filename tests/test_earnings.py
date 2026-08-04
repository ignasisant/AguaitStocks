"""Earnings calendar tests — pure date logic, no network."""

from datetime import date

from stocks.data.earnings import (
    EarningsEvent,
    EarningsResult,
    add_months,
    build_events,
    group_by_date,
    month_weeks,
    next_after,
)

REF = date(2026, 7, 24)


def test_next_after_picks_soonest_future():
    dates = [date(2026, 1, 1), date(2026, 8, 5), date(2026, 11, 2)]
    assert next_after(dates, REF) == date(2026, 8, 5)


def test_next_after_all_past_is_none():
    assert next_after([date(2025, 1, 1), date(2026, 7, 23)], REF) is None


def test_next_after_includes_today():
    assert next_after([REF], REF) == REF


def test_build_events_sorted_and_days_until():
    dated = {
        "AAPL": [date(2026, 8, 1)],
        "NVDA": [date(2026, 7, 26)],
        "OLD": [date(2020, 1, 1)],  # no future date -> excluded
    }
    events = build_events(dated, REF)
    assert [e.ticker for e in events] == ["NVDA", "AAPL"]
    assert events[0].days_until == 2
    assert events[1].days_until == 8


def test_build_events_within_days_window():
    dated = {
        "SOON": [date(2026, 7, 30)],
        "LATER": [date(2026, 9, 30)],
    }
    events = build_events(dated, REF, within_days=14)
    assert [e.ticker for e in events] == ["SOON"]


def test_add_months_wraps_year():
    assert add_months(2026, 7, 0) == (2026, 7)
    assert add_months(2026, 12, 1) == (2027, 1)
    assert add_months(2026, 1, -1) == (2025, 12)
    assert add_months(2026, 7, 18) == (2028, 1)


def test_month_weeks_covers_month_in_7day_rows():
    weeks = month_weeks(2026, 7)
    assert all(len(w) == 7 for w in weeks)
    flat = [d for w in weeks for d in w]
    # Every day of July is present, and each row starts on a Monday.
    assert date(2026, 7, 1) in flat and date(2026, 7, 31) in flat
    assert all(w[0].weekday() == 0 for w in weeks)


def test_group_by_date_indexes_and_drops_undated():
    events = [
        EarningsEvent("AAPL", date(2026, 8, 1), 8),
        EarningsEvent("MSFT", date(2026, 8, 1), 8),
        EarningsEvent("NONE", None, None),
    ]
    grouped = group_by_date(events)
    assert {e.ticker for e in grouped[date(2026, 8, 1)]} == {"AAPL", "MSFT"}
    assert date(2020, 1, 1) not in grouped
    assert all(d is not None for d in grouped)


def test_group_by_date_accepts_results():
    results = [
        EarningsResult("AAPL", date(2026, 5, 1), reported_eps=1.4),
        EarningsResult("MSFT", date(2026, 5, 1), reported_eps=2.9),
    ]
    grouped = group_by_date(results)
    assert {r.ticker for r in grouped[date(2026, 5, 1)]} == {"AAPL", "MSFT"}


def test_result_beat_prefers_surprise():
    assert EarningsResult("X", date(2026, 5, 1), surprise_pct=3.2).beat is True
    assert EarningsResult("X", date(2026, 5, 1), surprise_pct=-0.1).beat is False
    # Surprise wins even when the raw EPS comparison disagrees (rounding).
    r = EarningsResult(
        "X", date(2026, 5, 1), eps_estimate=1.0, reported_eps=0.99, surprise_pct=0.5
    )
    assert r.beat is True


def test_result_beat_falls_back_to_eps_comparison():
    assert (
        EarningsResult("X", date(2026, 5, 1), eps_estimate=1.0, reported_eps=1.2).beat
        is True
    )
    assert (
        EarningsResult("X", date(2026, 5, 1), eps_estimate=1.0, reported_eps=0.8).beat
        is False
    )
    assert EarningsResult("X", date(2026, 5, 1), reported_eps=1.2).beat is None
    assert EarningsResult("X", date(2026, 5, 1)).beat is None
