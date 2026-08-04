"""Home page — the daily glance: what's new plus the key portfolio metrics.

One metrics row with a 30-day sparkline and today's movers (a slim cut of the
Portfolio page), then "What's new" cards (watchlist big moves, earnings,
recent transactions, 52-week extremes), then the watchlist groups collapsed
into expanders. Every ticker cell links to the Ticker page; the full ledger
analytics stay on Portfolio.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
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
    ledger_history,
    ledger_state,
    positions_table,
)
from stocks.web.widgets import (
    HOVERLABEL,
    LOSS_COLOR,
    PROFIT_COLOR,
    db_mtime,
    recent_closes,
    ticker_table_html,
)


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

# ---------------------------------------------------------- portfolio glance
# Daily-glance cut of the Portfolio page: value / today / unrealised P/L plus
# a 30-day sparkline, then today's top movers. The full positions table, risk,
# tax and dividends stay on the Portfolio page.
MOVER_FMT = {"day_pct": "{:+.2%}"}
MOVER_LABELS = {"ticker": "Ticker", "day_pct": "Day %"}

txs: list = []
positions: list = []  # guests hold nothing; the refresh button below checks it
_spark_slot = None  # reserved in the glance row, filled at the bottom
if auth.is_logged_in():
    DB = str(auth.db_path())
    txs, positions, realized = ledger_state(DB, db_mtime(DB))
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
            # (spot-converted); the tables stay EUR.
            ccy = auth.display_currency()
            fx = 1.0 if ccy == "EUR" else eur_spot(ccy)
            if fx is None:
                ccy, fx = "EUR", 1.0
            sym = auth.CURRENCY_SYMBOL[ccy]
            hist = basket_history(DB, db_mtime(DB))
            day = basket_change(hist, 1)
            gain_pct = (value / cost - 1) if cost else None

            # Glance row: three metrics + the 30-day sparkline. Market value
            # stays delta-free — its % return already shows on "Unrealised
            # P/L", printing it twice reads as two numbers. Plain st.columns
            # (not metric_cells): the sparkline needs the width, and on phones
            # the cells stack naturally.
            c1, c2, c3, c4 = st.columns([1, 1, 1, 2], vertical_alignment="center")
            c1.metric("Market value", f"{sym}{value * fx:,.0f}")
            c2.metric(
                "Today",
                f"{sym}{day[0] * fx:+,.0f}" if day else "n/a",
                f"{day[1]:+.2%}" if day else None,
            )
            c3.metric(
                "Unrealised P/L",
                f"{sym}{(value - cost) * fx:+,.0f}",
                f"{gain_pct:+.1%}" if gain_pct is not None else None,
            )
            # 30-day value-vs-injected chart. Container only: ledger_history's
            # cold build fetches the ledger's full price span, so the fill
            # happens at the bottom of the script (deferred-slot pattern) and
            # never blocks this row's paint. basket_history would be wrong
            # here — fixed basket, not flow-adjusted, so it diverges from
            # injected capital around buys/sells.
            _spark_slot = c4.container()

            # Movers today: biggest day moves among open positions — the full
            # table lives on the Portfolio page. Positive/negative split, so a
            # ticker lands in at most one list.
            movers = tbl["day_pct"].dropna()
            gainers = movers[movers > 0].nlargest(3)
            losers = movers[movers < 0].nsmallest(3)
            if not gainers.empty or not losers.empty:
                st.markdown("**Movers today**")
                gcol, lcol = st.columns(2)

                def _mover_table(box, series: pd.Series, label: str) -> None:
                    box.caption(label)
                    if series.empty:
                        box.caption("None today.")
                        return
                    box.html(
                        ticker_table_html(
                            pd.DataFrame(
                                {"ticker": series.index, "day_pct": series.values}
                            ),
                            fmt=MOVER_FMT,
                            signed=("day_pct",),
                            labels=MOVER_LABELS,
                            names=False,
                        )
                    )

                _mover_table(gcol, gainers, "Gainers")
                _mover_table(lcol, losers, "Losers")
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

_held = {p.ticker for p in positions}

# Watchlist data loads here (the Big-moves card needs the closes); the group
# tables render further down. No st.stop() on an empty watchlist: the deferred
# cards at the bottom must still fill for a portfolio-only user.
holdings = load_watchlist(auth.watchlist_path())
closes = recent_closes(tuple(h.ticker for h in holdings)) if holdings else {}

# ------------------------------------------------------------------ what's new
# Bordered cards, two per row on desktop (st.columns stack on phones). The
# earnings and 52-week cards only reserve containers here — their yfinance
# passes (slow on a cold cache) fill at the bottom of the script so the rest
# of the page paints first.
st.subheader("What's new")

_r1a, _r1b = st.columns(2)
with _r1a, st.container(border=True):
    st.markdown(":material/bolt: **Big moves today**")
    # Watchlist-only: held tickers already appear under Movers today.
    _big = []
    for t in dict.fromkeys(h.ticker for h in holdings):
        if t in _held:
            continue
        c = closes.get(t) or []
        if len(c) >= 2 and c[-2] and abs(chg := c[-1] / c[-2] - 1) >= 0.03:
            _big.append({"ticker": t, "day_pct": chg})
    if _big:
        _big.sort(key=lambda r: -abs(r["day_pct"]))
        st.html(
            ticker_table_html(
                pd.DataFrame(_big),
                fmt=MOVER_FMT,
                signed=("day_pct",),
                labels=MOVER_LABELS,
            )
        )
    else:
        st.caption("No watchlist moves over ±3%.")
_earn_slot = _r1b.container()

_r2a, _r2b = st.columns(2)
if txs:
    with _r2a, st.container(border=True):
        st.markdown(":material/receipt_long: **Recent transactions**")

        def _tx_amount_eur(t) -> float:
            """Cash amount in EUR at the trade date (split has none). The
            ledger replay above already prefetched these FX dates, so rate_on
            resolves from the on-disk cache."""
            amount = {
                "buy": t.quantity * t.price + t.fee,
                "sell": t.quantity * t.price - t.fee,
                "dividend": t.price,
                "fee": t.fee,
            }.get(t.action)
            if amount is None:
                return float("nan")
            try:
                from stocks.data.fx import rate_on

                return amount * rate_on(t.date, t.currency, "EUR")
            except Exception:
                return float("nan")

        st.html(
            ticker_table_html(
                pd.DataFrame(
                    {
                        "date": t.date,
                        "action": t.action,
                        "ticker": t.ticker,
                        "amount_eur": _tx_amount_eur(t),
                    }
                    for t in txs[-5:][::-1]
                ),
                fmt={"amount_eur": "€{:,.2f}"},
                left_cols=("date", "action"),
                labels={
                    "date": "Date",
                    "action": "Type",
                    "ticker": "Ticker",
                    "amount_eur": "EUR",
                },
                names=False,
            )
        )
        st.page_link(
            "app_pages/import_transactions.py",
            label="Import & manage transactions",
            icon=":material/upload_file:",
        )
    _xt_slot = _r2b.container()
else:
    _xt_slot = _r2a.container()


# Defined above the refresh button so its clear() call can reference it; the
# actual 1y fetch still only runs in the deferred fill at the bottom.
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _year_extremes(tickers: tuple[str, ...]) -> list[tuple[str, float, str]]:
    """(ticker, last close, distance label) for names within 2% of their
    52-week high or low — one bulk 1y download, keyed by the ticker tuple so
    watchlist or portfolio edits invalidate it."""
    from stocks.data.fetch import fetch_many

    out: list[tuple[str, float, str]] = []
    for t, df in fetch_many(list(tickers), period="1y").items():
        close = df["Close"].dropna() if "Close" in df else None
        if close is None or len(close) < 2:
            continue
        last, hi, lo = float(close.iloc[-1]), float(close.max()), float(close.min())
        if hi and last >= hi * 0.98:
            label = (
                "at 52w high" if last >= hi else f"{last / hi - 1:+.1%} from 52w high"
            )
            out.append((t, last, label))
        elif lo and last <= lo * 1.02:
            label = (
                "at 52w low" if last <= lo else f"{last / lo - 1:+.1%} from 52w low"
            )
            out.append((t, last, label))
    return out


# ------------------------------------------------------------------ watchlist
if not holdings:
    st.warning("Watchlist empty.")
    st.page_link(
        "app_pages/profile.py",
        label="Add tickers on the Profile page",
        icon=":material/account_circle:",
    )


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
# then whatever is neither — one expander per group, favorites open.
favs = [h.ticker for h in holdings if h.favorite]
tag_groups: dict[str, list[str]] = {}
for h in holdings:
    for tag in h.tags:
        tag_groups.setdefault(tag, []).append(h.ticker)
rest = [h.ticker for h in holdings if not h.favorite and not h.tags]

if favs:
    with st.expander("Favorites", expanded=True, icon=":material/star:"):
        _watch_table(favs)
for tag in sorted(tag_groups, key=str.upper):
    with st.expander(tag, expanded=False, icon=":material/label:"):
        _watch_table(tag_groups[tag])
if rest:
    with st.expander("Watchlist", expanded=False, icon=":material/list:"):
        _watch_table(rest)
if holdings:
    st.caption(
        "Last close vs previous close (yfinance, cached 15 min). Click a "
        "ticker for price, fundamentals, insiders and comps; star or tag it "
        "from the sidebar to group it here."
    )
if holdings or positions:
    # Manual escape hatch for stale quotes: drop the price caches (watchlist
    # closes, both position price loads, the ledger history, the 52-week
    # scan) and rerun; ledger caches stay hot.
    if st.button("Refresh prices", icon=":material/refresh:"):
        recent_closes.clear()
        positions_table.clear()
        basket_history.clear()
        ledger_history.clear()
        _year_extremes.clear()
        st.rerun()


# ------------------------------------------ value vs injected (slot fill)
# Same fingerprint as the Portfolio page's calls, so the cache entry is
# shared and hot after either page built it.
if _spark_slot is not None:
    # Called inside the slot so the cold-build spinner shows in the chart's
    # cell instead of at the page bottom.
    with _spark_slot:
        _hist, _, _ = ledger_history((len(txs), txs[-1].date, date.today()), DB)
    if not _hist.empty:
        # ffill BEFORE the 30-day slice so both lines enter the window
        # continuous instead of starting on the first in-window quote.
        _hist = _hist.ffill()
        _hist = _hist.loc[_hist.index >= _hist.index[-1] - pd.Timedelta(days=30)]
    if len(_hist) >= 2:

        def _pct_span(p) -> str:
            """Gain % as a colored <b> span for the hovertemplate (same
            inline-HTML trick as the Portfolio page's history chart)."""
            if pd.isna(p):
                return "—"
            color = PROFIT_COLOR if p >= 0 else LOSS_COLOR
            return f'<span style="color:{color}"><b>{p:+.1%}</b></span>'

        _custom = [
            [inj, _pct_span(p)]
            for inj, p in zip(_hist["injected_eur"], _hist["pnl_pct"], strict=True)
        ]
        fig = go.Figure()
        # Injected is the reference line: muted gray step (contributions are
        # steps, not slopes), visually secondary to the accent value line.
        fig.add_trace(
            go.Scatter(
                x=_hist.index,
                y=_hist["injected_eur"],
                name="Injected",
                line=dict(color="#9aa4b2", width=1.5, shape="hv", dash="dot"),
                hoverinfo="skip",
            )
        )
        # Theme accent, not green/red: with two lines a sign-colored value
        # line reads as "in profit", not "up this month" — polarity lives in
        # the tooltip's colored % and the Today delta instead.
        fig.add_trace(
            go.Scatter(
                x=_hist.index,
                y=_hist["value_eur"],
                name="Value",
                line=dict(color="#60A5FA", width=2),
                customdata=_custom,
                hovertemplate=(
                    "<b>%{x|%d %b %Y}</b><br>"
                    "Value <b>€%{y:,.0f}</b> · "
                    "Injected €%{customdata[0]:,.0f} · "
                    "%{customdata[1]}<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            height=132,
            margin=dict(l=0, r=0, t=20, b=0),
            hovermode="x",
            legend=dict(
                orientation="h", x=0, y=1.0, yanchor="bottom", font=dict(size=10)
            ),
            xaxis=dict(
                nticks=3, tickfont=dict(size=10), showgrid=False,
                fixedrange=True, automargin=True,
            ),
            yaxis=dict(
                nticks=3, tickfont=dict(size=10), tickprefix="€",
                tickformat="~s", showgrid=False, fixedrange=True,
                automargin=True,
            ),
            hoverlabel=HOVERLABEL,
        )
        _spark_slot.plotly_chart(fig, config={"displayModeBar": False})


# --------------------------------------------- earnings next week (slot fill)
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _week_events(tickers: tuple[str, ...]) -> list[tuple[str, str, int]]:
    """(ticker, ISO date, days_until) for reports due within 7 days — one
    parallel yfinance pass, keyed by the ticker tuple so watchlist or
    portfolio edits invalidate it."""
    events = upcoming([Holding(t) for t in tickers], within_days=7)
    return [(e.ticker, e.date.isoformat(), e.days_until) for e in events]


# Portfolio + favorites + tagged tickers; crypto has no earnings.
_tagged = {t for ts in tag_groups.values() for t in ts}
_earn_tickers = tuple(
    sorted(t for t in _held | set(favs) | _tagged if not is_crypto(t))
)

if _earn_tickers:
    with _earn_slot.container(border=True):
        st.markdown(":material/event_upcoming: **Earnings next week**")
        _events = _week_events(_earn_tickers)
        if _events:

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

# --------------------------------------------- 52-week extremes (slot fill)
# Held + favorites only, so the 1y bulk fetch stays small; crypto pairs have
# no meaningful 52-week narrative here and are skipped.
_xt_tickers = tuple(sorted(t for t in _held | set(favs) if not is_crypto(t)))

if _xt_tickers:
    with _xt_slot.container(border=True):
        st.markdown(":material/candlestick_chart: **52-week extremes**")
        _extremes = _year_extremes(_xt_tickers)
        if _extremes:
            st.html(
                ticker_table_html(
                    pd.DataFrame(
                        [
                            {"ticker": t, "price": px, "where": label}
                            for t, px, label in _extremes
                        ]
                    ),
                    fmt={"price": "{:,.2f}"},
                    labels={
                        "ticker": "Ticker",
                        "price": "Last close",
                        "where": "52-week",
                    },
                )
            )
        else:
            st.caption("Nothing at 52-week extremes.")
