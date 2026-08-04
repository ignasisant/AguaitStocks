"""TradingView technical-analysis consensus — the one read our own indicators
can't give us.

We already compute RSI, SMA crosses, drawdown and 52-week highs from cached
price history (stocks.analysis.indicators, stocks.notify.alerts), so this module
deliberately does NOT re-expose those. It adds only what we lack:

  * the aggregated BUY / NEUTRAL / SELL consensus across TradingView's ~26
    oscillator + moving-average signals (`consensus`),
  * that same read across MULTIPLE timeframes in one shot, intraday .. weekly
    (`consensus_multi`),
  * INTRADAY resolution (1m / 5m / 1h …) our daily yfinance cache never sees.

This is a *technical* signal, not valuation. The toolkit is fundamentals-first
(EDGAR primary); treat a TradingView rating as sentiment colour, never a thesis.

Backed by the unofficial `tradingview-ta` library, which scrapes an undocumented
endpoint. It is an OPTIONAL dependency (``pip install 'stocks[tv]'``): may break
without notice, and TradingView's terms restrict redistribution — keep it to
personal use. Every public function degrades to empty / None when the library is
absent or a call fails, matching the "no key -> empty" contract of stocks.data.fmp.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from stocks.config import tv_symbols

DEFAULT_SCREENER = "america"

# Common US venues probed, in order, when a ticker has no explicit `tv` mapping
# in watchlist.yaml. Non-US names MUST be mapped (exchange is unguessable).
US_CANDIDATES = (("NASDAQ", "america"), ("NYSE", "america"), ("AMEX", "america"))

# Our interval label -> tradingview_ta.Interval attribute name.
INTERVALS = {
    "1m": "INTERVAL_1_MINUTE",
    "5m": "INTERVAL_5_MINUTES",
    "15m": "INTERVAL_15_MINUTES",
    "30m": "INTERVAL_30_MINUTES",
    "1h": "INTERVAL_1_HOUR",
    "2h": "INTERVAL_2_HOURS",
    "4h": "INTERVAL_4_HOURS",
    "1d": "INTERVAL_1_DAY",
    "1W": "INTERVAL_1_WEEK",
    "1M": "INTERVAL_1_MONTH",
}
INTRADAY_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h", "2h", "4h"})
# Sensible multi-timeframe sweep: intraday -> swing -> position.
DEFAULT_TIMEFRAMES = ("1h", "1d", "1W")


@dataclass(frozen=True)
class Symbol:
    """A fully-resolved TradingView venue for one ticker."""

    symbol: str
    exchange: str
    screener: str


@dataclass(frozen=True)
class Consensus:
    """TradingView's aggregated recommendation for one ticker at one interval.

    `recommendation` is the overall verdict (STRONG_BUY / BUY / NEUTRAL / SELL /
    STRONG_SELL); `ma` and `osc` are the moving-average and oscillator sub-reads.
    `buy` / `sell` / `neutral` count the individual signals behind the overall.
    """

    ticker: str
    interval: str
    recommendation: str
    buy: int
    sell: int
    neutral: int
    ma: str = ""
    osc: str = ""

    @property
    def total(self) -> int:
        return self.buy + self.sell + self.neutral

    @property
    def score(self) -> float:
        """Net signal in [-1, 1]: (buy - sell) / total; 0 when no signals.

        Sortable, so a watchlist can be ranked by technical posture the same way
        the fundamentals screener ranks by KPIs.
        """
        return (self.buy - self.sell) / self.total if self.total else 0.0


def _parse_spec(spec: str) -> Symbol | None:
    """``EXCHANGE:SYMBOL[@screener]`` -> Symbol; None if no exchange is given."""
    body, _, screener = spec.partition("@")
    exchange, sep, symbol = body.partition(":")
    if not sep:
        return None
    return Symbol(
        symbol=symbol.strip().upper(),
        exchange=exchange.strip().upper(),
        screener=(screener.strip() or DEFAULT_SCREENER),
    )


def candidates(ticker: str) -> list[Symbol]:
    """Venues to try for `ticker`, best first.

    An explicit watchlist.yaml `tv` mapping yields exactly one venue; otherwise
    we probe the common US exchanges. This is where the "symbol mapping" gap is
    absorbed: map the handful of non-US names once, let US names auto-probe.
    """
    spec = tv_symbols().get(ticker.upper())
    if spec:
        mapped = _parse_spec(spec)
        if mapped is not None:
            return [mapped]
    return [Symbol(ticker.upper(), exch, scr) for exch, scr in US_CANDIDATES]


def _analyze(sym: Symbol, interval: str):
    """Call the unofficial library for one (venue, interval). Raises on any
    failure, including the library being absent — callers treat that as a miss.

    Isolated so tests monkeypatch here and never touch the network.
    """
    from tradingview_ta import Interval, TA_Handler

    handler = TA_Handler(
        symbol=sym.symbol,
        exchange=sym.exchange,
        screener=sym.screener,
        interval=getattr(Interval, INTERVALS[interval]),
    )
    return handler.get_analysis()


def _build(ticker: str, interval: str, analysis) -> Consensus:
    """Pure adapter: a tradingview_ta Analysis -> our Consensus."""
    summary = analysis.summary or {}
    ma = getattr(analysis, "moving_averages", None) or {}
    osc = getattr(analysis, "oscillators", None) or {}
    return Consensus(
        ticker=ticker.upper(),
        interval=interval,
        recommendation=str(summary.get("RECOMMENDATION", "")).upper(),
        buy=int(summary.get("BUY", 0) or 0),
        sell=int(summary.get("SELL", 0) or 0),
        neutral=int(summary.get("NEUTRAL", 0) or 0),
        ma=str(ma.get("RECOMMENDATION", "")).upper(),
        osc=str(osc.get("RECOMMENDATION", "")).upper(),
    )


def _resolve(ticker: str, interval: str) -> tuple[Symbol, object] | None:
    """First candidate venue that returns an analysis at `interval`, with that
    analysis. None if the library is missing or no venue responds."""
    for sym in candidates(ticker):
        try:
            analysis = _analyze(sym, interval)
        except Exception:
            continue
        if analysis is not None:
            return sym, analysis
    return None


def consensus(ticker: str, interval: str = "1d") -> Consensus | None:
    """Aggregated TradingView recommendation for one ticker. None on any miss."""
    if interval not in INTERVALS:
        raise ValueError(f"unknown interval: {interval!r} (see INTERVALS)")
    resolved = _resolve(ticker, interval)
    if resolved is None:
        return None
    _, analysis = resolved
    return _build(ticker, interval, analysis)


def consensus_multi(
    ticker: str, intervals: tuple[str, ...] = DEFAULT_TIMEFRAMES
) -> dict[str, Consensus]:
    """Consensus across several timeframes, keyed by interval label.

    Resolves the venue once (probing is the expensive part) then reuses it for
    every interval, so a multi-timeframe read costs one probe, not one per frame.
    Intervals that error are simply absent from the result.
    """
    if not intervals:
        return {}
    bad = [iv for iv in intervals if iv not in INTERVALS]
    if bad:
        raise ValueError(f"unknown interval(s): {bad} (see INTERVALS)")

    resolved = _resolve(ticker, intervals[0])
    if resolved is None:
        return {}
    sym, first = resolved
    out = {intervals[0]: _build(ticker, intervals[0], first)}
    for iv in intervals[1:]:
        try:
            analysis = _analyze(sym, iv)
        except Exception:
            continue
        if analysis is not None:
            out[iv] = _build(ticker, iv, analysis)
    return out


def consensus_many(
    tickers: list[str], interval: str = "1d", max_workers: int = 8
) -> dict[str, Consensus]:
    """Consensus for many tickers concurrently (network-bound). Misses dropped."""
    if not tickers:
        return {}

    def one(t: str) -> tuple[str, Consensus | None]:
        return t, consensus(t, interval)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(one, tickers)
    return {t: c for t, c in results if c is not None}
