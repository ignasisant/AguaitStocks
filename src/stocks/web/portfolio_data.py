"""Shared cached loaders for ledger-derived data (Home + Portfolio pages).

st.cache_data entries key on the function's module + name: defining the same
loader in two page modules would double every fetch and price burst. Both
pages import these instead. Every function is keyed by (db, mtime, base) —
`db` isolates concurrent users, `mtime` (widgets.db_mtime) invalidates exactly
when the ledger file changes, and `base` is the reporting currency: money is
computed *in* it (each leg at its own trade-date rate), not converted after the
fact, so two users on different currencies must not share an entry. Callers
pass `auth.reporting_currency()`, or a tax jurisdiction's own currency for the
tax replay.

Every loader is `show_spinner=False`: the call site owns the loading state and
paints a `stocks.web.skeletons` placeholder shaped like the block it is about
to fill, so a cold cache holds the layout instead of collapsing it behind a
spinner.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from stocks import obs
from stocks.analysis.portfolio import (
    flow_series,
    injected_vs_value,
    load_closes,
    market_live,
    position_values_history,
    positions_frame,
    session_moves,
    session_quote,
    time_weighted_returns,
)
from stocks.portfolio.custody import Custody, by_position
from stocks.portfolio.ledger import all_transactions
from stocks.portfolio.positions import build


@st.cache_data(show_spinner=False, max_entries=32)
def ledger_state(
    db: str, mtime: float, base: str = "EUR", matching: str = "fifo"
):
    """Ledger + share-matching replay -> (transactions, positions, sales).

    Lots are valued in `base` at each transaction date's rate — a cost basis is
    a per-transaction conversion, so a USD book cannot be recovered by
    converting an EUR one at the end. `matching` is the jurisdiction's
    share-identification rule: the app's analytics use FIFO, a UK tax replay
    uses the s.104 pool, and the two produce different realized parcels from
    the same trades — hence both in the cache key. No ttl: `mtime` keys the
    cache, so this stays hot until the next import instead of expiring."""
    txs = all_transactions(Path(db))
    positions, realized = (
        build(txs, base=base, matching=matching) if txs else ([], [])
    )
    return txs, positions, realized


@st.cache_data(show_spinner=False, max_entries=32)
def custody_map(
    db: str, mtime: float, base: str = "EUR"
) -> dict[str, dict[str, Custody]]:
    """ticker -> broker -> open shares/cost: which broker's account each
    holding sits in (stocks.portfolio.custody).

    Ledger-only, so no ttl — `mtime` keys it like ledger_state and it stays
    hot until the next import.
    """
    return by_position(ledger_state(db, mtime, base)[0], base=base)


@st.cache_data(ttl=300, show_spinner=False)
def positions_table(db: str, mtime: float, base: str = "EUR") -> pd.DataFrame:
    """Live-priced positions table in `base` (one concurrent price burst via
    market_values), cached so Home, plain reruns and the Realized & tax
    tab reuse it instead of refetching every position serially."""
    return positions_frame(ledger_state(db, mtime, base)[1], base=base)


@st.cache_data(ttl=900, show_spinner=False)
def basket_history(db: str, mtime: float, base: str = "EUR") -> pd.DataFrame:
    """Fixed-basket daily values in `base` (3mo of closes × daily ECB FX at
    today's quantities) — feeds the day/week/month chips and per-ticker day
    change, so a position's move includes its FX move."""
    return position_values_history(
        ledger_state(db, mtime, base)[1], period="3mo", base=base
    )


@st.cache_data(ttl=300, show_spinner=False)
def last_session_moves(tickers: tuple[str, ...]) -> dict[str, float]:
    """Cached day % move per ticker (quote burst), extended hours included.

    Only fetched for names outside their regular session — the daily close-to-
    close basket can collapse to ~0% there (a stale/flat premarket bar), so the
    day-change cells read from this instead: the live pre/after-hours move while
    Yahoo quotes one, the last completed session once those windows shut. Keyed
    by the ticker tuple; ttl refreshes it around the next open."""
    return session_moves(list(tickers))


@st.cache_data(ttl=120, show_spinner=False)
def last_session_quote(ticker: str) -> dict | None:
    """Cached one-ticker quote snapshot: price, day % move, pre/post label.

    The ticker page's price hero reads this off-session — the daily closes miss
    the extended-hours move entirely, so a -13% premarket gap would render as
    yesterday's session on both the price and the delta. Short ttl: premarket
    prices move fast and this is a single request."""
    return session_quote(ticker)


def enriched_positions(db: str, mtime: float, base: str = "EUR") -> pd.DataFrame:
    """positions_table plus weight / day-change columns, sorted by weight.

    weight = share of live market value; day/day_pct come from the
    basket history's last two closes (includes the FX move). Cheap frame math
    over two cached loads — not cached itself.
    """
    tbl = positions_table(db, mtime, base)
    if tbl.empty:
        return tbl
    value = tbl["value"].dropna().sum()
    tbl["weight"] = tbl["value"] / value if value else float("nan")
    vals = basket_history(db, mtime, base)
    if len(vals) >= 2:
        last, prev = vals.iloc[-1], vals.iloc[-2]
        tbl["day"] = (last - prev).reindex(tbl.index)
        tbl["day_pct"] = (last / prev - 1).reindex(tbl.index)
    else:
        tbl["day"] = tbl["day_pct"] = float("nan")
    # Outside the regular session the close-to-close basket can be a flat
    # premarket 0%. Override those rows with the quote move (native): the live
    # pre/after-hours move, else the last completed session; day re-derives
    # from the EUR value. Crypto is 24/7 so it never overrides.
    off = tuple(t for t in tbl.index if not market_live(t))
    if off:
        moves = last_session_moves(off)
        for t, pct in moves.items():
            if t in tbl.index:
                tbl.at[t, "day_pct"] = pct
                val = tbl.at[t, "value"]
                tbl.at[t, "day"] = (
                    val * pct / (1 + pct)
                    if pd.notna(val) and pct != -1
                    else float("nan")
                )
    return tbl.sort_values("weight", ascending=False, na_position="last")


@st.cache_data(ttl=3600, show_spinner=False)
def ledger_history(fingerprint: tuple, db: str, base: str = "EUR"):
    """Full-span daily history from the ledger: injected vs value, TWR, missing.

    One fetch shared by Home's glance chart and the Portfolio page's Positions
    and Allocation & risk tabs. TWR = daily time-weighted returns of the book
    (flow-adjusted), so deposits/withdrawals don't read as performance and it's
    comparable against benchmarks. `fingerprint` is the cache key the callers
    build as (len(txs), txs[-1].date, date.today()) — new transactions and day
    rollovers invalidate it; ttl refreshes intraday prices.
    """
    from stocks.data.fx import rates_range

    ledger = all_transactions(Path(db))
    tickers = sorted({t.ticker for t in ledger if t.action in ("buy", "sell")})
    first = min(t.date for t in ledger)
    span = (date.today() - date.fromisoformat(first)).days
    period = "2y" if span <= 700 else "5y" if span <= 1780 else "max"
    closes = load_closes(tickers, period=period)
    fx = {
        ccy: pd.Series(rates_range(first, date.today().isoformat(), ccy, base))
        for ccy in {t.currency for t in ledger if t.action in ("buy", "sell")}
        if ccy != base
    }
    hist = injected_vs_value(ledger, closes, fx, base=base)
    if hist.empty:
        twr = pd.Series(dtype=float)
    else:
        # No ticker filter: unpriced names are carried at cost in value,
        # so their buy/sell flows must offset those value jumps.
        twr = time_weighted_returns(
            hist["value"], flow_series(ledger, base=base)
        )
    missing = sorted(
        {t for t in tickers if t not in closes}
        | set(hist.attrs.get("carried_at_cost", []) if not hist.empty else [])
    )
    return hist, twr, missing


@st.cache_data(ttl=86400, show_spinner=False, max_entries=8)
def trade_bars(db: str, mtime: float) -> dict[str, pd.DataFrame]:
    """Daily UNADJUSTED OHLC per traded ticker, spanning the whole ledger.

    Feeds the Fees tab's spread estimate: execution prices are as-traded, so
    the reference bars must not be dividend-adjusted (auto_adjust=False —
    splits are replayed from the ledger in portfolio.fees). Historical bars
    never change, hence the day-long ttl; `mtime` refetches after an import
    (new tickers / older first trade may widen the span)."""
    from stocks.data.fetch import fetch_many

    txs = ledger_state(db, mtime)[0]  # bars are native-currency: base is moot
    trades = [t for t in txs if t.action in ("buy", "sell")]
    if not trades:
        return {}
    tickers = sorted({t.ticker for t in trades})
    first = min(t.date for t in trades)
    span = (date.today() - date.fromisoformat(first)).days
    period = "2y" if span <= 700 else "5y" if span <= 1780 else "max"
    return fetch_many(tickers, period=period, auto_adjust=False)


@st.cache_data(ttl=3600, show_spinner=False)
def eur_spot(quote: str, base: str = "EUR") -> float | None:
    """Latest `base`→quote spot rate, for a threshold or a one-off conversion.

    Money the pages *report* is computed in the reporting currency per date, so
    this is for the few places a single live rate is the right tool: a foreign-
    asset reporting threshold, or a figure already summed in another base."""
    try:
        from stocks.data.fx import rate_on

        return float(rate_on(date.today(), base, quote))
    except Exception:
        return None  # FX down → caller keeps the figure it has


@st.cache_data(ttl=900, show_spinner=False)
def native_base_rates(ccys: tuple[str, ...], base: str = "EUR") -> dict[str, float]:
    """{currency: native->`base` spot} for the currencies a book trades in.

    positions_frame multiplies each native price by this rate, so dividing a
    reported figure back by it recovers the quote-currency number a ticker is
    actually priced in. Pairs whose lookup fails are absent (caller falls back
    to "n/a" rather than printing a converted figure under a foreign
    symbol)."""
    from stocks.data.fx import spot

    out: dict[str, float] = {}
    for c in ccys:
        with obs.swallow("fx.spot", ccy=c, base=base):
            out[c] = float(spot(c, base)[0])
    return out


@st.cache_data(ttl=900, show_spinner=False)
def recent_closes(tickers: tuple[str, ...]) -> dict[str, list[float]]:
    """Last two daily closes per ticker (prev, last).

    One bulk download (data.fetch.fetch_many) for the whole watchlist, cached
    15 min so the ticker list renders without hammering the network on every
    rerun. The cache key is the ticker tuple, so every page that shows the
    page shares one download.
    """
    from stocks.data.fetch import fetch_many

    out: dict[str, list[float]] = {}
    for t, df in fetch_many(list(tickers), period="5d").items():
        close = df["Close"].dropna() if "Close" in df else None
        if close is not None and len(close):
            out[t] = [float(v) for v in close.iloc[-2:]]
    return out


def db_mtime(db: str) -> float:
    """Ledger file mtime — a cache key that changes only when the book does,
    so ledger-derived caches stay hot until the next import instead of
    expiring on a timer. 0.0 when the file doesn't exist yet."""
    try:
        return Path(db).stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False, max_entries=64)
def held_tickers(db: str, mtime: float) -> list[str]:
    """Tickers with an open position in the ledger — reachable in search even
    when they're not on the watchlist, so imported activity is browsable.
    `db` is the session user's ledger path; it keys the cache so concurrent
    users never see each other's positions. `mtime` (db_mtime) invalidates
    the entry exactly when the ledger file changes."""
    try:
        from stocks.portfolio.ledger import all_transactions
        from stocks.portfolio.positions import build

        # Identity converter: quantities don't need FX, keeps this offline.
        positions, _ = build(all_transactions(Path(db)), to_base=lambda a, c, d: a)
        return [p.ticker for p in positions]
    except Exception:
        return []  # empty/inconsistent ledger must never break search
