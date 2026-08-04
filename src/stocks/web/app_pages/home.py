"""Home page — the portfolio at a glance plus the watchlist groups.

The landing view: open positions with their daily move (a slim cut of the
Portfolio page), then the watchlist split into favorites / tag groups / the
rest, each row carrying the day's change. Every ticker cell links to the
Ticker page; the full ledger analytics stay on Portfolio.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from stocks.analysis.portfolio import basket_change
from stocks.config import Holding, load_watchlist
from stocks.data.crypto import is_crypto
from stocks.data.earnings import upcoming
from stocks.portfolio.ledger import all_transactions
from stocks.web import auth
from stocks.web.portfolio_data import (
    basket_history,
    enriched_positions,
    eur_spot,
    ledger_state,
    positions_table,
)
from stocks.web.widgets import (
    db_mtime,
    is_mobile,
    metric_cells,
    recent_closes,
    ticker_table_html,
)

_MOBILE = is_mobile()


def _first_run_banner() -> None:
    """Getting-started checklist — shows until the ledger has transactions or
    the user dismisses it (persisted in prefs.json).

    Guests get a one-line banner with a direct sign-in button instead: their
    data dir is shared and read-only, so the dismissal only lives in session
    state.
    """
    if not auth.is_logged_in():
        if st.session_state.get("guest_banner_dismissed"):
            return
        if "auth" in st.secrets:
            st.info(
                "Browsing as a guest with a starter watchlist. Sign in to "
                "build your own watchlist and import your broker history.",
                icon=":material/waving_hand:",
            )
            row = st.container(horizontal=True)
            row.button(
                "Sign in with Google",
                key="guest_banner_login",
                icon=":material/login:",
                on_click=st.login,
            )
        else:
            st.info(
                "Browsing as a guest with a starter watchlist.",
                icon=":material/waving_hand:",
            )
            row = st.container(horizontal=True)
        if row.button("Dismiss", key="guest_banner_dismiss", type="tertiary"):
            st.session_state["guest_banner_dismissed"] = True
            st.rerun()
        return
    prefs = auth.load_prefs()
    if prefs.get("onboarding_dismissed"):
        return
    try:
        if all_transactions(auth.db_path()):
            return
    except Exception:
        pass  # unreadable/missing ledger = still new — show the banner
    with st.container(border=True):
        st.markdown(
            ":material/waving_hand: **New here? Three steps to a live portfolio**\n\n"
            "1. **Profile** — add the tickers you follow "
            "(you start with Apple and Microsoft as examples).\n"
            "2. **Import** — drop a Revolut statement (CSV) to fill your "
            "transaction ledger; every row is previewed before committing.\n"
            "3. **Portfolio** — positions, P/L, risk and Spanish tax then derive "
            "from the ledger automatically.\n\n"
            "No broker statement? The watchlist below, the **Screener** and "
            "**Valuation** work without one — click any ticker for its full "
            "analysis page."
        )
        if st.button("Got it — don't show this again", key="onboarding_dismiss"):
            prefs["onboarding_dismissed"] = True
            auth.save_prefs(prefs)
            st.rerun()


_first_run_banner()

# ------------------------------------------------------------------ portfolio
# Slim cut of the Portfolio page: headline value/day/P-L plus the positions
# table with the day change — enough for the daily glance. Risk, tax and
# dividends stay on the Portfolio page.
POS_FMT = {
    "value_eur": "€{:,.0f}",
    "weight": "{:.1%}",
    "day_eur": "€{:+,.0f}",
    "day_pct": "{:+.1%}",
    "pnl_eur": "€{:,.0f}",
    "pnl_pct": "{:+.1%}",
}
_SIGNED = ("day_eur", "day_pct", "pnl_eur", "pnl_pct")
POS_LABELS = {
    "ticker": "Ticker",
    "value_eur": "Value",
    "weight": "Weight",
    "day_eur": "Day €",
    "day_pct": "Day %",
    "pnl_eur": "P/L €",
    "pnl_pct": "P/L %",
}

positions: list = []  # guests hold nothing; the refresh button below checks it
if auth.is_logged_in():
    DB = str(auth.db_path())
    _, positions, realized = ledger_state(DB, db_mtime(DB))
    # realized non-empty proves ledger activity even with every position
    # closed; both empty = brand-new user, already covered by the banner.
    if positions or realized:
        st.subheader("Portfolio")
    if positions:
        tbl = enriched_positions(DB, db_mtime(DB))
        if tbl.empty:
            st.caption("Prices unavailable right now — try Refresh prices below.")
        else:
            cost = tbl["cost_eur"].sum()
            value = tbl["value_eur"].dropna().sum()
            # Headline figures honor the profile's display-currency preference
            # (spot-converted); the table stays EUR.
            ccy = auth.display_currency()
            fx = 1.0 if ccy == "EUR" else eur_spot(ccy)
            if fx is None:
                ccy, fx = "EUR", 1.0
            sym = auth.CURRENCY_SYMBOL[ccy]
            hist = basket_history(DB, db_mtime(DB))
            day, week, month = (basket_change(hist, d) for d in (1, 7, 30))
            gain_pct = (value / cost - 1) if cost else None
            _year = str(date.today().year)
            r_gain = sum(s.gain_eur for s in realized if s.sell_date[:4] == _year)
            r_cost = sum(s.cost_eur for s in realized if s.sell_date[:4] == _year)

            def _chg_metric(cell, label: str, chg) -> None:
                """EUR-change metric: signed value + % delta, n/a when the
                history doesn't cover the window."""
                cell.metric(
                    label,
                    f"{sym}{chg[0] * fx:+,.0f}" if chg else "n/a",
                    f"{chg[1]:+.2%}" if chg else None,
                )

            # Performance row: headline value, then windows short to long.
            # Market value stays delta-free — its % return already shows on
            # "Unrealised P/L", printing it twice reads as two numbers.
            c1, c2, c3, c4, c5 = metric_cells(5)
            c1.metric("Market value", f"{sym}{value * fx:,.0f}")
            _chg_metric(c2, "Today", day)
            _chg_metric(c3, "Week", week)
            _chg_metric(c4, "Month", month)
            c5.metric(
                "Unrealised P/L",
                f"{sym}{(value - cost) * fx:,.0f}",
                f"{gain_pct:+.1%}" if gain_pct is not None else None,
            )

            # Composition & movers row.
            k1, k2, k3, k4, k5 = metric_cells(5)
            k1.metric(
                "Realised YTD",
                f"{sym}{r_gain * fx:,.0f}",
                f"{r_gain / r_cost:+.1%}" if r_cost else None,
                help="Closed sales this calendar year (FIFO, EUR at trade "
                "dates). Tax detail on the Portfolio page.",
            )
            k2.metric("Positions", f"{len(tbl)}")
            top_w = tbl["weight"].iloc[0]
            k3.metric(
                "Top position",
                f"{tbl.index[0]} · {top_w:.0%}"
                if pd.notna(top_w)
                else str(tbl.index[0]),
                help="Largest holding's share of market value — a "
                "concentration check.",
            )
            movers = tbl["day_pct"].dropna()
            if not movers.empty:
                best, worst = movers.idxmax(), movers.idxmin()
                k4.metric("Best today", best, f"{movers[best]:+.2%}")
                k5.metric("Worst today", worst, f"{movers[worst]:+.2%}")
            else:
                k4.metric("Best today", "n/a")
                k5.metric("Worst today", "n/a")

            tbl.insert(0, "ticker", tbl.index)
            cols = (
                ["ticker", "value_eur", "weight", "day_pct", "pnl_pct"]
                if _MOBILE
                else ["ticker", "value_eur", "weight", "day_eur", "day_pct",
                      "pnl_eur", "pnl_pct"]
            )
            st.html(
                ticker_table_html(
                    tbl[cols], fmt=POS_FMT, signed=_SIGNED, labels=POS_LABELS
                )
            )
    elif realized:
        # Ledger has history but every position is closed.
        st.info(
            "No open positions — realised history and tax detail live on "
            "the Portfolio page.",
            icon=":material/history:",
        )
    if positions or realized:
        st.page_link(
            "app_pages/portfolio.py",
            label="Full portfolio — allocation, risk, tax & dividends",
            icon=":material/pie_chart:",
        )

