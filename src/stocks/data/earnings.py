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

from stocks import obs
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
    # NaT is pandas' missing-date singleton, not a Timestamp — this both
    # screens it and is the narrowing a type checker can follow.
    if not isinstance(ts, pd.Timestamp):
        return None
    return ts.date()


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
        # Filter on the same field the sort reads: `days_until` is None
        # exactly when `date` is, and keeping them in step is what makes the
        # sort key total.
        events = [e for e in events if e.days_until is not None]
    return sorted(events, key=lambda e: e.days_until or 0)


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

    from stocks.data.crypto import is_crypto
    from stocks.data.funds import is_fund

    # Coins and funds never report. Screened here, the one choke point every
    # caller goes through (the calendar, the digest, the price chart's event
    # markers), so a watchlist holding an ETF stops spending a round trip per
    # refresh on Yahoo answering "no earnings dates found". Classification is
    # cache-only: a fund the cache hasn't met yet costs one empty lookup, not
    # a blocking `.info` inside a thread pool.
    if is_crypto(ticker) or is_fund(ticker, fetch=False):
        return [], []

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
    with obs.swallow("earnings.calendar", ticker=ticker):
        cal = t.calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is not None:
            for d in raw if isinstance(raw, (list, tuple)) else [raw]:
                if (dd := _to_date(d)) is not None:
                    found.add(dd)
    with obs.swallow("earnings.dates", ticker=ticker):
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


# ------------------------------------------------------------ quarter figures
# The result dialog wants more than EPS: what revenue printed, how the margins
# moved, what GAAP net income was. yfinance carries that on the quarterly
# income statement — a different payload than get_earnings_dates, keyed by
# fiscal *quarter end* instead of report date, hence match_quarter() below.
# Row labels vary by filer, so each field lists its aliases in priority order.
QUARTER_ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "pretax_income": ("Pretax Income",),
    "tax_provision": ("Tax Provision",),
    "rnd": ("Research And Development",),
    "opex": ("Operating Expense",),
    "diluted_eps": ("Diluted EPS",),
    "diluted_shares": ("Diluted Average Shares",),
}

# A print lands weeks after the quarter it reports on — the 10-Q deadline is
# 40-45 days out, and slow foreign filers stretch to ~10 weeks. The upper bound
# stays under a full quarter (~91 days) on purpose: any wider and the PREVIOUS
# quarter could match a print whose own quarter yfinance hasn't published yet,
# putting stale figures under the right headline.
MIN_REPORT_LAG = 5
MAX_REPORT_LAG = 80

# Same fiscal quarter, one year back: 365 days ± a fiscal-calendar week or two.
YOY_TOLERANCE = 45


@dataclass(frozen=True)
class Quarter:
    """One fiscal quarter's income-statement slice (GAAP, as filed).

    Every field is optional: yfinance drops rows a filer doesn't report, and
    the margins degrade to None rather than guessing a denominator.
    """

    end: date
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    pretax_income: float | None = None
    tax_provision: float | None = None
    rnd: float | None = None
    opex: float | None = None
    diluted_eps: float | None = None
    diluted_shares: float | None = None

    def _over_revenue(self, value: float | None) -> float | None:
        if value is None or not self.revenue:
            return None
        return value / self.revenue

    @property
    def gross_margin(self) -> float | None:
        return self._over_revenue(self.gross_profit)

    @property
    def operating_margin(self) -> float | None:
        return self._over_revenue(self.operating_income)

    @property
    def net_margin(self) -> float | None:
        return self._over_revenue(self.net_income)

    @property
    def rnd_intensity(self) -> float | None:
        return self._over_revenue(self.rnd)

    @property
    def tax_rate(self) -> float | None:
        """Effective tax rate: provision / pretax income."""
        if self.tax_provision is None or not self.pretax_income:
            return None
        return self.tax_provision / self.pretax_income


def _cell(frame: pd.DataFrame, label: str, col) -> float | None:
    """Scalar at (label, col), tolerating duplicate index labels."""
    if label not in frame.index:
        return None
    value = frame.loc[label, col]
    if isinstance(value, pd.Series):
        value = value.dropna()
        if value.empty:
            return None
        value = value.iloc[0]
    return _to_float(value)


def quarters(income_q: pd.DataFrame) -> list[Quarter]:
    """Quarterly income statement to Quarters, newest-first. Pure."""
    if income_q is None or income_q.empty:
        return []
    out: list[Quarter] = []
    for col in income_q.columns:
        end = _to_date(col)
        if end is None:
            continue
        fields = {
            field: next(
                (
                    v
                    for label in labels
                    if (v := _cell(income_q, label, col)) is not None
                ),
                None,
            )
            for field, labels in QUARTER_ROWS.items()
        }
        # An all-empty column (yfinance pads the frame out) carries nothing.
        if fields["revenue"] is None and fields["net_income"] is None:
            continue
        out.append(Quarter(end=end, **fields))
    return sorted(out, key=lambda q: q.end, reverse=True)


def match_quarter(quarters_: list[Quarter], report: date) -> Quarter | None:
    """The quarter a print on `report` reported on; None when not published yet.

    Picks the shortest plausible lag so a newly filed quarter wins over the one
    before it. Pure.
    """
    dated = [
        (lag, q)
        for q in quarters_
        if MIN_REPORT_LAG <= (lag := (report - q.end).days) <= MAX_REPORT_LAG
    ]
    return min(dated, key=lambda pair: pair[0])[1] if dated else None


def year_ago(quarters_: list[Quarter], q: Quarter) -> Quarter | None:
    """The same fiscal quarter one year before `q`; None when out of history."""
    target = q.end - timedelta(days=365)
    near = [
        (abs((x.end - target).days), x)
        for x in quarters_
        if x.end != q.end and abs((x.end - target).days) <= YOY_TOLERANCE
    ]
    return min(near, key=lambda pair: pair[0])[1] if near else None


def prior_quarter(quarters_: list[Quarter], q: Quarter) -> Quarter | None:
    """The quarter immediately before `q`; None when out of history."""
    earlier = [x for x in quarters_ if x.end < q.end]
    return max(earlier, key=lambda x: x.end) if earlier else None


def pct_change(current: float | None, previous: float | None) -> float | None:
    """Fractional change current/previous - 1; None when it can't be computed.

    Guards a negative or zero base, where the percentage is meaningless (a swing
    from -1bn to +2bn is not "-300% growth").
    """
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1


def fetch_quarters(ticker: str) -> list[Quarter]:
    """Quarterly income statement for one ticker, newest-first (network).

    Empty on any failure except a rate limit, which re-raises so the web layer
    can say why the section is empty (same contract as fetch_earnings).
    """
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    try:
        frame = yf.Ticker(resolve(ticker)).quarterly_income_stmt
    except YFRateLimitError:
        raise
    except Exception:
        return []
    return quarters(frame)


def fetch_statement_currency(ticker: str) -> str | None:
    """Currency the income statement is filed in ('financialCurrency').

    The statement frames carry no currency of their own, and an ADR files in
    its local currency while its estimates are quoted per USD ADS — so the
    figures can only be labeled from the snapshot.
    """
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    try:
        return (yf.Ticker(resolve(ticker)).info or {}).get("financialCurrency")
    except YFRateLimitError:
        raise
    except Exception:
        return None
