"""Fetch OHLCV price data via yfinance; cache to CSV under data/."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf

from stocks.config import DATA_DIR


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV history for one ticker."""
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    df.index.name = "Date"
    return df


def fetch_many(
    tickers: list[str], period: str = "1y", interval: str = "1d"
) -> dict[str, pd.DataFrame]:
    return {t: fetch_history(t, period, interval) for t in tickers}


def latest_price(ticker: str) -> float:
    """Most recent close price."""
    df = fetch_history(ticker, period="5d", interval="1d")
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
