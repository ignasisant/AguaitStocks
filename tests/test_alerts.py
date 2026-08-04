"""Alert evaluation tests — synthetic price frames, no network."""

import pandas as pd
import pytest

from stocks.config import Alert
from stocks.notify.alerts import evaluate


def df_from(prices: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="D")
    return pd.DataFrame({"Close": [float(p) for p in prices]}, index=idx)


def test_above_below():
    up = df_from([100, 101, 105])
    assert evaluate("X", Alert("above", price=104), up) is not None
    assert evaluate("X", Alert("above", price=110), up) is None
    down = df_from([100, 99, 95])
    assert evaluate("X", Alert("below", price=96), down) is not None
    assert evaluate("X", Alert("below", price=90), down) is None


def test_pct_move():
    jump = df_from([100, 100, 110])  # +10% on last bar
    assert evaluate("X", Alert("pct_move", pct=5), jump) is not None
    assert evaluate("X", Alert("pct_move", pct=15), jump) is None


def test_drawdown():
    dd = df_from([100, 120, 84])  # 84/120 - 1 = -30%
    hit = evaluate("X", Alert("drawdown", pct=20), dd)
    assert hit is not None and hit.value < 0
    assert evaluate("X", Alert("drawdown", pct=40), dd) is None


def test_rsi_extremes():
    rising = df_from(list(range(1, 40)))  # RSI -> ~100
    assert evaluate("X", Alert("rsi_above", level=70), rising) is not None
    falling = df_from(list(range(40, 1, -1)))  # RSI -> ~0
    assert evaluate("X", Alert("rsi_below", level=30), falling) is not None
    # defaults (30/70) apply when level omitted
    assert evaluate("X", Alert("rsi_below"), falling) is not None


def test_sma_cross_up_and_down():
    up = df_from([10, 10, 10, 10, 10, 5, 20])
    hit = evaluate("X", Alert("sma_cross", window=3), up)
    assert hit is not None and "above" in hit.message
    down = df_from([10, 10, 10, 10, 10, 20, 5])
    hit = evaluate("X", Alert("sma_cross", window=3), down)
    assert hit is not None and "below" in hit.message


def test_52w_high_low():
    hi = df_from([1, 2, 3, 4, 10])
    assert evaluate("X", Alert("high_52w", window=5), hi) is not None
    lo = df_from([10, 4, 3, 2, 1])
    assert evaluate("X", Alert("low_52w", window=5), lo) is not None
    # not a new high on the last bar
    assert evaluate("X", Alert("high_52w", window=5), df_from([1, 9, 3, 4, 5])) is None


def test_unknown_alert_type_rejected_at_construction():
    with pytest.raises(ValueError):
        Alert("bogus", price=1)
