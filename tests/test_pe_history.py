"""Historic P/E reconstruction tests — pure math over synthetic facts, no network."""

from datetime import date

import pandas as pd
import pytest

from stocks.analysis.pe_history import (
    discrete_quarters,
    interpret,
    pe_series,
    reconstruct_ttm_eps,
    split_factors,
    window_stats,
)


def _facts(rows):
    return pd.DataFrame(rows, columns=["end", "filed", "eps", "kind"])


FACTS = _facts([
    (date(2025, 3, 31), date(2025, 5, 1), 1.0, "Q"),
    (date(2025, 6, 30), date(2025, 8, 1), 2.0, "Q"),
    (date(2025, 9, 30), date(2025, 11, 1), 3.0, "Q"),
    (date(2025, 12, 31), date(2026, 2, 15), 10.0, "FY"),  # Q4 = 10 − 6 = 4
])


def test_split_factors_cumulative_after_end():
    splits = {date(2025, 7, 15): 10.0, date(2026, 1, 10): 2.0}
    ends = [date(2025, 3, 31), date(2025, 9, 30), date(2026, 3, 31)]
    out = split_factors(splits, ends)
    assert out[date(2025, 3, 31)] == 20.0  # both splits after this quarter
    assert out[date(2025, 9, 30)] == 2.0
    assert out[date(2026, 3, 31)] == 1.0


def test_discrete_quarters_derives_q4_from_fy():
    q = discrete_quarters(FACTS)
    assert list(q["eps"]) == [1.0, 2.0, 3.0, 4.0]
    # Derived Q4 carries the 10-K's filing date.
    assert q.iloc[-1]["filed"] == date(2026, 2, 15)


def test_discrete_quarters_skips_fy_with_missing_quarters():
    q = discrete_quarters(FACTS.drop(index=1))  # only 2 discrete quarters in year
    assert list(q["eps"]) == [1.0, 3.0]


def test_reconstruct_ttm_eps_stamped_at_filing():
    ttm = reconstruct_ttm_eps(discrete_quarters(FACTS))
    assert list(ttm) == [10.0]  # 1+2+3+4
    assert ttm.index[0] == pd.Timestamp("2026-02-15")


def test_reconstruct_ttm_eps_split_adjusts():
    factors = {date(2025, 3, 31): 2.0}  # 2:1 split after Q1
    ttm = reconstruct_ttm_eps(discrete_quarters(FACTS), factors)
    assert list(ttm) == [9.5]  # 0.5+2+3+4


def test_pe_series_as_of_join_and_positive_filter():
    ttm = pd.Series([10.0], index=pd.DatetimeIndex(["2026-02-15"]))
    close = pd.Series(
        [90.0, 120.0],
        index=pd.DatetimeIndex(["2026-02-14", "2026-02-16"]),
    )
    pe = pe_series(close, ttm)
    # Day before the filing has no known TTM figure -> dropped.
    assert list(pe.index) == [pd.Timestamp("2026-02-16")]
    assert pe.iloc[0] == pytest.approx(12.0)


def test_pe_series_drops_lossmaking():
    ttm = pd.Series([-1.0], index=pd.DatetimeIndex(["2026-01-01"]))
    close = pd.Series([50.0], index=pd.DatetimeIndex(["2026-01-02"]))
    assert pe_series(close, ttm).empty


def test_window_stats_premium_and_percentile():
    idx = pd.date_range("2025-01-01", periods=100, freq="D")
    pe = pd.Series([20.0] * 99 + [30.0], index=idx)
    st = window_stats(pe, windows={"1y": 365})
    row = st.loc["1y"]
    assert row["current"] == 30.0
    assert row["mean"] == pytest.approx(20.1)
    assert row["premium"] == pytest.approx(30.0 / 20.1 - 1)
    assert row["percentile"] == 100.0


def test_interpret_bands():
    assert interpret(90) == "expensive vs own history"
    assert interpret(10) == "cheap vs own history"
    assert interpret(50) == "in line with own history"
