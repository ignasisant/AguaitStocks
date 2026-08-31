"""Earnings calendar tests — pure date logic, no network."""

from datetime import date

import pandas as pd

from stocks.data.earnings import (
    EarningsEvent,
    EarningsResult,
    Quarter,
    add_months,
    build_events,
    group_by_date,
    match_quarter,
    month_weeks,
    next_after,
    pct_change,
    prior_quarter,
    quarters,
    year_ago,
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


# ------------------------------------------------------- quarter figures


def statement() -> pd.DataFrame:
    """Quarterly income statement in yfinance's shape: rows are line items."""
    return pd.DataFrame(
        {
            pd.Timestamp("2026-04-30"): [81615e6, 61157e6, 53536e6, 58321e6,
                                         69903e6, 11582e6, 6321e6, 2.39],
            pd.Timestamp("2026-01-31"): [68127e6, 51093e6, 44299e6, 42960e6,
                                         50398e6, 7438e6, 5512e6, 1.76],
            pd.Timestamp("2025-04-30"): [44062e6, 26668e6, 21638e6, 18775e6,
                                         21910e6, 3135e6, 3989e6, 0.76],
        },
        index=["Total Revenue", "Gross Profit", "Operating Income", "Net Income",
               "Pretax Income", "Tax Provision", "Research And Development",
               "Diluted EPS"],
    )


def test_quarters_parses_newest_first_with_margins():
    qs = quarters(statement())
    assert [q.end for q in qs] == [
        date(2026, 4, 30), date(2026, 1, 31), date(2025, 4, 30)
    ]
    q = qs[0]
    assert q.revenue == 81615e6 and q.diluted_eps == 2.39
    assert round(q.gross_margin, 4) == 0.7493
    assert round(q.operating_margin, 4) == 0.6560
    assert round(q.net_margin, 4) == 0.7146
    assert round(q.tax_rate, 4) == 0.1657


def test_quarters_tolerates_missing_rows_and_empty_columns():
    frame = statement().drop(index=["Gross Profit", "Diluted EPS"])
    frame[pd.Timestamp("2025-01-31")] = [None] * len(frame)
    qs = quarters(frame)
    assert len(qs) == 3  # the all-empty column is dropped
    assert qs[0].gross_profit is None and qs[0].gross_margin is None
    assert qs[0].diluted_eps is None


def test_quarters_of_empty_frame_is_empty():
    assert quarters(pd.DataFrame()) == []


def test_match_quarter_picks_the_quarter_the_print_reported_on():
    qs = quarters(statement())
    matched = match_quarter(qs, date(2026, 5, 20))
    assert matched is not None and matched.end == date(2026, 4, 30)


def test_match_quarter_rejects_an_unpublished_quarter():
    # A print 118 days after the newest quarter on file reports a quarter
    # yfinance has not published yet — showing the old one would be a lie.
    assert match_quarter(quarters(statement()), date(2026, 8, 26)) is None


def test_match_quarter_never_falls_back_a_whole_quarter():
    # Two days after a quarter end is too fast to be that quarter's print, and
    # the one before it (91 days back) is past the lag window — so: unknown,
    # rather than the wrong quarter's figures.
    assert match_quarter(quarters(statement()), date(2026, 5, 2)) is None


def test_year_ago_matches_the_same_fiscal_quarter():
    qs = quarters(statement())
    prior = year_ago(qs, qs[0])
    assert prior is not None and prior.end == date(2025, 4, 30)
    # One quarter back has no year-ago partner in this history.
    assert year_ago(qs, qs[1]) is None


def test_prior_quarter_is_the_one_immediately_before():
    qs = quarters(statement())
    assert prior_quarter(qs, qs[0]).end == date(2026, 1, 31)
    assert prior_quarter(qs, qs[-1]) is None


def test_pct_change_guards_a_nonpositive_base():
    assert round(pct_change(81615e6, 44062e6), 4) == 0.8523
    assert pct_change(2.0, 0.0) is None
    assert pct_change(2.0, -1.0) is None
    assert pct_change(None, 1.0) is None


def test_margins_need_revenue():
    bare = Quarter(end=date(2026, 4, 30), net_income=100.0)
    assert bare.net_margin is None and bare.tax_rate is None
