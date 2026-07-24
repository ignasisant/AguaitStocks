"""Technical indicators on price series."""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int = 20) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window).mean()


def ema(series: pd.Series, span: int = 20) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Relative Strength Index (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = -delta.clip(upper=0).rolling(window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def daily_returns(series: pd.Series) -> pd.Series:
    return series.pct_change()


def add_indicators(df: pd.DataFrame, price_col: str = "Close") -> pd.DataFrame:
    """Append common indicator columns to an OHLCV frame."""
    out = df.copy()
    out["SMA20"] = sma(out[price_col], 20)
    out["SMA50"] = sma(out[price_col], 50)
    out["EMA20"] = ema(out[price_col], 20)
    out["RSI14"] = rsi(out[price_col], 14)
    out["Return"] = daily_returns(out[price_col])
    return out
