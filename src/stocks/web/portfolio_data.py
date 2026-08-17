"""Shared cached loaders for ledger-derived data (Home + Portfolio pages).

st.cache_data entries key on the function's module + name: defining the same
loader in two page modules would double every fetch and price burst. Both
pages import these instead. Every function is keyed by (db, mtime) —
`db` isolates concurrent users, `mtime` (widgets.db_mtime) invalidates
exactly when the ledger file changes; live prices refresh via ttl.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from stocks.analysis.portfolio import (
    flow_series,
    injected_vs_value,
    load_closes,
    market_live,
    position_values_history,
    positions_frame_eur,
    session_moves,
    time_weighted_returns,
)
from stocks.portfolio.ledger import all_transactions
from stocks.portfolio.positions import build


@st.cache_data(show_spinner=False, max_entries=32)
def ledger_state(db: str, mtime: float):
    """Ledger + FIFO replay -> (transactions, open positions, realized sales).

    No ttl: `mtime` keys the cache, so this stays hot until the next import
    instead of expiring on a timer."""
    txs = all_transactions(Path(db))
    positions, realized = build(txs) if txs else ([], [])
    return txs, positions, realized


@st.cache_data(ttl=300, show_spinner="Loading live prices…")
def positions_table(db: str, mtime: float) -> pd.DataFrame:
    """Live-priced EUR positions table (one concurrent price burst via
    market_values_eur), cached so Home, plain reruns and the Realized & tax
    tab reuse it instead of refetching every position serially."""
    return positions_frame_eur(ledger_state(db, mtime)[1])


@st.cache_data(ttl=900, show_spinner="Loading recent prices…")
def basket_history(db: str, mtime: float) -> pd.DataFrame:
    """Fixed-basket daily EUR values (3mo of closes × ECB FX at today's
    quantities) — feeds the day/week/month chips and per-ticker day change."""
    return position_values_history(ledger_state(db, mtime)[1], period="3mo")


@st.cache_data(ttl=300, show_spinner="Loading last session moves…")
def last_session_moves(tickers: tuple[str, ...]) -> dict[str, float]:
    """Cached last regular-session % move per ticker (fast_info burst).

    Only fetched for off-session names — when the market's closed the daily
    close-to-close basket can collapse to ~0% (a stale/flat premarket bar), so
    the day-change cells read from this instead. Keyed by the ticker tuple; ttl
    refreshes it around the next open."""
    return session_moves(list(tickers))


def enriched_positions(db: str, mtime: float) -> pd.DataFrame:
    """positions_table plus weight / day-change columns, sorted by weight.

    weight = share of live EUR market value; day_eur/day_pct come from the
    basket history's last two closes (includes the FX move). Cheap frame math
    over two cached loads — not cached itself.
    """
    tbl = positions_table(db, mtime)
    if tbl.empty:
        return tbl
    value = tbl["value_eur"].dropna().sum()
    tbl["weight"] = tbl["value_eur"] / value if value else float("nan")
    vals = basket_history(db, mtime)
    if len(vals) >= 2:
        last, prev = vals.iloc[-1], vals.iloc[-2]
        tbl["day_eur"] = (last - prev).reindex(tbl.index)
        tbl["day_pct"] = (last / prev - 1).reindex(tbl.index)
    else:
        tbl["day_eur"] = tbl["day_pct"] = float("nan")
    # Market closed → the close-to-close basket can be a flat premarket 0%.
    # Override those rows with fast_info's last regular-session move (native)
    # so the (dimmed) day cell shows the real last close; day_eur re-derives
    # from the EUR value. Crypto is 24/7 so it never overrides.
    off = tuple(t for t in tbl.index if not market_live(t))
    if off:
        moves = last_session_moves(off)
        for t, pct in moves.items():
            if t in tbl.index:
                tbl.at[t, "day_pct"] = pct
                val = tbl.at[t, "value_eur"]
                tbl.at[t, "day_eur"] = (
                    val * pct / (1 + pct)
                    if pd.notna(val) and pct != -1
                    else float("nan")
                )
    return tbl.sort_values("weight", ascending=False, na_position="last")


@st.cache_data(ttl=3600, show_spinner="Building ledger history…")
def ledger_history(fingerprint: tuple, db: str):
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
        ccy: pd.Series(rates_range(first, date.today().isoformat(), ccy, "EUR"))
        for ccy in {t.currency for t in ledger if t.action in ("buy", "sell")}
        if ccy != "EUR"
    }
    hist = injected_vs_value(ledger, closes, fx)
    if hist.empty:
        twr = pd.Series(dtype=float)
    else:
        # No ticker filter: unpriced names are carried at cost in value_eur,
        # so their buy/sell flows must offset those value jumps.
        twr = time_weighted_returns(hist["value_eur"], flow_series(ledger))
    missing = sorted(
        {t for t in tickers if t not in closes}
        | set(hist.attrs.get("carried_at_cost", []) if not hist.empty else [])
    )
    return hist, twr, missing


@st.cache_data(ttl=3600, show_spinner=False)
def eur_spot(quote: str) -> float | None:
    """Latest EUR→quote rate for the display-currency preference."""
    try:
        from stocks.data.fx import rate_on

        return float(rate_on(date.today(), "EUR", quote))
    except Exception:
        return None  # FX down → headline falls back to EUR
