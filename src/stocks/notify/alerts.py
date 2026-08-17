"""Evaluate watchlist alerts against price history.

Beyond simple price thresholds ('above'/'below'), rules fire on daily moves,
drawdown from a trailing high, RSI oversold/overbought, SMA crosses, and new
52-week highs/lows. `evaluate` is pure (takes a price frame); check_holding /
check_all pull the history via yfinance.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

from stocks.analysis.indicators import rsi, sma
from stocks.config import Alert, Holding, load_watchlist
from stocks.data.fetch import fetch_history

# Enough history for a 52-week high/low and long SMAs.
ALERT_PERIOD = "1y"
RSI_DEFAULT_LEVELS = {"rsi_below": 30.0, "rsi_above": 70.0}


@dataclass
class AlertHit:
    ticker: str
    type: str
    message: str
    value: float | None = None
    alert: Alert | None = None  # the rule that fired, for dedupe fingerprints

    def __str__(self) -> str:
        return f"{self.ticker}: {self.message}"


def _daily_return(close: pd.Series) -> float | None:
    if len(close) < 2:
        return None
    return float(close.iloc[-1] / close.iloc[-2] - 1)


def evaluate(ticker: str, alert: Alert, df: pd.DataFrame) -> AlertHit | None:
    """Return an AlertHit if `alert` fires on `df` (needs a 'Close' column)."""
    close = df["Close"].dropna() if "Close" in df else pd.Series(dtype=float)
    if close.empty:
        return None
    last = float(close.iloc[-1])
    t = alert.type

    def hit(msg: str, value: float | None = None) -> AlertHit:
        return AlertHit(ticker, t, msg, value, alert)

    if t in ("above", "below"):
        if alert.triggered(last):
            return hit(f"{t} {alert.price:g} (last {last:.2f})", last)
        return None

    if t == "pct_move":
        r = _daily_return(close)
        if r is not None and alert.pct is not None and abs(r) * 100 >= alert.pct:
            return hit(f"daily move {r * 100:+.1f}% (>= {alert.pct:g}%)", r)
        return None

    if t == "drawdown":
        window = alert.window or len(close)
        high = float(close.tail(window).max())
        dd = last / high - 1 if high else 0.0
        if alert.pct is not None and -dd * 100 >= alert.pct:
            return hit(
                f"drawdown {dd * 100:.1f}% from high {high:.2f} (>= {alert.pct:g}%)", dd
            )
        return None

    if t in ("rsi_below", "rsi_above"):
        window = alert.window or 14
        series = rsi(close, window).dropna()
        if series.empty:
            return None
        val = float(series.iloc[-1])
        level = alert.level if alert.level is not None else RSI_DEFAULT_LEVELS[t]
        fired = val <= level if t == "rsi_below" else val >= level
        if fired:
            op = "<=" if t == "rsi_below" else ">="
            return hit(f"RSI{window} {val:.1f} {op} {level:g}", val)
        return None

    if t == "sma_cross":
        window = alert.window or 50
        diff = (close - sma(close, window)).dropna()
        if len(diff) < 2:
            return None
        prev, cur = float(diff.iloc[-2]), float(diff.iloc[-1])
        level = float(sma(close, window).iloc[-1])
        if prev < 0 <= cur:
            return hit(f"crossed above SMA{window} ({last:.2f} vs {level:.2f})", last)
        if prev > 0 >= cur:
            return hit(f"crossed below SMA{window} ({last:.2f} vs {level:.2f})", last)
        return None

    if t == "high_52w":
        window = alert.window or 252
        if last >= float(close.tail(window).max()):
            return hit(f"new {window}-day high {last:.2f}", last)
        return None

    if t == "low_52w":
        window = alert.window or 252
        if last <= float(close.tail(window).min()):
            return hit(f"new {window}-day low {last:.2f}", last)
        return None

    raise ValueError(f"unknown alert type: {t!r}")


def check_holding(holding: Holding, df: pd.DataFrame | None = None) -> list[AlertHit]:
    if not holding.alerts:
        return []
    if df is None:
        df = fetch_history(holding.ticker, period=ALERT_PERIOD)
    hits = [evaluate(holding.ticker, a, df) for a in holding.alerts]
    return [h for h in hits if h is not None]


def _safe_check(holding: Holding) -> list[AlertHit]:
    """check_holding, isolated: one dead ticker must not abort the whole run."""
    try:
        return check_holding(holding)
    except Exception:
        return []


def check_holdings(
    holdings: list[Holding],
    frames: dict[str, pd.DataFrame] | None = None,
    max_workers: int = 8,
) -> list[AlertHit]:
    """Evaluate a list of holdings, optionally against pre-fetched frames.

    With `frames` (the multi-user cron path: one fetch per distinct ticker
    shared across accounts) a holding whose ticker is missing is skipped —
    its fetch failed upstream. Without, each holding fetches its own history
    concurrently, like check_all always has.
    """
    holdings = [h for h in holdings if h.alerts]
    if frames is not None:
        hits: list[AlertHit] = []
        for h in holdings:
            df = frames.get(h.ticker)
            if df is not None:
                hits.extend(check_holding(h, df))
        return hits
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(_safe_check, holdings)
    return [hit for r in results for hit in r]


def check_all(max_workers: int = 8) -> list[AlertHit]:
    return check_holdings(load_watchlist(), max_workers=max_workers)
