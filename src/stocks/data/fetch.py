"""Fetch OHLCV price data via yfinance; cache to CSV under data/."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from stocks import obs
from stocks.config import DATA_DIR, ticker_aliases


def _retry[T](fn: Callable[[], T], attempts: int = 3, base_delay: float = 1.5) -> T:
    """Run fn, retrying on Yahoo's 429 with exponential backoff (1.5s, 3s).

    Hosted deploys hit Yahoo from datacenter IPs, so transient rate limits
    are routine; a short backoff usually clears them. The final attempt re-raises so
    callers (the app-level guard) can degrade gracefully.
    """
    for i in range(attempts - 1):
        try:
            return fn()
        except YFRateLimitError:
            # How often the host is throttled — and whether the backoff clears
            # it — is the difference between "Yahoo is flaky today" and "this
            # deploy's egress IP is burnt". Neither is visible from the UI.
            obs.warn("yahoo.rate_limited", attempt=i + 1, attempts=attempts)
            time.sleep(base_delay * 2**i)
    try:
        return fn()
    except YFRateLimitError:
        obs.warn("yahoo.rate_limit_exhausted", attempts=attempts)
        raise


def resolve(ticker: str) -> str:
    """Yahoo Finance symbol for a ticker, mapping broker codes via
    watchlist.yaml `aliases` (identity when unmapped)."""
    return ticker_aliases().get(ticker.upper(), ticker)


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV history for one ticker."""
    df = _retry(
        lambda: yf.Ticker(resolve(ticker)).history(period=period, interval=interval)
    )
    df.index.name = "Date"
    return df


def fetch_many(
    tickers: list[str], period: str = "1y", interval: str = "1d"
) -> dict[str, pd.DataFrame]:
    """OHLCV history for many tickers in ONE bulk request (yf.download).

    Tickers with no data are absent from the result. Results are keyed by the
    ticker as requested (broker code), not the resolved Yahoo symbol. This is
    the shared bulk path for the updater, portfolio analytics and the
    dashboard picker.
    """
    if not tickers:
        return {}
    symbol_of = {t: resolve(t) for t in tickers}
    symbols = list(dict.fromkeys(symbol_of.values()))
    data = _retry(
        lambda: yf.download(
            symbols,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    )
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            # yf.download keeps the ticker column level only for >1 symbol.
            df = data[symbol_of[t]] if len(symbols) > 1 else data
        except KeyError:
            continue
        df = df.dropna(how="all")
        if not df.empty:
            df.index.name = "Date"
            out[t] = df
    return out


def latest_price(ticker: str) -> float:
    """Most recent price — fast_info first, 5d history as fallback."""
    t = yf.Ticker(resolve(ticker))
    try:
        price = t.fast_info["lastPrice"]
        if price:
            return float(price)
    except Exception:
        pass
    df = _retry(lambda: t.history(period="5d", interval="1d"))
    if df.empty:
        raise ValueError(f"no data for {ticker}")
    return float(df["Close"].iloc[-1])


def cache_path(ticker: str) -> Path:
    return DATA_DIR / f"{ticker.upper()}.csv"


def save_history(ticker: str, df: pd.DataFrame) -> Path:
    path = cache_path(ticker)
    df.to_csv(path)
    return path


def load_cached(ticker: str) -> pd.DataFrame | None:
    path = cache_path(ticker)
    if not path.exists():
        return None
    return pd.read_csv(path, index_col="Date", parse_dates=True)
