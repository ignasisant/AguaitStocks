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
from stocks.web.i18n import t as tr
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
                tr("home.guest_banner"),
                icon=":material/waving_hand:",
            )
            row = st.container(horizontal=True)
            row.button(
                tr("common.sign_in_google"),
                key="guest_banner_login",
                icon=":material/login:",
                on_click=st.login,
            )
        else:
            st.info(
                tr("home.guest_banner_short"),
                icon=":material/waving_hand:",
            )
            row = st.container(horizontal=True)
        if row.button(tr("home.dismiss"), key="guest_banner_dismiss", type="tertiary"):
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
        st.markdown(tr("home.onboarding_md"))
        if st.button(tr("home.onboarding_dismiss"), key="onboarding_dismiss"):
            prefs["onboarding_dismissed"] = True
            auth.save_prefs(prefs)
            st.rerun()


_first_run_banner()

# ---------------------------------------------------------- portfolio glance
# Daily-glance cut of the Portfolio page: value / today / unrealised P/L plus
# a 30-day sparkline, then today's top movers. The full positions table, risk,
# tax and dividends stay on the Portfolio page.
MOVER_FMT = {"day_pct": "{:+.2%}"}
MOVER_LABELS = {"ticker": tr("home.col_ticker"), "day_pct": tr("home.col_day_pct")}

txs: list = []
positions: list = []  # guests hold nothing; the refresh button below checks it
_spark_slot = None  # reserved in the glance row, filled at the bottom
if auth.is_logged_in():
    DB = str(auth.db_path())
    txs, positions, realized = ledger_state(DB, db_mtime(DB))
    # realized non-empty proves ledger activity even with every position
    # closed; both empty = brand-new user, already covered by the banner.
    if positions or realized:
        st.subheader(tr("nav.portfolio"))
    if positions:
        tbl = enriched_positions(DB, db_mtime(DB))
        if tbl.empty:
            st.caption(tr("home.prices_unavailable"))
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
            c1.metric(tr("home.market_value"), f"{sym}{value * fx:,.0f}")
            c2.metric(
                tr("home.today"),
                f"{sym}{day[0] * fx:+,.0f}" if day else tr("home.na"),
                f"{day[1]:+.2%}" if day else None,
            )
            c3.metric(
                tr("home.unrealised_pl"),
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
                st.markdown(tr("home.movers_today"))
                gcol, lcol = st.columns(2)

                def _mover_table(box, series: pd.Series, label: str) -> None:
                    box.caption(label)
                    if series.empty:
                        box.caption(tr("home.none_today"))
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

                _mover_table(gcol, gainers, tr("home.gainers"))
                _mover_table(lcol, losers, tr("home.losers"))
    elif realized:
        # Ledger has history but every position is closed.
        st.info(
            tr("home.no_open_positions"),
            icon=":material/history:",
        )
    if positions or realized:
        st.page_link(
            "app_pages/portfolio.py",
            label=tr("home.link_full_portfolio"),
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
st.subheader(tr("home.whats_new"))

_r1a, _r1b = st.columns(2)
with _r1a, st.container(border=True):
    st.markdown(tr("home.big_moves"))
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
        st.caption(tr("home.no_big_moves"))
_earn_slot = _r1b.container()

_r2a, _r2b = st.columns(2)
if txs:
    with _r2a, st.container(border=True):
        st.markdown(tr("home.recent_transactions"))

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
                    "date": tr("home.col_date"),
                    "action": tr("home.col_type"),
                    "ticker": tr("home.col_ticker"),
                    "amount_eur": "EUR",
                },
                names=False,
            )
        )
        st.page_link(
            "app_pages/import_transactions.py",
            label=tr("home.link_import"),
            icon=":material/upload_file:",
        )
    _xt_slot = _r2b.container()
else:
    _xt_slot = _r2a.container()


# Defined above the refresh button so its clear() call can reference it; the
# actual 1y fetch still only runs in the deferred fill at the bottom.
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _year_extremes(tickers: tuple[str, ...]) -> list[tuple[str, float, str, float | None]]:
    """(ticker, last close, "high"/"low", distance) for names within 2% of
    their 52-week high or low — one bulk 1y download, keyed by the ticker tuple
    so watchlist or portfolio edits invalidate it. Distance is None when the
    price is at/beyond the extreme; the display label is built and localized at
    render so a cached row never carries a fixed-language string."""
    from stocks.data.fetch import fetch_many

    out: list[tuple[str, float, str, float | None]] = []
    for t, df in fetch_many(list(tickers), period="1y").items():
        close = df["Close"].dropna() if "Close" in df else None
        if close is None or len(close) < 2:
            continue
        last, hi, lo = float(close.iloc[-1]), float(close.max()), float(close.min())
        if hi and last >= hi * 0.98:
            out.append((t, last, "high", None if last >= hi else last / hi - 1))
        elif lo and last <= lo * 1.02:
            out.append((t, last, "low", None if last <= lo else last / lo - 1))
    return out