# --------------------------------------------------------- earnings next week
# Slot reserved here so the card sits between portfolio and watchlist, but the
# yfinance earnings pass (slow on a cold cache) is filled in at the bottom of
# the script — the tables below render first instead of waiting on it.
_earn_slot = st.container()

# ------------------------------------------------------------------ watchlist
# No st.stop() on an empty watchlist: the earnings card at the bottom must
# still fill for a portfolio-only user, so empty groups just render nothing.
holdings = load_watchlist(auth.watchlist_path())
if not holdings:
    st.warning("Watchlist empty.")
    st.page_link(
        "app_pages/profile.py",
        label="Add tickers on the Profile page",
        icon=":material/account_circle:",
    )

closes = recent_closes(tuple(h.ticker for h in holdings)) if holdings else {}


def _watch_table(tickers: list[str]) -> None:
    """Ticker | last close | day % for one watchlist group."""
    rows = []
    for t in tickers:
        c = closes.get(t) or []
        last = c[-1] if c else float("nan")
        day = (c[-1] / c[-2] - 1) if len(c) >= 2 and c[-2] else float("nan")
        rows.append({"ticker": t, "price": last, "day_pct": day})
    st.html(
        ticker_table_html(
            pd.DataFrame(rows),
            fmt={"price": "{:,.2f}", "day_pct": "{:+.2%}"},
            signed=("day_pct",),
            labels={"ticker": "Ticker", "price": "Last close", "day_pct": "Day %"},
        )
    )


