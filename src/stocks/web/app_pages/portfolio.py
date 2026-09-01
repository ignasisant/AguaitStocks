"""Portfolio page — driven entirely by the transaction ledger (data/portfolio.db).

Upload a Revolut CSV on the Import page first; everything here derives from
those transactions via FIFO: open positions & P/L, allocation & risk, realized
gains & Spanish tax, and dividends. Weights are put on an EUR footing so a
multi-currency book (USD + EUR) is measured consistently.

Tabs are dynamic (on_change="rerun"): only the visible tab's content runs, so
the price/profile fetch behind "Allocation & risk" doesn't block the others.
The Positions tab's two sections are parallel fragments, so the live-price
table and the ledger-history chart load concurrently on a full rerun.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from urllib.error import URLError

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.analysis.portfolio import (
    analyze,
    annualized_return,
    annualized_volatility,
    basket_change,
    correlation_matrix,
    cumulative_returns,
    effective_positions,
    flow_series,
    holdings_from_positions,
    market_active,
    market_value_weights_eur,
    max_drawdown,
    money_weighted_return,
    portfolio_returns,
    top_n_weight,
    us_extended_session,
    us_market_open,
)
from stocks.portfolio import dividends, fees
from stocks.portfolio.tax_es import fiscal_year, modelo_720_flag
from stocks.web import auth, notices, skeletons
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import (
    basket_history,
    enriched_positions,
    eur_spot,
    ledger_history,
    ledger_state,
    positions_table,
    trade_bars,
)
from stocks.web.widgets import (
    BORDER,
    CANDLE_DOWN,
    CANDLE_UP,
    CATEGORICAL_COLORS,
    DIVERGING_SCALE,
    EVENT_LINE,
    INFO_COLOR,
    LOSS_BAND,
    LOSS_COLOR,
    PROFIT_BAND,
    PROFIT_COLOR,
    SEQUENTIAL_SCALE,
    SURFACE_CARD,
    SURFACE_PAGE,
    SURFACE_SUNKEN,
    TEXT_FAINT,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TRANSPARENT,
    WARN_ORANGE,
    chart_layout,
    data_table,
    db_mtime,
    is_mobile,
    kpi_delta_chip,
    kpi_grid_html,
    responsive_ticker_table_html,
    show_chart,
    ticker_table_html,
)

_MOBILE = is_mobile()

# Everything below derives from the personal transaction ledger.
auth.require_login()

st.title(tr("nav.portfolio"))

# This session user's ledger; with its mtime it keys every ledger-derived
# cache (web/portfolio_data.py) so concurrent users never read each other's
# book and entries stay hot until the next import.
DB = str(auth.db_path())

txs, positions, realized = ledger_state(DB, db_mtime(DB))
if not txs:
    st.warning(tr("portfolio.no_transactions"))
    st.stop()
if not positions and not realized:
    st.warning(tr("portfolio.ledger_no_positions"))
    st.stop()

# The active tab rides the URL (?tab=slug) so a reload — or a shared link —
# lands on the same tab. Slugs, not labels: labels are localized and would
# break bookmarks across language switches.
_TAB_SLUGS = ("positions", "risk", "tax", "dividends", "fees")
_TAB_LABELS = [tr("portfolio.tab_positions"), tr("portfolio.tab_alloc_risk"),
               tr("portfolio.tab_realized_tax"), tr("portfolio.tab_dividends"),
               tr("portfolio.tab_fees")]


def _tab_to_url() -> None:
    label = st.session_state.get("portfolio_tab")
    if label in _TAB_LABELS:
        st.query_params["tab"] = _TAB_SLUGS[_TAB_LABELS.index(label)]


_qp_tab = st.query_params.get("tab")
tab_pos, tab_risk, tab_tax, tab_div, tab_fees = st.tabs(
    _TAB_LABELS,
    default=(_TAB_LABELS[_TAB_SLUGS.index(_qp_tab)]
             if _qp_tab in _TAB_SLUGS else None),
    key="portfolio_tab",
    on_change=_tab_to_url,
)


# ------------------------------------------------------------------- Positions
# Both sections are parallel fragments: a full rerun dispatches each to a
# thread pool, so the live-price table and the ledger-history chart build
# concurrently instead of back to back.
@st.fragment(parallel=True)
def _positions_section() -> None:
    # The card is drawn twice: first as a shimmer of the two KPI rows and the
    # positions table, then — same bordered box, same height — refilled with the
    # real figures. The two fragments below run in parallel, so without this the
    # tab would pop into place a section at a time.
    card = skeletons.reserve("metrics", border=True, title=True, n=(4, 3))
    # Shared loader (web/portfolio_data.py) — already weight/day-enriched and
    # weight-sorted; Home shows the same frame, so the price burst is shared.
    try:
        tbl = enriched_positions(DB, db_mtime(DB))
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        card.clear()  # the toast is the whole message; no empty card behind it
        return
    except Exception:
        card.container(border=True).warning(tr("portfolio.data_unavailable"))
        return
    with card.container(border=True):
        st.subheader(tr("portfolio.open_positions_pl"))
        if tbl.empty:
            st.caption(tr("portfolio.no_open_positions"))
            # Empty book = the next step is importing one; say so in place
            # instead of leaving a dead end.
            st.page_link(
                "app_pages/import_transactions.py",
                label=tr("nav.import"),
                icon=":material/upload_file:",
            )
        else:
            cost = tbl["cost_eur"].sum()
            value = tbl["value_eur"].dropna().sum()
            # Headline figures honor the profile's display-currency preference
            # (spot-converted); the table and tax tabs stay EUR.
            ccy = auth.display_currency()
            fx = 1.0 if ccy == "EUR" else eur_spot(ccy)
            if fx is None:
                ccy, fx = "EUR", 1.0
            sym = auth.CURRENCY_SYMBOL[ccy]
            realized_gain = sum(s.gain_eur for s in realized)
            realized_cost = sum(s.cost_eur for s in realized)
            # Same TIKR-style tiles as the Ticker fundamentals card (and the
            # Home glance): value and its chip on one line, help as a "?" pill.
            st.html(kpi_grid_html([
                (tr("portfolio.cost_basis"), f"{sym}{cost * fx:,.0f}", None, None),
                (tr("portfolio.market_value"), f"{sym}{value * fx:,.0f}", None, None),
                (
                    tr("portfolio.unrealised_pl"),
                    f"{sym}{(value - cost) * fx:+,.0f}",
                    kpi_delta_chip(value / cost - 1 if cost else None),
                    None,
                ),
                (
                    tr("portfolio.realised_pl"),
                    f"{sym}{realized_gain * fx:+,.0f}",
                    kpi_delta_chip(
                        realized_gain / realized_cost if realized_cost else None
                    ),
                    tr("portfolio.realised_pl_help"),
                ),
            ]))
            if ccy != "EUR":
                st.caption(tr("portfolio.headline_converted", ccy=ccy))

            try:
                vals = basket_history(DB, db_mtime(DB))
            except (YFRateLimitError, URLError) as exc:
                notices.data_toast(exc)
                vals = pd.DataFrame()  # 1w/1m read n/a; the table below stands
            except Exception:
                # Degrade: 1w/1m read n/a; the positions table below still renders.
                st.warning(tr("portfolio.data_unavailable"))
                vals = pd.DataFrame()
            # Regular session closed → sum the per-row day_eur (already overridden
            # to the live pre/after-hours quote, or the last completed session once
            # those windows shut) instead of the basket's close-to-close, which can
            # be a flat premarket 0%. Grey its delta ("off") only when nothing is
            # trading — an extended-hours quote is live.
            mkt_open = us_market_open()
            extended = None if mkt_open else us_extended_session()
            today_closed = None
            if not mkt_open:
                d_eur = tbl["day_eur"].dropna().sum()
                base = value - d_eur
                today_closed = (d_eur, d_eur / base if base else 0.0)
            delta_tiles = []
            for label, days in (
                (tr("portfolio.today"), 1),
                (tr("portfolio.one_week"), 7),
                (tr("portfolio.one_month"), 30),
            ):
                chg = (today_closed if days == 1 and today_closed
                       else basket_change(vals, days))
                if chg is None:
                    delta_tiles.append((label, tr("portfolio.na"), None, None))
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
            st.html(kpi_grid_html(delta_tiles))
            if not mkt_open:
                st.caption(
                    tr(
                        f"portfolio.{extended}market_note"
                        if extended
                        else "portfolio.market_closed_note"
                    )
                )

            # Shared Positions-style HTML table (logo+name cells, semantic P/L
            # colors — see widgets.ticker_table_html). Rows come pre-sorted by
            # weight; click-to-sort is the only capability given up.
            tbl.insert(0, "ticker", tbl.index)
            tbl = tbl[
                ["ticker", "shares", "ccy", "cost_eur", "value_eur", "weight",
                 "day_eur", "day_pct", "pnl_eur", "pnl_pct"]
            ]

            fmt = {
                "shares": "{:.4f}",
                "cost_eur": "€{:,.0f}",
                "value_eur": "€{:,.0f}",
                "weight": "{:.1%}",
                "day_eur": "€{:+,.0f}",
                "day_pct": "{:+.1%}",
                "pnl_eur": "€{:,.0f}",
                "pnl_pct": "{:+.1%}",
            }
            pnl_cols = ("day_eur", "day_pct", "pnl_eur", "pnl_pct")
            # Rows with no live quote: dim only the day columns (a last-close move,
            # not live); total P/L stays full color. Crypto is 24/7, and a US name
            # in pre/after-hours is live, so neither dims.
            muted = {t for t in tbl["ticker"] if not market_active(t)}
            day_cols = ("day_eur", "day_pct")
            # € and % of the same move belong in one cell: "€-97  (-1.1%)",
            # the percentage as a tinted pill. Halves the desktop column count.
            pairs = (("day_eur", "day_pct"), ("pnl_eur", "pnl_pct"))
            labels = {
                "ticker": tr("portfolio.col_position"),
                "shares": tr("portfolio.col_shares"),
                "ccy": tr("portfolio.col_currency"),
                "cost_eur": tr("portfolio.cost_basis"),
                "value_eur": tr("portfolio.market_value"),
                "weight": tr("portfolio.col_weight"),
                "day_eur": tr("portfolio.today"),
                "pnl_eur": tr("portfolio.col_total_pl"),
            }

            if _MOBILE:
                # Dense Revolut-style rows — value + day% on the right, total
                # P/L as a pill beside the symbol (the dim line ellipsizes, so a
                # number there got cut), weight below; nothing pans
                # horizontally. Full sortable table one expander below.
                st.html(ticker_table_html(
                    tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols,
                    labels=labels,
                    mobile={"value": "value_eur", "delta": "day_pct",
                            "badge": "pnl_pct", "sub": ("weight",)}))
                with st.expander(tr("portfolio.all_columns")):
                    st.html(ticker_table_html(
                        tbl, fmt=fmt, signed=pnl_cols, muted=muted,
                        muted_cols=day_cols, pairs=pairs, labels=labels,
                        sortable="positions"))
            else:
                st.html(ticker_table_html(
                    tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols,
                    pairs=pairs, labels=labels, sortable="positions"))
            st.caption(tr("portfolio.positions_caption"))


def _alloc_pie(alloc: pd.Series, title: str) -> go.Figure:
    """Allocation donut in the DS categorical palette.

    Percentages ride the legend entries instead of the slices — sliver
    slices under ~1% used to print their labels on top of each other. More
    buckets than palette hues folds the tail into one muted "Others" slice
    (fixed hue order, never cycled).
    """
    pct = (alloc / alloc.sum() * 100).sort_values(ascending=False)
    colors = list(CATEGORICAL_COLORS)
    n = len(colors)
    if len(pct) > n:
        pct = pd.concat([
            pct.iloc[:n - 1],
            pd.Series({tr("portfolio.alloc_other"): pct.iloc[n - 1:].sum()}),
        ])
        colors[-1] = TEXT_FAINT
    fig = go.Figure(
        go.Pie(
            labels=[f"{name} · {v:.1f}%" for name, v in pct.items()],
            values=pct.values,
            hole=0.45,
            sort=False,
            textinfo="none",
            marker=dict(
                colors=colors[:len(pct)],
                # 2px surface gap so adjacent fills never touch.
                line=dict(color=SURFACE_CARD, width=2),
            ),
            hovertemplate="<b>%{label}</b><extra></extra>",
        )
    )
    fig.update_layout(
        **chart_layout(title=title, height=300),
        legend=dict(font=dict(size=12, color=TEXT_SECONDARY)),
    )
    return fig


# The Geography allocation cell offers two views — the classic donut and a
# rotatable orthographic globe. Its own fragment so flipping the toggle
# repaints this one cell instead of rerunning the whole history section. The
# chart draws from session state and the toggle renders below it, keeping the
# three allocation charts top-aligned across their columns.
@st.fragment
def _geography_cell(alloc: pd.Series, title: str) -> None:
    view = st.session_state.get("geo_alloc_view", "map")
    if view == "map":
        pct = alloc[alloc > 0] / alloc.sum() * 100
        # "Unknown" is allocation()'s bucket for tickers with no country in
        # meta; it has no polygon, so it leaves the map for the caption below.
        mapped = pct.drop("Unknown", errors="ignore")
        fig = go.Figure(
            go.Choropleth(
                locations=list(mapped.index),
                locationmode="country names",
                # Log-spaced color: a 70% home market would otherwise pin
                # every other holding onto the ramp's first step.
                z=[math.log10(v) for v in mapped.values],
                customdata=list(mapped.values),
                hovertemplate=(
                    "<b>%{location}</b><br>%{customdata:.1f}%<extra></extra>"),
                colorscale=SEQUENTIAL_SCALE,
                showscale=False,
                marker_line_color=BORDER,
                marker_line_width=0.5,
            )
        )
        fig.update_layout(
            **chart_layout(title=title, height=300),
            geo=dict(
                projection_type="orthographic",
                # Start over the Atlantic: US and Europe (the usual bulk of
                # the book) both on the visible hemisphere.
                projection_rotation=dict(lon=-40, lat=25),
                bgcolor=TRANSPARENT,
                # The sphere must read as a circle: hairline frame around
                # the disc, ocean one step darker than the card behind it,
                # land lifted slightly off the ocean.
                showframe=True,
                framecolor=BORDER,
                framewidth=1,
                showcoastlines=False,
                showland=True,
                landcolor=SURFACE_SUNKEN,
                showocean=True,
                oceancolor=SURFACE_PAGE,
                showcountries=True,
                countrycolor=BORDER,
            ),
        )
        # Default dragmode is "zoom" (a rectangle select on geo axes); "pan"
        # is what spins an orthographic globe under the pointer. Mobile keeps
        # drag off entirely — a rotating globe would swallow page scrolling.
        fig.update_layout(dragmode=False if _MOBILE else "pan")
        show_chart(fig, key="alloc_geo")
        unmapped = 100 - mapped.sum()
        if unmapped >= 0.05:
            st.caption(tr("portfolio.geo_unmapped", pct=f"{unmapped:.1f}"))
    else:
        show_chart(_alloc_pie(alloc, title), key="alloc_geo")
    st.segmented_control(
        title,
        options=("map", "chart"),
        default="map",
        format_func=lambda v: (
            f":material/public: {tr('portfolio.geo_view_map')}" if v == "map"
            else f":material/donut_small: {tr('portfolio.geo_view_chart')}"
        ),
        key="geo_alloc_view",
        label_visibility="collapsed",
        required=True,
    )


@st.fragment(parallel=True)
def _history_section() -> None:
    # Full-span price history: the slowest fetch on the page on a cold cache.
    # Reserve the chart's exact height so the tab doesn't grow by 380px under
    # the reader when it lands.
    card = skeletons.reserve(
        "chart", border=True, title=True, height=380, legend=True
    )
    try:
        hist, _, missing = ledger_history(
            (len(txs), txs[-1].date, date.today()), DB
        )
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        card.clear()
        return
    except Exception:
        card.container(border=True).warning(tr("portfolio.data_unavailable"))
        return
    with card.container(border=True):
        st.subheader(tr("portfolio.injected_vs_value"))
        if hist.empty:
            st.caption(tr("portfolio.not_enough_history"))
        else:
            # Filter in Python (not plotly rangeselector) so the y-axis rescales
            # to the visible window.
            SPANS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}
            # "All" is both a display label and the default/comparison key, so bind
            # the translated label to a variable; "YTD" and the SPANS codes stay
            # untranslated (they're compared/keyed against the raw values).
            ALL = tr("portfolio.range_all")
            sel = st.segmented_control(
                tr("portfolio.range"),
                [*SPANS, "YTD", ALL],
                default=ALL,
                key="hist_range",
                label_visibility="collapsed",
            )
            sel = sel or ALL  # segmented_control returns None if cleared.
            if sel in SPANS:
                cutoff = hist.index.max() - pd.Timedelta(days=SPANS[sel])
                hist = hist[hist.index >= cutoff]
            elif sel == "YTD":
                hist = hist[hist.index >= pd.Timestamp(date.today().year, 1, 1)]

            GREEN, RED = PROFIT_COLOR, LOSS_COLOR

            def _pct_span(p: float) -> str:
                if pd.isna(p):
                    return "—"
                color = GREEN if p >= 0 else RED
                return f'<span style="color:{color}"><b>{p:+.1%}</b></span>'

            customdata = [
                [inj, _pct_span(p)]
                for inj, p in zip(hist["injected_eur"], hist["pnl_pct"], strict=True)
            ]
            # Split the value line by sign of P/L; masks overlap one point at
            # each crossing so the green and red segments stay connected.
            gain = hist["value_eur"] >= hist["injected_eur"]
            up = hist["value_eur"].where(gain | gain.shift(1, fill_value=False))
            down = hist["value_eur"].where(~gain | (~gain).shift(1, fill_value=False))
            GREEN_FILL, RED_FILL = PROFIT_BAND, LOSS_BAND

            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist["injected_eur"],
                    name=tr("portfolio.series_injected"),
                    line=dict(color=TEXT_MUTED, width=2, shape="hv"),
                    hoverinfo="skip",
                )
            )
            # Green band where value ≥ injected …
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist[["value_eur", "injected_eur"]].max(axis=1),
                    line=dict(width=0), fill="tonexty", fillcolor=GREEN_FILL,
                    hoverinfo="skip", showlegend=False,
                )
            )
            # … re-anchor on injected, then red band where value < injected.
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist["injected_eur"],
                    line=dict(width=0), hoverinfo="skip", showlegend=False,
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist[["value_eur", "injected_eur"]].min(axis=1),
                    line=dict(width=0), fill="tonexty", fillcolor=RED_FILL,
                    hoverinfo="skip", showlegend=False,
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=up, name=tr("portfolio.series_value_profit"),
                    line=dict(color=GREEN, width=2),
                    hoverinfo="skip", showlegend=bool(gain.any()),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=down, name=tr("portfolio.series_value_loss"),
                    line=dict(color=RED, width=2),
                    hoverinfo="skip", showlegend=bool((~gain).any()),
                )
            )
            # Invisible full-coverage trace: one tooltip that works everywhere,
            # regardless of which colored segment is under the cursor.
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist["value_eur"],
                    line=dict(width=0), opacity=0, showlegend=False,
                    customdata=customdata,
                    hovertemplate=tr("portfolio.hist_hover_tmpl"),
                )
            )
            fig.update_layout(
                **chart_layout(height=380, top_legend=True),
                hovermode="x",
                yaxis=dict(title="EUR", fixedrange=True),
            )
            fig.update_xaxes(
                showspikes=True, spikemode="across", spikethickness=1,
                spikecolor=EVENT_LINE, spikedash="dot",
            )
            if _MOBILE:
                # DS mobile chart spec: ~3 date labels on a phone.
                fig.update_xaxes(nticks=3)
            show_chart(fig)
            notes = [tr("portfolio.hist_note_injected")]
            if missing:
                notes.append(
                    tr("portfolio.hist_note_missing", tickers=", ".join(missing))
                )
            st.caption(" ".join(notes))


if tab_pos.open:
    with tab_pos:
        _positions_section()
        _history_section()

# ----------------------------------------------------------- Allocation & risk
if tab_risk.open:
    with tab_risk:
        if not positions:
            st.caption(tr("portfolio.no_positions_analyse"))
        else:
            FROM_START = tr("portfolio.from_start")
            choice = st.selectbox(
                tr("portfolio.return_window"),
                [FROM_START, "6mo", "1y", "2y", "5y"], index=0,
            )
            tickers = tuple(p.ticker for p in positions)
            first_tx = min(t.date for t in txs)
            _WINDOW_DAYS = {"6mo": 182, "1y": 365, "2y": 730, "5y": 1826}
            win_start = (
                pd.Timestamp(first_tx)
                if choice == FROM_START
                else pd.Timestamp(date.today())
                - pd.Timedelta(days=_WINDOW_DAYS[choice])
            )
            if choice == FROM_START:
                # Fetch enough history to cover the ledger, then clip below so
                # metrics start at the first transaction, not the stock's IPO.
                span = (date.today() - date.fromisoformat(first_tx)).days
                period = (
                    "1y" if span <= 360
                    else "2y" if span <= 700
                    else "5y" if span <= 1780
                    else "max"
                )
            else:
                period = choice

            def _pct(x: float) -> str:
                return "—" if pd.isna(x) else f"{x * 100:.1f}%"

            # ---- Real performance: the ledger TWR path in EUR — sold
            # positions and dividends included, deposits/withdrawals excluded
            # from the return itself (they only move the money-weighted view).
            with st.container(border=True):
                st.subheader(tr("portfolio.real_perf"))
                perf_kpis = skeletons.reserve("metrics", n=4)
                twr = None
                try:
                    hist, twr, _ = ledger_history(
                        (len(txs), txs[-1].date, date.today()), DB
                    )
                except (YFRateLimitError, URLError) as exc:
                    notices.data_toast(exc)
                    perf_kpis.clear()
                except Exception:
                    perf_kpis.container().warning(tr("portfolio.report_failed"))
                if twr is not None:
                    twr_win = twr[twr.index >= win_start]
                    mwr = (
                        money_weighted_return(
                            hist["value_eur"], flow_series(txs), start=win_start
                        )
                        if not hist.empty
                        else float("nan")
                    )
                    with perf_kpis.container():
                        st.html(kpi_grid_html([
                            (tr("portfolio.annualised_return"),
                             _pct(annualized_return(twr_win)),
                             None,
                             tr("portfolio.twr_return_help")),
                            (tr("portfolio.mwr"),
                             _pct(mwr),
                             None,
                             tr("portfolio.mwr_help")),
                            (tr("portfolio.annualised_vol"),
                             _pct(annualized_volatility(twr_win)),
                             None,
                             tr("portfolio.twr_vol_help")),
                            (tr("portfolio.max_drawdown"),
                             _pct(max_drawdown(cumulative_returns(twr_win) + 1)),
                             None,
                             tr("portfolio.twr_dd_help")),
                        ]))
                        st.caption(tr("portfolio.real_perf_note"))

            # ---- Current basket risk: today's holdings backtested at fixed
            # EUR weights over the window — a risk profile of what you hold
            # now, not a performance figure (that's the card above).
            with st.container(border=True):
                st.subheader(tr("portfolio.basket_risk"))

                # (db, mtime) must be arguments: st.cache_data keys on arguments
                # only, and the cache is shared across sessions — reading the
                # session's positions through the closure would serve one user's
                # report to another whose ticker tuple matches.
                @st.cache_data(ttl=3600, show_spinner=False)
                def _report(period: str, tickers: tuple[str, ...], db: str, mtime: float):
                    pos = ledger_state(db, mtime)[1]
                    held = [p for p in pos if p.ticker in tickers]
                    holds = holdings_from_positions(held)
                    return analyze(period=period, holdings=holds)

                # Prices + company profiles for every held name — the fetch that
                # gates the rest of this tab. The four risk KPIs shimmer while it
                # runs, so changing the window doesn't blank the card.
                risk_kpis = skeletons.reserve("metrics", n=4)
                try:
                    rep = _report(period, tickers, DB, db_mtime(DB))
                except (YFRateLimitError, URLError) as exc:
                    notices.data_toast(exc)
                    risk_kpis.clear()
                    rep = None
                except Exception:
                    risk_kpis.container().warning(tr("portfolio.report_failed"))
                    rep = None
                if rep is not None:
                    if choice == FROM_START:
                        start = pd.Timestamp(first_tx)

                        def _since(obj):
                            idx = obj.index
                            if getattr(idx, "tz", None) is not None:
                                idx = idx.tz_localize(None)
                            return obj[idx >= start]

                        rep.returns = _since(rep.returns)
                        rep.bench_returns = {
                            b: _since(r) for b, r in rep.bench_returns.items()}
                    # Put weights on an EUR footing, then rebuild the portfolio
                    # return series.
                    rep.weights = market_value_weights_eur(
                        positions, rep.prices, rep.meta)
                    rep.port_returns = portfolio_returns(rep.returns, rep.weights)

                    with risk_kpis.container():
                        st.html(kpi_grid_html([
                            (tr("portfolio.annualised_vol"),
                             _pct(rep.volatility),
                             None,
                             tr("portfolio.basket_vol_help")),
                            (tr("portfolio.effective_names"),
                             f"{effective_positions(rep.weights):.1f}",
                             None,
                             tr("portfolio.effective_names_help")),
                            (tr("portfolio.top5"),
                             f"{top_n_weight(rep.weights, 5) * 100:.0f}%",
                             None,
                             tr("portfolio.top5_help")),
                            (tr("portfolio.max_drawdown"),
                             _pct(rep.max_drawdown),
                             None,
                             tr("portfolio.basket_dd_help")),
                        ]))

                        betas = " · ".join(
                            f"β {b} {rep.beta_vs(b):.2f}" for b in rep.bench_returns)
                        if betas:
                            st.caption(
                                tr("portfolio.basket_beta_caption", betas=betas))

            if rep is not None:
                with st.container(border=True):
                    st.subheader(tr("portfolio.allocation"))
                    cols = st.columns(3)
                    for col, key, title in zip(
                        cols, ("sector", "country", "currency"),
                        (tr("portfolio.alloc_sector"), tr("portfolio.alloc_geography"),
                         tr("portfolio.alloc_currency")),
                        strict=True,
                    ):
                        alloc = rep.allocation(key)
                        if alloc.empty:
                            col.caption(tr("portfolio.no_alloc_data", kind=title.lower()))
                            continue
                        if key == "country":
                            with col:
                                _geography_cell(alloc, title)
                            continue
                        show_chart(_alloc_pie(alloc, title), container=col)

                # Same full-span history the Positions tab builds, and cold
                # whenever the reader opens this tab first — so the card holds
                # its 420px chart as a shimmer rather than appearing late and
                # shoving the correlation matrix down the page.
                cum_card = skeletons.reserve(
                    "chart", border=True, title=True, height=420, legend=True
                )
                try:
                    _, twr, twr_missing = ledger_history(
                        (len(txs), txs[-1].date, date.today()), DB
                    )
                except (YFRateLimitError, URLError) as exc:
                    notices.data_toast(exc)
                    cum_card.clear()  # else-block skipped: no chart
                except Exception:
                    cum_card.container(border=True).warning(
                        tr("portfolio.data_unavailable"))
                else:
                    with cum_card.container(border=True):
                        st.subheader(tr("portfolio.cumulative_return"))
                        # Clip the ledger TWR to the window so it rebases with
                        # the benchmarks.
                        win_start = rep.returns.index[0]
                        if getattr(win_start, "tz", None) is not None:
                            win_start = win_start.tz_localize(None)
                        twr_win = twr[twr.index >= win_start.normalize()]

                        line = go.Figure()
                        if not twr_win.empty:
                            twr_cum = cumulative_returns(twr_win)
                            line.add_trace(
                                go.Scatter(
                                    x=twr_cum.index, y=twr_cum * 100,
                                    name=tr("portfolio.series_portfolio_twr"),
                                    hovertemplate=tr("portfolio.hover_portfolio_twr"),
                                )
                            )
                        basket_cum = cumulative_returns(rep.port_returns)
                        line.add_trace(
                            go.Scatter(
                                x=basket_cum.index, y=basket_cum * 100,
                                name=tr("portfolio.series_current_basket"),
                                line=dict(dash="dot"),
                                hovertemplate=tr("portfolio.hover_current_basket"),
                            )
                        )
                        for b, r in rep.bench_returns.items():
                            cum = cumulative_returns(r)
                            line.add_trace(
                                go.Scatter(
                                    x=cum.index, y=cum * 100, name=b,
                                    hovertemplate=(
                                        f"{b}  <b>%{{y:+.1f}}%</b><extra></extra>"),
                                )
                            )
                        # One box per date comparing portfolio, basket and
                        # every benchmark.
                        line.update_layout(
                            **chart_layout(height=420, top_legend=True),
                            hovermode="x unified",
                            yaxis=dict(title="%", fixedrange=True),
                        )
                        show_chart(line)
                        notes = [tr("portfolio.twr_note")]
                        if twr_missing:
                            notes.append(
                                tr("portfolio.twr_note_missing",
                                   tickers=", ".join(twr_missing))
                            )
                        st.caption(" ".join(notes))

                with st.container(border=True):
                    st.subheader(tr("portfolio.return_correlation"))
                    corr = correlation_matrix(rep.returns)
                    if not corr.empty:
                        heat = go.Figure(
                            go.Heatmap(
                                z=corr.values, x=corr.columns, y=corr.index,
                                zmin=-1, zmax=1, colorscale=DIVERGING_SCALE,
                                hovertemplate=tr("portfolio.hover_correlation"),
                            )
                        )
                        # ≥24px per row so ticker labels never collide; headroom covers
                        # the auto-expanding tick-label margins.
                        heat.update_layout(
                            height=max(400, 24 * len(corr) + 120),
                            margin=dict(l=0, r=0, t=10, b=0),
                        )
                        show_chart(heat)

# --------------------------------------------------------------- Realized & tax
if tab_tax.open:
    with tab_tax:
        sell_years = sorted({int(s.sell_date[:4]) for s in realized}, reverse=True)
        if not sell_years:
            st.caption(tr("portfolio.no_realized_sales"))
        else:
            buy_dates: dict[str, list[str]] = defaultdict(list)
            for t in txs:
                if t.action == "buy":
                    buy_dates[t.ticker].append(t.date)
            year_ty = {
                y: fiscal_year(realized, y, buy_dates)
                for y in sorted(sell_years)
            }

            # Year-by-year picture first: what each ejercicio adds to the
            # savings base. Gains stack up, deductible losses (and losses
            # recovered from earlier 2-month deferrals) stack down, and the
            # diamond marks the resulting net base — the figure the brackets
            # below tax.
            if len(year_ty) > 1:
                with st.container(border=True):
                    st.subheader(tr("portfolio.realized_by_year"))
                    yrs = list(year_ty)
                    fig = go.Figure()
                    def _hover(name: str) -> str:
                        # Unified hover drops the trace name unless it is baked
                        # into the template (same trick as the TWR chart).
                        return f"{name}  <b>€%{{y:,.0f}}</b><extra></extra>"

                    gains_lbl = tr("portfolio.chart_gains")
                    losses_lbl = tr("portfolio.chart_losses")
                    recovered_lbl = tr("portfolio.chart_recovered")
                    net_lbl = tr("portfolio.chart_net")
                    fig.add_bar(
                        name=gains_lbl, x=yrs,
                        y=[year_ty[y].realized_gain_eur for y in yrs],
                        marker_color=CANDLE_UP,
                        hovertemplate=_hover(gains_lbl),
                    )
                    fig.add_bar(
                        name=losses_lbl, x=yrs,
                        y=[-year_ty[y].deductible_loss_eur for y in yrs],
                        marker_color=CANDLE_DOWN,
                        hovertemplate=_hover(losses_lbl),
                    )
                    if any(t.recovered_loss_eur for t in year_ty.values()):
                        fig.add_bar(
                            name=recovered_lbl, x=yrs,
                            y=[-year_ty[y].recovered_loss_eur for y in yrs],
                            marker_color=WARN_ORANGE,
                            hovertemplate=_hover(recovered_lbl),
                        )
                    fig.add_scatter(
                        name=net_lbl, x=yrs,
                        y=[year_ty[y].net_taxable_eur for y in yrs],
                        mode="markers",
                        marker=dict(symbol="diamond", size=10, color=INFO_COLOR),
                        hovertemplate=_hover(net_lbl),
                    )
                    # One hover box per ejercicio: the year as header and every
                    # component named inside, so a lone "€7,699" can't float
                    # ambiguously between bars.
                    fig.update_layout(
                        **chart_layout(top_legend=True),
                        barmode="relative",
                        hovermode="x unified",
                    )
                    fig.update_xaxes(type="category")
                    show_chart(fig)
                    st.caption(tr("portfolio.realized_by_year_caption"))

            with st.container(border=True):
                # Few ejercicios read faster as buttons than as a dropdown.
                if len(sell_years) <= 3:
                    year = st.segmented_control(
                        tr("portfolio.fiscal_year"), sorted(sell_years),
                        default=max(sell_years), key="tax_year",
                    ) or max(sell_years)
                else:
                    year = st.selectbox(
                        tr("portfolio.fiscal_year"), sell_years, key="tax_year")
                ty = year_ty[year]

                st.subheader(tr("portfolio.irpf_savings_base", year=year))
                st.caption(tr("portfolio.irpf_caption"))
                st.html(kpi_grid_html([
                    (tr("portfolio.net_taxable"), f"€{ty.net_taxable_eur:,.0f}",
                     None, tr("portfolio.net_taxable_help")),
                    (tr("portfolio.estimated_tax"), f"€{ty.estimated_tax_eur:,.0f}",
                     None, tr("portfolio.estimated_tax_help")),
                    (tr("portfolio.carryforward_loss"),
                     f"€{ty.carryforward_loss_eur:,.0f}",
                     None, tr("portfolio.carryforward_loss_help")),
                ]))
                summary = tr(
                    "portfolio.realized_summary",
                    gain=f"{ty.realized_gain_eur:,.0f}",
                    loss=f"{ty.deductible_loss_eur:,.0f}",
                )
                if ty.deferred_loss_eur:
                    summary += tr(
                        "portfolio.deferred_note", deferred=f"{ty.deferred_loss_eur:,.0f}"
                    )
                if ty.recovered_loss_eur:
                    summary += tr(
                        "portfolio.recovered_note",
                        recovered=f"{ty.recovered_loss_eur:,.0f}",
                    )
                st.caption(summary)

                rows = [
                    {
                        "ticker": s.ticker, "buy": s.buy_date, "sell": s.sell_date,
                        "qty": s.quantity, "cost_eur": s.cost_eur,
                        "proceeds_eur": s.proceeds_eur, "gain_eur": s.gain_eur,
                        # Return on the cost of the shares this sale consumed —
                        # the pill beside the symbol on phones, merged into the
                        # gain cell on desktop.
                        "gain_pct": (s.gain_eur / s.cost_eur) if s.cost_eur else None,
                    }
                    for s in ty.sales
                ]
                if rows:
                    sales_df = pd.DataFrame(rows)
                    sales_fmt = {
                        "qty": "{:,.4f}",
                        "cost_eur": "€{:,.2f}",
                        "proceeds_eur": "€{:,.2f}",
                        "gain_eur": "€{:+,.2f}",
                        "gain_pct": "{:+.1%}",
                    }
                    sales_signed = ("gain_eur", "gain_pct")
                    sales_labels = {
                        "ticker": tr("portfolio.col_position"),
                        "buy": tr("portfolio.col_bought"),
                        "sell": tr("portfolio.col_sold"),
                        "qty": tr("portfolio.col_shares"),
                        "cost_eur": tr("portfolio.cost_basis"),
                        "proceeds_eur": tr("portfolio.col_proceeds"),
                        "gain_eur": tr("portfolio.col_gain"),
                    }
                    # Seven columns pan off a 390px screen, so narrow
                    # viewports get the same dense rows as the Positions tab:
                    # proceeds and the signed gain on the right, the return as
                    # a pill beside the symbol, dates + shares on the wrapping
                    # dim line (company names dropped — that line is already
                    # three items long). Both renderings ship; a 640px media
                    # query picks by live width, so a resized desktop window
                    # and desktop-UA tablets adapt too — not only "Mobi" UAs.
                    st.html(responsive_ticker_table_html(
                        sales_df, fmt=sales_fmt, signed=sales_signed,
                        labels=sales_labels,
                        left_cols=("buy", "sell"), sortable="realized",
                        pairs=(("gain_eur", "gain_pct"),),
                        mobile={
                            "value": "proceeds_eur", "delta": "gain_eur",
                            "badge": "gain_pct", "wrap": True,
                            "sub": ("buy", "sell", "qty"),
                            "sub_labels": {
                                "buy": tr("portfolio.col_bought"),
                                "sell": tr("portfolio.col_sold"),
                                "qty": tr("portfolio.col_shares"),
                            },
                        }))
                    if _MOBILE:
                        # Phones lose the dense rows' hidden columns; the full
                        # sortable table stays one expander away.
                        with st.expander(tr("portfolio.all_columns_realized")):
                            st.html(ticker_table_html(
                                sales_df, fmt=sales_fmt, signed=sales_signed,
                                left_cols=("buy", "sell"),
                                sortable="realized_all",
                                pairs=(("gain_eur", "gain_pct"),),
                                labels=sales_labels))

                # Mark to market from the cached positions table — shares the
                # Positions tab's price burst; unpriced names fall back to cost.
                try:
                    ptbl = positions_table(DB, db_mtime(DB))
                except (YFRateLimitError, URLError) as exc:
                    notices.data_toast(exc)  # else-block skipped: no breakdown
                except Exception:
                    st.warning(tr("portfolio.data_unavailable"))
                else:
                    foreign = (
                        float(ptbl["value_eur"].fillna(ptbl["cost_eur"]).sum())
                        if not ptbl.empty
                        else 0.0
                    )
                    # Build the message web-side from the flag's fields (tax_es
                    # stays English for the CLI); localize the ≥/< 50k branch here.
                    _flag = modelo_720_flag(foreign)
                    _msg = tr(
                        "portfolio.modelo_720_reportable"
                        if _flag.reportable
                        else "portfolio.modelo_720_ok",
                        val=f"{_flag.total_value_eur:,.0f}",
                    )
                    st.caption(tr("portfolio.modelo_720", message=_msg))
                st.caption(tr("portfolio.planning_aid"))

# ------------------------------------------------------------------- Dividends
if tab_div.open:
    with tab_div:
        years = dividends.by_year(txs)
        if not years:
            st.caption(tr("portfolio.no_dividends"))
        else:
            with st.container(border=True):
                rows = [
                    {
                        "year": yr, "gross_eur": d.gross_eur,
                        "withheld_eur": d.withheld_eur,
                        "net_eur": d.net_eur, "creditable_eur": d.creditable_eur,
                        "reclaimable_eur": d.reclaimable_eur,
                    }
                    for yr, d in sorted(years.items())
                ]
                div_frame = (
                    pd.DataFrame(rows)
                    .set_index("year")
                    .rename_axis(tr("portfolio.col_year"))
                    .rename(columns={
                        "gross_eur": tr("portfolio.col_gross"),
                        "withheld_eur": tr("portfolio.col_withheld"),
                        "net_eur": tr("portfolio.col_net"),
                        "creditable_eur": tr("portfolio.col_creditable"),
                        "reclaimable_eur": tr("portfolio.col_reclaimable"),
                    })
                )
                # Five money columns pan off a phone: there each year becomes a
                # card with one "label — €amount" line per column.
                data_table(
                    div_frame,
                    index_title=True,
                    fmt=dict.fromkeys(div_frame.columns, "€{:,.0f}"),
                )
                st.caption(tr("portfolio.dividends_caption"))

# ------------------------------------------------------------------- Fees
if tab_fees.open:
    with tab_fees:
        brokers = fees.by_broker(txs)
        if not brokers:
            st.caption(tr("portfolio.no_fees"))
        else:
            # Spread needs the trade-day bars; explicit commissions don't —
            # if Yahoo is down the tab degrades to the ledger-only columns.
            spreads: dict[str, fees.SpreadStats] = {}
            bars_ok = False
            try:
                bars = trade_bars(DB, db_mtime(DB))
            except (YFRateLimitError, URLError) as exc:
                notices.data_toast(exc)
            except Exception:
                st.warning(tr("portfolio.data_unavailable"))
            else:
                spreads = fees.spread_by_broker(txs, bars)
                bars_ok = True

            with st.container(border=True):
                st.subheader(tr("portfolio.fees_title"))
                volume = sum(b.volume_eur for b in brokers.values())
                explicit = sum(b.explicit_eur for b in brokers.values())
                spread = sum(s.spread_eur for s in spreads.values())
                total = explicit + spread
                st.html(kpi_grid_html([
                    (tr("portfolio.fees_explicit"), f"€{explicit:,.2f}",
                     None, tr("portfolio.fees_explicit_help")),
                    (tr("portfolio.fees_spread"),
                     f"€{spread:,.2f}" if bars_ok else tr("portfolio.na"),
                     None, tr("portfolio.fees_spread_help")),
                    (tr("portfolio.fees_pct_volume"),
                     f"{total / volume:.2%}" if volume else tr("portfolio.na"),
                     None, tr("portfolio.fees_pct_volume_help")),
                ]))

                any_other = any(b.other_fees_eur for b in brokers.values())
                rows = []
                for name in sorted(brokers, key=lambda n: -brokers[n].volume_eur):
                    b, s = brokers[name], spreads.get(name)
                    row = {
                        "broker": name,
                        "trades": b.trades,
                        "volume_eur": b.volume_eur,
                        "commission_eur": b.commission_eur,
                    }
                    if any_other:
                        row["other_eur"] = b.other_fees_eur
                    if bars_ok:
                        row["spread_eur"] = s.spread_eur if s else 0.0
                        row["spread_bps"] = s.spread_bps if s else 0.0
                    row["total_eur"] = b.explicit_eur + (s.spread_eur if s else 0.0)
                    row["cost_pct"] = (
                        row["total_eur"] / b.volume_eur if b.volume_eur else None
                    )
                    rows.append(row)
                fee_frame = pd.DataFrame(rows).set_index("broker").rename_axis(
                    tr("portfolio.col_broker"))
                fee_fmt = {
                    tr("portfolio.col_trades"): "{:,.0f}",
                    tr("portfolio.col_volume"): "€{:,.0f}",
                    tr("portfolio.col_commissions"): "€{:,.2f}",
                    tr("portfolio.col_other_fees"): "€{:,.2f}",
                    tr("portfolio.col_spread"): "€{:,.2f}",
                    tr("portfolio.col_spread_bps"): "{:,.1f}",
                    tr("portfolio.col_total_cost"): "€{:,.2f}",
                    tr("portfolio.col_cost_pct"): "{:.2%}",
                }
                fee_frame = fee_frame.rename(columns={
                    "trades": tr("portfolio.col_trades"),
                    "volume_eur": tr("portfolio.col_volume"),
                    "commission_eur": tr("portfolio.col_commissions"),
                    "other_eur": tr("portfolio.col_other_fees"),
                    "spread_eur": tr("portfolio.col_spread"),
                    "spread_bps": tr("portfolio.col_spread_bps"),
                    "total_eur": tr("portfolio.col_total_cost"),
                    "cost_pct": tr("portfolio.col_cost_pct"),
                })
                # Many money columns pan off a phone: there each broker becomes
                # a card with one "label — value" line per column (data_table).
                data_table(
                    fee_frame,
                    index_title=True,
                    fmt={k: v for k, v in fee_fmt.items() if k in fee_frame.columns},
                )
                if bars_ok:
                    measured = sum(s.measured for s in spreads.values())
                    skipped = sum(s.skipped for s in spreads.values())
                    if skipped:
                        st.caption(tr("portfolio.fees_spread_coverage",
                                      measured=measured,
                                      total=measured + skipped))
                    outside = sum(s.outside_range_eur for s in spreads.values())
                    if outside > 0.005:
                        st.caption(tr("portfolio.fees_outside_range",
                                      val=f"{outside:,.2f}"))
                st.caption(tr("portfolio.fees_caption"))
