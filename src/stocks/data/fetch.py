"""Fetch OHLCV price data via yfinance; cache to CSV under data/."""

from __future__ import annotations

import threading
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


# `.info` is the heaviest call yfinance makes — a full quoteSummary — and it
# used to be fetched independently by everything that wanted one field of it:
# the allocation profile (sector/country/currency), the session quote
# (pre/post prices), the fund classifier (quoteType), the logo resolver
# (website) and the earnings currency. One ticker on one page render could
# pay for it three to five times over, and that pile of requests is what
# trips Yahoo's rate limiter from a datacenter IP.
#
# The TTL is deliberately short. The same blob carries both facts that never
# move (sector, country) and facts that move by the second (premarket price),
# so it is pinned to the faster of the two — the point is to collapse the
# several calls *within one render*, not to hold quotes. The web layer's own
# st.cache_data wrappers still bound how often a render happens at all.
_INFO_TTL_S = 120.0
_info_memo: dict[str, tuple[float, dict]] = {}
_info_lock = threading.Lock()


def clear_info_cache() -> None:
    """Forget every memoized `.info` blob.

    For tests that swap yfinance out between assertions, and for anything that
    wants the next read to go to the network — a memo hit doesn't see a
    patched `yfinance.Ticker`, and within the TTL it doesn't see a new quote.
    """
    with _info_lock:
        _info_memo.clear()


def info(ticker: str) -> dict:
    """Yahoo's quote/profile blob for `ticker` (yfinance `.info`), memoized.

    Returns {} when Yahoo has nothing (or hands back a non-dict). Errors are
    not memoized and propagate — callers decide whether a missing profile is
    fatal (`data.earnings` re-raises a rate limit) or cosmetic (`data.logo`
    swallows everything). Safe to call from a thread pool: entries are shared
    under a lock, so a `load_meta` fan-out over one ticker fetches once.
    """
    key = resolve(ticker)
    now = time.monotonic()
    with _info_lock:
        hit = _info_memo.get(key)
        if hit is not None and now - hit[0] < _INFO_TTL_S:
            return hit[1]
    fetched = yf.Ticker(key).info
    blob = fetched if isinstance(fetched, dict) else {}
    with _info_lock:
        _info_memo[key] = (time.monotonic(), blob)
        # Bounded: a long-lived server would otherwise accumulate an entry per
        # symbol anyone ever looked at. Evicting the expired ones is enough —
        # the live set is one page's worth of tickers.
        if len(_info_memo) > 512:
            cutoff = time.monotonic() - _INFO_TTL_S
            for stale in [k for k, (at, _) in _info_memo.items() if at < cutoff]:
                del _info_memo[stale]
    return blob


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV history for one ticker."""
    df = _retry(
        lambda: yf.Ticker(resolve(ticker)).history(period=period, interval=interval)
    )
    df.index.name = "Date"
    return df


def fetch_many(
    tickers: list[str],
    period: str = "1y",
    interval: str = "1d",
    auto_adjust: bool = True,
) -> dict[str, pd.DataFrame]:
    """OHLCV history for many tickers in ONE bulk request (yf.download).

    Tickers with no data are absent from the result. Results are keyed by the
    ticker as requested (broker code), not the resolved Yahoo symbol. This is
    the shared bulk path for the updater, portfolio analytics and the
    dashboard picker. auto_adjust=False keeps dividend-unadjusted bars —
    needed when comparing against as-traded ledger prices (portfolio.fees).
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
            auto_adjust=auto_adjust,
            progress=False,
            threads=True,
        )
    )
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            # Strip the ticker column level when there is one. Older yfinance
            # only added it for multi-symbol downloads, so this used to key off
            # the symbol count; 1.5.x adds it for a single symbol too, and the
            # count test then handed the caller a frame whose columns were
            # ('AAPL', 'Close') — every `df["Close"]` downstream silently found
            # nothing, so a one-name watchlist or a single-position book read as
            # unpriced. Ask the frame what shape it is instead.
            df = (
                data[symbol_of[t]]
                if isinstance(data.columns, pd.MultiIndex)
                else data
            )
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
    with obs.swallow("yahoo.fast_info", ticker=ticker):
        price = t.fast_info["lastPrice"]
        if price:
            return float(price)
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
