"""Fetch OHLCV price data via yfinance; cache to CSV under data/."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf

from stocks.config import DATA_DIR, ticker_aliases


def resolve(ticker: str) -> str:
    """Yahoo Finance symbol for a ticker, mapping broker codes via
    watchlist.yaml `aliases` (identity when unmapped)."""
    return ticker_aliases().get(ticker.upper(), ticker)


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV history for one ticker."""
    df = yf.Ticker(resolve(ticker)).history(period=period, interval=interval)
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
    data = yf.download(
        symbols,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
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
    df = t.history(period="5d", interval="1d")
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