# Favorites first, then each tag group (a ticker can appear in several),
# then whatever is neither — one section per group.
favs = [h.ticker for h in holdings if h.favorite]
tag_groups: dict[str, list[str]] = {}
for h in holdings:
    for tag in h.tags:
        tag_groups.setdefault(tag, []).append(h.ticker)
rest = [h.ticker for h in holdings if not h.favorite and not h.tags]

if favs:
    st.subheader(":material/star: Favorites")
    _watch_table(favs)
for tag in sorted(tag_groups, key=str.upper):
    st.subheader(f":material/label: {tag}")
    _watch_table(tag_groups[tag])
if rest:
    if favs or tag_groups:
        st.subheader(":material/list: Watchlist")
    else:
        st.subheader("Watchlist")
    _watch_table(rest)
if holdings:
    st.caption(
        "Last close vs previous close (yfinance, cached 15 min). Click a "
        "ticker for price, fundamentals, insiders and comps; star or tag it "
        "from the sidebar to group it here."
    )
if holdings or positions:
    # Manual escape hatch for stale quotes: drop the price caches (watchlist
    # closes + both position price loads) and rerun; ledger caches stay hot.
    if st.button("Refresh prices", icon=":material/refresh:"):
        recent_closes.clear()
        positions_table.clear()
        basket_history.clear()
        st.rerun()


# --------------------------------------------- earnings next week (slot fill)
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _week_events(tickers: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """(ticker, ISO date, days_until) for reports due within 7 days — one
    parallel yfinance pass, keyed by the ticker tuple so watchlist or
    portfolio edits invalidate it."""
    events = upcoming([Holding(t) for t in tickers], within_days=7)
    return [(e.ticker, e.date.isoformat(), e.days_until) for e in events]


# Portfolio + favorites + tagged tickers; crypto has no earnings. Positions
# come from the already-cached ledger state, so guests simply get no held set.
_held: set[str] = set()
if auth.is_logged_in():
    _db = str(auth.db_path())
    _held = {p.ticker for p in ledger_state(_db, db_mtime(_db))[1]}
_tagged = {t for ts in tag_groups.values() for t in ts}
_earn_tickers = tuple(
    sorted(t for t in _held | set(favs) | _tagged if not is_crypto(t))
)

if _earn_tickers:
    with _earn_slot.container(border=True):
        st.markdown(":material/event_upcoming: **Earnings next week**")
        _events = _week_events(_earn_tickers)
        if _events:
            _today = date.today()

            def _when(iso: str, days: int) -> str:
                d = date.fromisoformat(iso)
                label = {0: "today", 1: "tomorrow"}.get(days, d.strftime("%A"))
                return f"{d.strftime('%d %b')} · {label}"

            st.html(
                ticker_table_html(
                    pd.DataFrame(
                        [
                            {"ticker": t, "reports": _when(iso, days)}
                            for t, iso, days in _events
                        ]
                    ),
                    labels={"ticker": "Ticker", "reports": "Reports"},
                )
            )
        else:
            st.caption("No reports due in the next 7 days.")
        st.page_link(
            "app_pages/earnings.py",
            label="Full earnings calendar",
            icon=":material/calendar_month:",
        )