# ------------------------------------------------------------------ watchlist
if not holdings:
    st.warning(tr("home.watchlist_empty"))
    st.page_link(
        "app_pages/profile.py",
        label=tr("home.link_add_tickers"),
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
            labels={
                "ticker": tr("home.col_ticker"),
                "price": tr("home.col_last_close"),
                "day_pct": tr("home.col_day_pct"),
            },
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
    with st.expander(tr("home.favorites"), expanded=True, icon=":material/star:"):
        _watch_table(favs)
for tag in sorted(tag_groups, key=str.upper):
    with st.expander(tag, expanded=False, icon=":material/label:"):
        _watch_table(tag_groups[tag])
if rest:
    with st.expander(tr("home.watchlist"), expanded=False, icon=":material/list:"):
        _watch_table(rest)
if holdings:
    st.caption(tr("home.watchlist_caption"))
if holdings or positions:
    # Manual escape hatch for stale quotes: drop the price caches (watchlist
    # closes, both position price loads, the ledger history, the 52-week
    # scan) and rerun; ledger caches stay hot.
    if st.button(tr("home.refresh_prices"), icon=":material/refresh:"):
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

        # Localized date for the hover (plotly's %{x|%b} would render the month
        # in English); day/year stay numeric, month name comes from the catalog.
        def _spark_date(ts) -> str:
            return f"{ts.day:02d} {tr(f'home.mon_{ts.month}')} {ts.year}"

        _custom = [
            [inj, _pct_span(p), _spark_date(ts)]
            for ts, inj, p in zip(
                _hist.index, _hist["injected_eur"], _hist["pnl_pct"], strict=True
            )
        ]
        fig = go.Figure()
        # Injected is the reference line: muted gray step (contributions are
        # steps, not slopes), visually secondary to the accent value line.
        fig.add_trace(
            go.Scatter(
                x=_hist.index,
                y=_hist["injected_eur"],
                name=tr("home.chart_injected"),
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
                name=tr("home.chart_value"),
                line=dict(color="#60A5FA", width=2),
                customdata=_custom,
                hovertemplate=tr("home.spark_hover_tmpl"),
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
        st.markdown(tr("home.earnings_next_week"))
        _events = _week_events(_earn_tickers)
        if _events:

            def _when(iso: str, days: int) -> str:
                d = date.fromisoformat(iso)
                if days == 0:
                    label = tr("home.rel_today")
                elif days == 1:
                    label = tr("home.rel_tomorrow")
                else:
                    label = tr(f"home.weekday_{d.weekday()}")
                return f"{d.day:02d} {tr(f'home.month_{d.month}')} · {label}"

            st.html(
                ticker_table_html(
                    pd.DataFrame(
                        [
                            {"ticker": t, "reports": _when(iso, days)}
                            for t, iso, days in _events
                        ]
                    ),
                    labels={
                        "ticker": tr("home.col_ticker"),
                        "reports": tr("home.col_reports"),
                    },
                )
            )
        else:
            st.caption(tr("home.no_reports"))
        st.page_link(
            "app_pages/earnings.py",
            label=tr("home.link_earnings_calendar"),
            icon=":material/calendar_month:",
        )

# --------------------------------------------- 52-week extremes (slot fill)
# Held + favorites only, so the 1y bulk fetch stays small; crypto pairs have
# no meaningful 52-week narrative here and are skipped.
_xt_tickers = tuple(sorted(t for t in _held | set(favs) if not is_crypto(t)))

if _xt_tickers:
    with _xt_slot.container(border=True):
        st.markdown(tr("home.extremes_52w"))
        _extremes = _year_extremes(_xt_tickers)
        if _extremes:

            def _where(kind: str, pct: float | None) -> str:
                if kind == "high":
                    return (
                        tr("home.at_52w_high")
                        if pct is None
                        else tr("home.from_52w_high", pct=f"{pct:+.1%}")
                    )
                return (
                    tr("home.at_52w_low")
                    if pct is None
                    else tr("home.from_52w_low", pct=f"{pct:+.1%}")
                )

            st.html(
                ticker_table_html(
                    pd.DataFrame(
                        [
                            {"ticker": t, "price": px, "where": _where(kind, pct)}
                            for t, px, kind, pct in _extremes
                        ]
                    ),
                    fmt={"price": "{:,.2f}"},
                    labels={
                        "ticker": tr("home.col_ticker"),
                        "price": tr("home.col_last_close"),
                        "where": tr("home.col_52week"),
                    },
                )
            )
        else:
            st.caption(tr("home.no_extremes"))
