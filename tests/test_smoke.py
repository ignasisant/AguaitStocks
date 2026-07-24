"""Smoke tests — no network required."""

import pandas as pd

from stocks.analysis.indicators import add_indicators, rsi, sma
from stocks.config import Alert, load_watchlist


def sample_series() -> pd.Series:
    return pd.Series(range(1, 101), dtype=float)


def test_sma_length():
    s = sample_series()
    assert len(sma(s, 20)) == len(s)


def test_rsi_bounds():
    r = rsi(sample_series()).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_alert_triggered():
    assert Alert("above", 100).triggered(105)
    assert Alert("below", 100).triggered(95)
    assert not Alert("above", 100).triggered(95)


def test_add_indicators_columns():
    df = pd.DataFrame({"Close": sample_series()})
    out = add_indicators(df)
    assert {"SMA20", "SMA50", "EMA20", "RSI14", "Return"} <= set(out.columns)


def test_load_watchlist_returns_list():
    assert isinstance(load_watchlist(), list)
