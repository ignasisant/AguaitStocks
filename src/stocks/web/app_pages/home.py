"""Home page — the daily glance: what's new plus the key portfolio metrics.

The Portfolio page's headline metrics (cost basis, market value, unrealised &
realised P/L, then today / 1 week / 1 month deltas) with a range-selectable
value-vs-injected sparkline and
today's movers, then "What's new" cards (watchlist big moves, earnings,
recent transactions, 52-week extremes), then the watchlist groups collapsed
into expanders. Every ticker cell links to the Ticker page; the full ledger
analytics stay on Portfolio.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from urllib.error import URLError

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.analysis.portfolio import (
    basket_change,
    market_active,
    market_live,
    us_extended_session,
    us_market_open,
)
from stocks.config import Holding, load_watchlist
from stocks.data.crypto import is_crypto
from stocks.data.earnings import calendar_events
from stocks.data.funds import is_fund
from stocks.portfolio.ledger import all_transactions
from stocks.web import auth, notices, skeletons
from stocks.web.earnings_ui import calendar_component, render_result_body
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import (
    basket_history,
    enriched_positions,
    eur_spot,
    last_session_moves,
    ledger_history,
    ledger_state,
    native_eur_rates,
    positions_table,
)
from stocks.web.widgets import (
    HOVERLABEL,
    LOSS_COLOR,
    PROFIT_COLOR,
    TEXT_MUTED,
    TRANSPARENT,
    calendar_css,
    company_name,
    db_mtime,
    is_mobile,
    kpi_delta_chip,
    kpi_grid_html,
    logo,
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
                on_click=auth.login,
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


def _setup_card() -> None:
    """Feature-activation checklist — which of the four connectable
    capabilities (Google sign-in, ledger import, a BYOK provider key,
    Telegram) this account has switched on. Pending rows carry their
    activation entry point; once everything is active the card offers a
    one-time dismiss persisted in prefs.json.
    """
    signed_in = auth.is_logged_in()
    prefs = auth.load_prefs()
    imported = False
    if signed_in:
        try:
            imported = bool(all_transactions(auth.db_path()))
        except Exception:
            pass  # unreadable/missing ledger reads as "not imported yet"
    # "Connected" = a provider key saved to prefs (encrypted, chat_core) or
    # entered this session; the keyless TopStocks free chain doesn't count.
    has_ai_key = any(k.endswith("_key_enc") for k in prefs) or any(
        k.startswith("llm_key::") and st.session_state[k] for k in st.session_state
    )
    tg_linked = bool(prefs.get("telegram_chat_id"))

    states = (signed_in, imported, has_ai_key, tg_linked)
    done = sum(states)
    if done == len(states) and prefs.get("setup_card_dismissed"):
        return

    def _label(on: bool, icon: str, label: str) -> str:
        """Pill label: the feature's own icon, green + bold when active,
        gray when pending."""
        return (f":green[:material/{icon}:] **{label}**" if on
                else f":gray[:material/{icon}:] :gray[{label}]")

    # Dense single-strip card: title + progress badge + one uniform tertiary
    # pill per feature, all in a wrapping horizontal row. Same element type
    # everywhere keeps the pills aligned; page pills navigate via
    # st.switch_page. Guests get disabled pills — the target pages sit behind
    # require_login() — except sign-in, which is the pending action itself.
    with st.container(border=True):
        row = st.container(horizontal=True, vertical_alignment="center",
                           gap="small")
        row.markdown(
            f"{tr('home.setup_title')} "
            f":gray-badge[{tr('home.setup_progress', done=done, total=len(states))}]"
        )

        # Google pill: pending = the sign-in action; done = Profile (log-out
        # and account settings live there).
        if signed_in:
            if row.button(_label(True, "account_circle", tr("home.setup_google")),
                          key="setup_card_google", type="tertiary"):
                st.switch_page("app_pages/profile.py")
        else:
            row.button(_label(False, "account_circle", tr("home.setup_google")),
                       key="setup_card_login", type="tertiary",
                       on_click=auth.login, disabled="auth" not in st.secrets)

        if row.button(_label(imported, "upload_file", tr("home.setup_import")),
                      key="setup_card_import", type="tertiary",
                      disabled=not signed_in):
            st.switch_page("app_pages/import_transactions.py")

        # AI pill: the key gate lives in the assistant side panel, not a nav
        # page, so it opens the panel in place (signed-in only — app.py gates
        # render_side_panel on is_logged_in()). auto_awesome = the launcher
        # FAB's icon, so the pill points at the thing it opens.
        if row.button(_label(has_ai_key, "auto_awesome", tr("home.setup_ai")),
                      key="setup_card_ai", type="tertiary",
                      disabled=not signed_in):
            st.session_state["chat_panel_open"] = True
            st.rerun()

        if row.button(_label(tg_linked, "send", tr("home.setup_tg")),
                      key="setup_card_tg", type="tertiary",
                      disabled=not signed_in):
            # Land on the Notifications tab, where the linking flow lives.
            st.session_state["profile_tab"] = "notify"
            st.switch_page("app_pages/profile.py")

        if done == len(states):
            if row.button(f":material/close: {tr('home.dismiss')}",
                          key="setup_card_dismiss", type="tertiary"):
                prefs["setup_card_dismissed"] = True
                auth.save_prefs(prefs)
                st.rerun()


_first_run_banner()
_setup_card()

# ---------------------------------------------------------- portfolio glance
# Daily-glance cut of the Portfolio page: value / today / unrealised P/L plus
# a range-selectable sparkline, then today's top movers. The full table, risk,
# tax and dividends stay on the Portfolio page.
# Movers table: ticker, live share price with the day move as a chip beside it
# ("$226.10  +18.92%"), live market value (display currency), portfolio weight.
# The price/value/weight columns come from enriched_positions (tbl) reindexed to
# each list's tickers; the value column's fmt is built at render (it needs the
# display-currency symbol) and the price cells are pre-formatted per row in
# their own quote currency.
MOVER_FMT = {"weight": "{:.1%}", "day_pct": "{:+.2%}"}


def _native_price(value: float, ccy: str, rates: dict[str, float]) -> str:
    """One share price, formatted in the ticker's own quote currency.

    `value` is the EUR price per share; `rates` maps that currency to its
    native->EUR spot. Pre-formatted here (not via ticker_table_html's `fmt`)
    because the symbol varies per row, and a column format string can't.
    """
    rate = rates.get(ccy)
    if not rate or pd.isna(value):
        return "n/a"
    prefix = auth.CURRENCY_SYMBOL.get(ccy) or f"{ccy} "
    return f"{prefix}{value / rate:,.2f}"


MOVER_LABELS = {
    "ticker": tr("home.col_ticker"),
    "price": tr("home.col_price"),
    "value": tr("home.col_value"),
    "weight": tr("home.col_weight"),
    "day_pct": tr("home.col_day_pct"),
}

txs: list = []
positions: list = []  # guests hold nothing; the refresh button below checks it
held_moves: dict[str, float] = {}  # {ticker: day_pct}, feeds the Big-moves card
_spark_slot = None  # reserved in the glance row, filled at the bottom
if auth.is_logged_in():
    DB = str(auth.db_path())
    realized: list = []
    try:
        txs, positions, realized = ledger_state(DB, db_mtime(DB))
    except (YFRateLimitError, URLError) as exc:
        # Throttled/offline: toast it and leave the glance empty (txs/positions
        # keep their empty defaults above). Not cached, so a rerun retries.
        notices.data_toast(exc)
    except Exception:
        # Ledger replay / FX prefetch failed for a non-banner reason: skip the
        # whole glance (txs/positions/realized stay empty) instead of crashing.
        st.warning(tr("home.data_unavailable"))
    # realized non-empty proves ledger activity even with every position
    # closed; both empty = brand-new user, already covered by the banner.
    if positions or realized:
        st.subheader(tr("home.portfolio_title"))
    if positions:
        # The live price burst behind the glance is the page's slowest block,
        # so the card shimmers its two KPI rows in place while it runs: the
        # "What's new" section below keeps its position instead of sliding up
        # and dropping back down when the numbers land. Every branch out ends
        # in the slot, so the shimmer is always replaced by what it stood for.
        glance = skeletons.reserve("metrics", border=True, n=(4, 3))
        try:
            tbl = enriched_positions(DB, db_mtime(DB))
        except (YFRateLimitError, URLError) as exc:
            notices.data_toast(exc)
            tbl = pd.DataFrame()  # empty (not None) -> the softer price caption
        except Exception:
            tbl = None  # price/FX enrichment failed — skip the glance card
        if tbl is None:
            glance.container().warning(tr("home.data_unavailable"))
        elif tbl.empty:
            glance.container().caption(tr("home.prices_unavailable"))
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
            try:
                hist = basket_history(DB, db_mtime(DB))
            except (YFRateLimitError, URLError) as exc:
                notices.data_toast(exc)
                hist = pd.DataFrame()  # 1w/1m deltas read n/a
            except Exception:
                # basket_change(empty, …) is None, so the 1w/1m deltas below
                # degrade to their "n/a" cells instead of crashing the card.
                hist = pd.DataFrame()
            gain_pct = (value / cost - 1) if cost else None
            realized_gain = sum(s.gain_eur for s in realized)
            realized_cost = sum(s.cost_eur for s in realized)

            # KPIs + sparkline live in a bordered card, matching the "What's
            # new" cards below — the same card the skeleton above was drawn
            # inside, so filling the slot swaps shimmer for figures without
            # moving the outline. `with`-scoped so bare st.* calls
            # (st.columns, st.caption) land inside it.
            with glance.container(border=True):
                # Layout: desktop splits the glance into a 70%-wide KPI column
                # and a 30%-wide sparkline column spanning both KPI rows — the
                # chart no longer floats against the delta row alone. Mobile
                # drops the sparkline to a slot below; the KPI grid wraps its
                # own tiles (auto-fit), so no per-breakpoint cell carving.
                # The sparkline's own fetch (ledger_history) runs at the bottom
                # of the script, so its cell shimmers a chart-shaped slot until
                # then rather than sitting blank beside finished KPIs.
                if is_mobile():
                    kcol = st.container()
                    _spark_slot = skeletons.reserve("chart", height=132)
                else:
                    kcol, ccol = st.columns([7, 3], vertical_alignment="center")
                    _spark_slot = skeletons.reserve(
                        "chart", container=ccol, height=190
                    )

                # Balance row — the same four headline figures as the Portfolio
                # page, as kpi_grid_html tiles so the glance reads like the
                # Ticker fundamentals card: value and its chip on one line.
                # Market value stays chip-free (its % return already shows on
                # Unrealised P/L; printing it twice reads as two numbers).
                kcol.html(kpi_grid_html([
                    (tr("home.cost_basis"), f"{sym}{cost * fx:,.0f}", None, None),
                    (
                        tr("home.market_value"),
                        f"{sym}{value * fx:,.0f}",
                        None,
                        None,
                    ),
                    (
                        tr("home.unrealised_pl"),
                        f"{sym}{(value - cost) * fx:+,.0f}",
                        kpi_delta_chip(gain_pct),
                        None,
                    ),
                    (
                        tr("home.realised_pl"),
                        f"{sym}{realized_gain * fx:+,.0f}",
                        kpi_delta_chip(
                            realized_gain / realized_cost if realized_cost else None
                        ),
                        tr("home.realised_pl_help"),
                    ),
                ]))

                # Delta row — Today / 1 week / 1 month, mirroring the Portfolio
                # page's second metric row, in the KPI column beside the
                # sparkline.
                # Regular session closed → "Today" comes from the per-row
                # day_eur (overridden to the live pre/after-hours quote, or the
                # last completed session once those windows shut), not the
                # basket's flat premarket 0%. Only a fully shut market greys
                # its delta ("off") — an extended-hours quote is live.
                mkt_open = us_market_open()
                extended = None if mkt_open else us_extended_session()
                today_closed = None
                if not mkt_open:
                    d_eur = tbl["day_eur"].dropna().sum()
                    base = value - d_eur
                    today_closed = (d_eur, d_eur / base if base else 0.0)
                delta_tiles = []
                for label, days in (
                    (tr("home.today"), 1),
                    (tr("home.one_week"), 7),
                    (tr("home.one_month"), 30),
                ):
                    chg = (
                        today_closed
                        if days == 1 and today_closed
                        else basket_change(hist, days)
                    )
                    if chg is None:
                        delta_tiles.append((label, tr("home.na"), None, None))
                    else:
                        delta_tiles.append((
                            label,
                            f"{sym}{chg[0] * fx:+,.0f}",
                            kpi_delta_chip(
                                chg[1],
                                fmt="{:+.2%}",
                                off=days == 1 and not mkt_open and not extended,
                            ),
                            None,
                        ))
                kcol.html(kpi_grid_html(delta_tiles))
                if not mkt_open:
                    st.caption(
                        tr(
                            f"home.{extended}market_note"
                            if extended
                            else "home.market_closed_note"
                        )
                    )
                # The sparkline slot (assigned above) is filled at the bottom of
                # the script — ledger_history's cold build fetches the ledger's
                # full price span, so the deferred-slot pattern keeps it off
                # this row's paint. basket_history would be wrong here — fixed
                # basket, not flow-adjusted, so it diverges from injected
                # capital around buys/sells.

            # Movers today: biggest day moves among open positions — the full
            # table lives on the Portfolio page. Positive/negative split, so a
            # ticker lands in at most one list. Own card, matching the glance
            # card above.
            movers = tbl["day_pct"].dropna()
            held_moves = movers.to_dict()  # fed to the Big-moves card below
            gainers = movers[movers > 0].nlargest(3)
            losers = movers[movers < 0].nsmallest(3)
            if not gainers.empty or not losers.empty:
                with st.container(border=True):
                    st.markdown(tr("home.movers_today"))
                    gcol, lcol = st.columns(2)

                    def _mover_table(box, series: pd.Series, label: str) -> None:
                        box.caption(label)
                        if series.empty:
                            box.caption(tr("home.none_today"))
                            return
                        idx = series.index
                        # Price per share in the currency the ticker trades in
                        # ($ for a US name, € for a European one) — a share
                        # price is quoted by its own market, unlike the EUR
                        # value/weight columns beside it. value_eur / shares
                        # rather than a second quote burst (value_eur is already
                        # live-priced), then divided back by the native->EUR
                        # rate it was built with. Unpriced rows read "n/a".
                        shares = tbl["shares"].reindex(idx).replace(0, float("nan"))
                        per_share = tbl["value_eur"].reindex(idx) / shares
                        ccys = [
                            "EUR" if pd.isna(c) or not c else str(c).upper()
                            for c in tbl["ccy"].reindex(idx)
                        ]
                        rates = native_eur_rates(tuple(sorted(set(ccys))))
                        price = [
                            _native_price(v, c, rates)
                            for v, c in zip(per_share, ccys, strict=True)
                        ]
                        box.html(
                            ticker_table_html(
                                pd.DataFrame(
                                    {
                                        "ticker": idx,
                                        "price": price,
                                        "day_pct": series.values,
                                        "value": tbl["value_eur"].reindex(idx).values
                                        * fx,
                                        "weight": tbl["weight"].reindex(idx).values,
                                    }
                                ),
                                fmt={**MOVER_FMT, "value": f"{sym}{{:,.0f}}"},
                                signed=("day_pct",),
                                # Price + its day move in one cell, the % as a
                                # tinted chip — same treatment as the Positions
                                # table's "€-97 (-1.1%)" cells.
                                pairs=(("price", "day_pct"),),
                                labels=MOVER_LABELS,
                                names=False,
                                muted={t for t in idx if not market_active(t)},
                                muted_cols=("day_pct",),
                                mobile={"value": "value", "delta": "day_pct",
                                        "sub": ("price", "weight")},
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

# No st.stop() on an empty watchlist: the deferred cards at the bottom must
# still fill for a portfolio-only user.
holdings = load_watchlist(auth.watchlist_path())

# ------------------------------------------------------------------ what's new
# Bordered cards, two per row on desktop (st.columns stack on phones). The
# earnings and 52-week cards only reserve containers here — their yfinance
# passes (slow on a cold cache) fill at the bottom of the script so the rest
# of the page paints first.
st.subheader(tr("home.whats_new"))

# Grid geometry, declared here because the skeleton below has to reserve the
# calendar's exact footprint; the grid itself is built at the bottom of the
# script (_mini_calendar_html) and its CSS carries the same cell height.
_MINI_CAL_WEEKS = 4
_MINI_CAL_COLS = 5  # weekdays only — equity prints never land on a weekend
_MINI_CAL_CELL = 62  # px; passed to calendar_css as cell_height, and sizes
                     # the loading skeleton so both stay one number

# Upcoming earnings — a compact 3-week calendar, the full-width centerpiece of
# this section. Reserve the slot here; its yfinance pass fills it at the bottom
# of the script so the rest of the page paints first (deferred-slot pattern).
# The shimmer is the grid at full size, so the cards below never shift when the
# real one drops in.
_earn_slot = skeletons.reserve(
    "calendar",
    border=True,
    title=True,
    weeks=_MINI_CAL_WEEKS,
    cols=_MINI_CAL_COLS,
    cell=_MINI_CAL_CELL,
)

# Ledger/price cards below, bordered and two per row on desktop (st.columns
# stack full-width on phones).
_r1a, _r1b = st.columns(2)
if txs:
    with _r1a, st.container(border=True, height="stretch"):
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
                mobile={"value": "amount_eur", "sub": ("date", "action")},
            )
        )
        st.page_link(
            "app_pages/import_transactions.py",
            label=tr("home.link_import"),
            icon=":material/upload_file:",
        )
    _xt_col = _r1b
else:
    _xt_col = _r1a
# Second deferred card: its 52-week scan is a 1y bulk download, so it too
# shimmers a table until the fill at the bottom of the script.
_xt_slot = skeletons.reserve(
    "table", container=_xt_col, border=True, title=True, rows=4, cols=3
)


# Defined above the refresh button so its clear() call can reference it; the
# actual 1y fetch still only runs in the deferred fill at the bottom.
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _year_extremes(
    tickers: tuple[str, ...],
) -> list[tuple[str, float, str, float | None]]:
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

# The watchlist close burst runs here rather than higher up so the two deferred
# cards above are already shimmering while it waits on Yahoo. A throttled Yahoo
# or dead network must dim/blank the watchlist rows, not crash the page. The
# miss isn't cached (st.cache_data skips exceptions), so a rerun retries; the
# toast tells the reader why the rows are blank.
try:
    closes = recent_closes(tuple(h.ticker for h in holdings)) if holdings else {}
except (YFRateLimitError, URLError) as exc:
    notices.data_toast(exc)
    closes = {}


def _watch_table(tickers: list[str]) -> None:
    """Ticker | last close | day % for one watchlist group."""
    # One row per ticker in the placeholder, so the expander opens at the
    # height it will keep once the off-session moves come back.
    with skeletons.slot("table", rows=len(tickers), cols=3) as box:
        # Outside the regular session the close-to-close day % can be a flat
        # premarket 0%, so pull the quote move (pre/after-hours, else the last
        # completed session) for those names.
        off = tuple(t for t in tickers if not market_live(t))
        moves = last_session_moves(off) if off else {}
        rows = []
        for t in tickers:
            c = closes.get(t) or []
            last = c[-1] if c else float("nan")
            day = (c[-1] / c[-2] - 1) if len(c) >= 2 and c[-2] else float("nan")
            day = moves.get(t, day)
            rows.append({"ticker": t, "price": last, "day_pct": day})
        box.container().html(
            ticker_table_html(
                pd.DataFrame(rows),
                fmt={"price": "{:,.2f}", "day_pct": "{:+.2%}"},
                signed=("day_pct",),
                labels={
                    "ticker": tr("home.col_ticker"),
                    "price": tr("home.col_last_close"),
                    "day_pct": tr("home.col_day_pct"),
                },
                muted={t for t in tickers if not market_active(t)},
                muted_cols=("day_pct",),
                mobile={"value": "price", "delta": "day_pct"},
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
    # The chart skeleton reserved up in the glance card is on screen for this
    # call; every path below either fills that slot or clears it.
    try:
        _hist, _, _ = ledger_history((len(txs), txs[-1].date, date.today()), DB)
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        _hist = pd.DataFrame()  # sparkline dropped, rest of the page stands
    except Exception:
        # Price-span build failed — drop the sparkline, keep the page.
        _spark_slot.container().warning(tr("home.data_unavailable"))
        _hist = pd.DataFrame()
    if not _hist.empty:
        # ffill BEFORE any range slice so both lines enter the window
        # continuous instead of starting on the first in-window quote.
        _hist = _hist.ffill()
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

        # Display window per range label, in calendar days (the ticker page's
        # P/E range convention: literal labels, no i18n).
        _SPARK_RANGES = {
            "1w": 7, "1m": 30, "6m": 182, "1y": 365, "2y": 730, "5y": 1825,
        }

        @st.fragment
        def _spark_chart() -> None:
            """Value-vs-injected line with its own range selector. A fragment,
            like the ticker page's valuation section: flipping the range
            redraws only this cell, not the whole page."""
            rng = (
                st.segmented_control(
                    tr("home.chart_value"),
                    list(_SPARK_RANGES),
                    default="1m",
                    key="home_spark_range",
                    label_visibility="collapsed",
                )
                or "1m"
            )
            win = _hist.loc[
                _hist.index
                >= _hist.index[-1] - pd.Timedelta(days=_SPARK_RANGES[rng])
            ]
            if len(win) < 2:
                win = _hist  # book younger than the window — show the full span
            _custom = [
                [inj, _pct_span(p), _spark_date(ts)]
                for ts, inj, p in zip(
                    win.index, win["injected_eur"], win["pnl_pct"], strict=True
                )
            ]
            fig = go.Figure()
            # Injected is the reference line: muted gray step (contributions
            # are steps, not slopes), visually secondary to the value line.
            fig.add_trace(
                go.Scatter(
                    x=win.index,
                    y=win["injected_eur"],
                    name=tr("home.chart_injected"),
                    line=dict(color=TEXT_MUTED, width=1.5, shape="hv", dash="dot"),
                    hoverinfo="skip",
                )
            )
            # Sign-split value line (same mask-overlap trick as the Portfolio
            # history chart): green above injected, red below; masks overlap one
            # point at each crossing so the segments stay connected.
            _gain = win["value_eur"] >= win["injected_eur"]
            _up = win["value_eur"].where(_gain | _gain.shift(1, fill_value=False))
            _down = win["value_eur"].where(
                ~_gain | (~_gain).shift(1, fill_value=False)
            )
            # Both traces share the "Valor" legend name; only one shows in the
            # legend (green wins when any point is in profit) — no duplicate.
            fig.add_trace(
                go.Scatter(
                    x=win.index,
                    y=_up,
                    name=tr("home.chart_value"),
                    line=dict(color=PROFIT_COLOR, width=2),
                    hoverinfo="skip",
                    showlegend=bool(_gain.any()),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=win.index,
                    y=_down,
                    name=tr("home.chart_value"),
                    line=dict(color=LOSS_COLOR, width=2),
                    hoverinfo="skip",
                    showlegend=not bool(_gain.any()),
                )
            )
            # Invisible full-coverage trace: one continuous tooltip regardless
            # of which colored segment is under the cursor (as on Portfolio).
            fig.add_trace(
                go.Scatter(
                    x=win.index,
                    y=win["value_eur"],
                    line=dict(width=0),
                    opacity=0,
                    showlegend=False,
                    customdata=_custom,
                    hovertemplate=tr("home.spark_hover_tmpl"),
                )
            )
            fig.update_layout(
                # Sized to fill the 30% column beside two KPI rows, minus the
                # range selector row above the plot.
                height=170 if not is_mobile() else 132,
                margin=dict(l=0, r=0, t=20, b=0),
                hovermode="x",
                legend=dict(
                    orientation="h", x=0, y=1.0, yanchor="bottom",
                    font=dict(size=10),
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
                # Transparent so the sparkline blends into its card surface
                # (same reason as show_chart), not the darker page paper.
                paper_bgcolor=TRANSPARENT,
                plot_bgcolor=TRANSPARENT,
            )
            st.plotly_chart(fig, config={"displayModeBar": False})

        with _spark_slot.container():
            _spark_chart()
    elif not _spark_slot.resolved:
        # Nothing to plot — a throttled fetch, or a book younger than the two
        # points a line needs. Drop the shimmer rather than leave it sweeping
        # over a cell that will never fill.
        _spark_slot.clear()


# --------------------------------------------- earnings calendar (slot fill)
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _cal_data(tickers: tuple[str, ...]):
    """(upcoming events, past reported results) for the calendar — one parallel
    yfinance pass, keyed by the ticker tuple so watchlist or portfolio edits
    invalidate it. Same data the full Earnings page uses; the mini-grid below
    only draws the days inside its window (over-fetching costs nothing —
    fetch_earnings pulls every date per ticker regardless)."""
    return calendar_events([Holding(t) for t in tickers])


# A compact 4-week grid: the previous week, this week, and the two following
# weeks, so recent prints sit beside what's coming. Only the five weekday
# columns show — equity earnings are weekday events, so a Sat/Sun column would
# sit permanently empty and just waste width. Upcoming chips are neutral (red
# when ≤7 days out); past prints are green/red beat/miss chips that open a
# result overview on click — same behaviour as the full calendar page.
# Five weekday columns in a narrow card — the shared grid, compact density.
_MINI_CAL_CSS = calendar_css(
    "mini",
    density="compact",
    cell_height=f"{_MINI_CAL_CELL}px",
    cell_width="20%",
)

# Clickable grid, declared once at module scope (never inside a function — the
# CCv2 registry keys on the name). Past chips carry data-ticker/data-date and
# click back to Python; upcoming chips carry neither and stay inert.
_mini_cal_grid = calendar_component("home_earnings_calendar", _MINI_CAL_CSS)


def _mini_chip(ev, names: dict[str, str], logos: dict[str, str | None]) -> str:
    """An upcoming-earnings chip — logo + symbol, inert (matching the full
    calendar, where only past prints are clickable). Red when ≤7 days out."""
    src = logos.get(ev.ticker)
    img = f'<img src="{html.escape(src, quote=True)}">' if src else ""
    soon = " soon" if ev.days_until is not None and ev.days_until <= 7 else ""
    title = html.escape(f"{ev.ticker} — {names.get(ev.ticker) or ev.ticker}")
    return (
        f'<div class="mini-chip{soon}" title="{title}">'
        f'{img}<span>{html.escape(ev.ticker)}</span></div>'
    )


def _mini_result_chip(r, names: dict[str, str], logos: dict[str, str | None]) -> str:
    """A past-print chip — green beat / red miss with a ▲/▼ arrow, carrying the
    data attributes the component wires to the result dialog on click."""
    src = logos.get(r.ticker)
    img = f'<img src="{html.escape(src, quote=True)}">' if src else ""
    verdict = "" if r.beat is None else (" beat" if r.beat else " miss")
    arrow = "" if r.beat is None else (" ▲" if r.beat else " ▼")
    bits = [f"{r.ticker} — {names.get(r.ticker) or r.ticker}"]
    if r.reported_eps is not None:
        est = (
            tr("earnings.chip_vs_est", est=f"{r.eps_estimate:.2f}")
            if r.eps_estimate is not None
            else ""
        )
        bits.append(f"EPS {r.reported_eps:.2f}{est}")
    if r.surprise_pct is not None:
        bits.append(f"{r.surprise_pct:+.1f}%")
    title = html.escape(" · ".join(bits) + tr("earnings.chip_click_details"))
    return (
        f'<div class="mini-chip past{verdict}" title="{title}"'
        f' data-ticker="{html.escape(r.ticker)}" data-date="{r.date.isoformat()}">'
        f'{img}<span>{html.escape(r.ticker)}{arrow}</span></div>'
    )


def _mini_calendar_html(start: date, by_date: dict[date, list],
                        res_by_date: dict[date, list], ref: date,
                        names: dict[str, str],
                        logos: dict[str, str | None]) -> str:
    """`_MINI_CAL_WEEKS` × 5 weekdays as an HTML table. `start` is a Monday; each
    cell holds that day's past-result chips then upcoming chips, with today
    highlighted and past days dimmed."""
    head = "<tr>" + "".join(f"<th>{tr(f'home.wd_{i}')}</th>" for i in range(5)) + "</tr>"
    rows = []
    for week in range(_MINI_CAL_WEEKS):
        cells = []
        for wd in range(5):
            day = start + timedelta(days=week * 7 + wd)
            cls = "today" if day == ref else ("dim" if day < ref else "")
            cls_attr = f' class="{cls}"' if cls else ""
            chips = "".join(
                _mini_result_chip(r, names, logos) for r in res_by_date.get(day, [])
            )
            chips += "".join(_mini_chip(e, names, logos) for e in by_date.get(day, []))
            cells.append(
                f'<td{cls_attr}><div class="mini-daynum">{day.day}</div>{chips}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="mini-cal">{head}{"".join(rows)}</table>'


# Portfolio + favorites + tagged tickers; neither coins nor funds report
# earnings (fund classification is cache-only, see stocks.data.funds).
_tagged = {t for ts in tag_groups.values() for t in ts}
_earn_tickers = tuple(
    sorted(
        t
        for t in _held | set(favs) | _tagged
        if not is_crypto(t) and not is_fund(t, fetch=False)
    )
)

if not _earn_tickers:
    # Nothing to fetch (no stocks held or starred) — the card never comes,
    # so retire its placeholder instead of shimmering forever.
    _earn_slot.clear()
else:
    with _earn_slot.container(border=True, height="stretch"):
        st.markdown(tr("home.earnings_upcoming"))
        try:
            _events, _results = _cal_data(_earn_tickers)
        except (YFRateLimitError, URLError) as exc:
            notices.data_toast(exc)
            _events = _results = None  # card shows its unavailable stub
        except Exception:
            # Non-banner failure (payload/parser change): stub the card and
            # keep the page — the miss isn't cached, so a rerun retries.
            _events = _results = None
        if _events is None:
            st.caption(tr("home.earnings_unavailable"))
        else:
            _today = date.today()
            # Window: the previous week's Monday through the last shown Friday.
            _cal_start = _today - timedelta(days=_today.weekday() + 7)
            _cal_end = _cal_start + timedelta(days=_MINI_CAL_WEEKS * 7 - 1)
            # Upcoming (one next-date per ticker) and past prints, grouped by
            # day — weekdays only (the grid drops Sat/Sun; equity prints never
            # land there).
            _by_date: dict[date, list] = {}
            for _e in _events:
                if (_e.date and _cal_start <= _e.date <= _cal_end
                        and _e.date.weekday() < 5):
                    _by_date.setdefault(_e.date, []).append(_e)
            _res_by_date: dict[date, list] = {}
            for _r in _results:
                if _cal_start <= _r.date <= _cal_end and _r.date.weekday() < 5:
                    _res_by_date.setdefault(_r.date, []).append(_r)
            _syms = {x.ticker for row in _by_date.values() for x in row} | {
                x.ticker for row in _res_by_date.values() for x in row
            }
            if _syms:
                _names = {t: (company_name(t) or t) for t in _syms}
                _logos = {t: logo(t) for t in _syms}

                @st.dialog(tr("earnings.dialog_title"), width="large")
                def _mini_result_dialog(ticker: str, iso: str) -> None:
                    render_result_body(ticker, iso, _results, _names, _logos)

                _grid = _mini_cal_grid(
                    data={"html": _mini_calendar_html(
                        _cal_start, _by_date, _res_by_date, _today, _names, _logos
                    )},
                    key="home_earn_cal",
                    on_pick_change=lambda: None,
                )
                if _grid.pick:
                    _mini_result_dialog(_grid.pick["ticker"], _grid.pick["date"])
                st.caption(tr("home.earnings_3w_caption"))
            else:
                st.caption(tr("home.no_reports_3w"))
        st.page_link(
            "app_pages/earnings.py",
            label=tr("home.link_earnings_calendar"),
            icon=":material/calendar_month:",
        )

# --------------------------------------------- 52-week extremes (slot fill)
# Held + favorites only, so the 1y bulk fetch stays small; crypto pairs have
# no meaningful 52-week narrative here and are skipped.
_xt_tickers = tuple(sorted(t for t in _held | set(favs) if not is_crypto(t)))

if not _xt_tickers:
    _xt_slot.clear()  # nothing to scan — retire the placeholder
else:
    with _xt_slot.container(border=True):
        st.markdown(tr("home.extremes_52w"))
        try:
            _extremes = _year_extremes(_xt_tickers)
        except (YFRateLimitError, URLError) as exc:
            notices.data_toast(exc)
            _extremes = None  # card shows its unavailable stub
        except Exception:
            # None ≠ [] — an empty scan means "nothing at extremes", a failed
            # one shows the unavailable stub; the miss isn't cached, so a
            # rerun retries.
            _extremes = None
        if _extremes is None:
            st.caption(tr("home.extremes_unavailable"))
        elif _extremes:

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
                    mobile={"value": "price", "sub": ("where",)},
                )
            )
        else:
            st.caption(tr("home.no_extremes"))
