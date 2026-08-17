"""Earnings calendar: next report date per ticker + upcoming-window reminders.

Aggressive growth names gap hard on prints, so the point is a heads-up N days
before earnings — plus the rear-view mirror: past prints with reported vs
estimated EPS, so the calendar shows how recent quarters landed. Date selection
(next_after / build_events) is pure and tested; only fetch_earnings /
price_reaction touch yfinance.
"""

from __future__ import annotations

import calendar as _calendar
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from stocks.config import Holding, load_watchlist
from stocks.data.fetch import resolve


@dataclass
class EarningsEvent:
    ticker: str
    date: date | None
    days_until: int | None = None


@dataclass
class EarningsResult:
    """A reported quarter: what the street expected vs what printed."""

    ticker: str
    date: date
    eps_estimate: float | None = None
    reported_eps: float | None = None
    surprise_pct: float | None = None

    @property
    def beat(self) -> bool | None:
        """True beat / False miss / None when there's nothing to compare."""
        if self.surprise_pct is not None:
            return self.surprise_pct >= 0
        if self.reported_eps is not None and self.eps_estimate is not None:
            return self.reported_eps >= self.eps_estimate
        return None


def _to_date(value) -> date | None:
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts.date()


def _to_float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def next_after(dates: list[date], ref: date) -> date | None:
    """Earliest date on or after `ref`; None if all are in the past."""
    future = [d for d in dates if d >= ref]
    return min(future) if future else None


def build_events(
    dated: dict[str, list[date]], ref: date, within_days: int | None = None
) -> list[EarningsEvent]:
    """Turn {ticker: [dates]} into upcoming events, sorted soonest-first.

    Pure: no network, no clock. `within_days` keeps only reports at most that
    many days out; None keeps every ticker with a known future date.
    """
    events: list[EarningsEvent] = []
    for ticker, dates in dated.items():
        nxt = next_after(dates, ref)
        days = (nxt - ref).days if nxt else None
        events.append(EarningsEvent(ticker, nxt, days))
    if within_days is not None:
        events = [
            e for e in events if e.days_until is not None and e.days_until <= within_days
        ]
    else:
        events = [e for e in events if e.date is not None]
    return sorted(events, key=lambda e: e.days_until)


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift a (year, month) by `delta` months, wrapping the year. Pure."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def month_weeks(year: int, month: int) -> list[list[date]]:
    """Weeks (Mon-first) of `date`s covering the month, incl. adjacent spill.

    Thin wrapper over stdlib calendar so the page can render a grid without
    reaching for the network. Each inner list is exactly 7 dates.
    """
    return _calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)


def group_by_date(events: Iterable) -> dict[date, list]:
    """Index events/results by their date (items with no date are dropped)."""
    grouped: dict[date, list] = defaultdict(list)
    for e in events:
        if e.date is not None:
            grouped[e.date].append(e)
    return dict(grouped)


def fetch_earnings(ticker: str) -> tuple[list[date], list[EarningsResult]]:
    """One yfinance pass: all known earnings dates + past reported results.

    get_earnings_dates carries EPS estimate / reported / surprise per row;
    rows with a reported figure become EarningsResults, every date (past and
    future, plus the forward-looking `calendar` entries) lands in the list.
    """
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    # A dead symbol failing in resolve()/Ticker() must not abort a whole
    # pool.map in upcoming()/calendar_events() — but rate limits re-raise so
    # the web app's banner still sees them.
    try:
        t = yf.Ticker(resolve(ticker))
    except YFRateLimitError:
        raise
    except Exception:
        return [], []
    found: set[date] = set()
    results: dict[date, EarningsResult] = {}
    try:
        cal = t.calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is not None:
            for d in raw if isinstance(raw, (list, tuple)) else [raw]:
                if (dd := _to_date(d)) is not None:
                    found.add(dd)
    except Exception:
        pass
    try:
        df = t.get_earnings_dates(limit=12)
        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                if (dd := _to_date(idx)) is None:
                    continue
                found.add(dd)
                reported = _to_float(row.get("Reported EPS"))
                if reported is not None:
                    results[dd] = EarningsResult(
                        ticker,
                        dd,
                        eps_estimate=_to_float(row.get("EPS Estimate")),
                        reported_eps=reported,
                        surprise_pct=_to_float(row.get("Surprise(%)")),
                    )
    except Exception:
        pass
    return sorted(found), sorted(results.values(), key=lambda r: r.date)


def earnings_dates(ticker: str) -> list[date]:
    """Known earnings dates (past + future) for one ticker, via yfinance."""
    return fetch_earnings(ticker)[0]


def price_reaction(ticker: str, event_date: date) -> float | None:
    """% move across the print: last close before the date vs first close after.

    Report timing (pre/post market) is unknown, so the window deliberately
    spans the date itself — a two-session move that always contains the gap.
    """
    import yfinance as yf

    try:
        hist = yf.Ticker(resolve(ticker)).history(
            start=event_date - timedelta(days=7),
            end=event_date + timedelta(days=7),
            interval="1d",
            auto_adjust=True,
        )
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    closes = {ts.date(): float(v) for ts, v in hist["Close"].dropna().items()}
    before = [d for d in closes if d < event_date]
    after = [d for d in closes if d > event_date]
    if not before or not after or not closes[max(before)]:
        return None
    return (closes[min(after)] / closes[max(before)] - 1) * 100


def upcoming(
    holdings: list[Holding] | None = None,
    within_days: int | None = 30,
    ref: date | None = None,
    max_workers: int = 8,
) -> list[EarningsEvent]:
    """Fetch earnings dates for the watchlist and return the upcoming window."""
    holdings = holdings if holdings is not None else load_watchlist()
    ref = ref or date.today()
    tickers = [h.ticker for h in holdings]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        dated = dict(zip(tickers, pool.map(earnings_dates, tickers), strict=True))
    return build_events(dated, ref, within_days)


def calendar_events(
    holdings: list[Holding] | None = None,
    ref: date | None = None,
    max_workers: int = 8,
) -> tuple[list[EarningsEvent], list[EarningsResult]]:
    """(upcoming events, past reported results) for the watchlist, one fetch.

    Same network cost as upcoming(): fetch_earnings already pulls the result
    columns, so past prints ride along for free. Results are strictly before
    `ref` (a same-day print still counts as upcoming) sorted newest-first.
    """
    holdings = holdings if holdings is not None else load_watchlist()
    ref = ref or date.today()
    tickers = [h.ticker for h in holdings]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fetched = list(pool.map(fetch_earnings, tickers))
    dated = {t: dates for t, (dates, _) in zip(tickers, fetched, strict=True)}
    results = [r for _, rs in fetched for r in rs if r.date < ref]
    events = build_events(dated, ref, within_days=None)
    return events, sorted(results, key=lambda r: r.date, reverse=True)
