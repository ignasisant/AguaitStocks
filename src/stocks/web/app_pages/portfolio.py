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
    market_value_weights_base,
    max_drawdown,
    money_weighted_return,
    portfolio_returns,
    top_n_weight,
    us_extended_session,
    us_market_open,
)
from stocks.portfolio import custody, dividends, fees
from stocks.portfolio.tax import month_range
from stocks.web import auth, notices, skeletons, tax_ui
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import (
    basket_history,
    custody_map,
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
    broker_name,
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

# The currency this account reckons in (Profile). Money is computed *in* it —
# every ledger leg at its own trade-date rate — so it keys the cached loaders
# rather than converting their output afterwards. REPORT_SYM prefixes the
# figures that are rendered as strings.
REPORT_CCY = auth.reporting_currency()
REPORT_SYM = auth.CURRENCY_SYMBOL[REPORT_CCY]

txs, positions, realized = ledger_state(DB, db_mtime(DB), REPORT_CCY)
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


# --------------------------------------------------------------------- Custody
# Which broker's account a holding sits in. A multi-broker book splits the same
# ticker across custodians and the ledger note prefix is the only record of it
# (see stocks.portfolio.custody), so the Positions table carries it per row and
# the allocation card adds a broker donut.
def _broker_cell(row: dict[str, custody.Custody]) -> str:
    """One position's custody: "Revolut" — or "Revolut 60% · ClickTrade 40%"
    when its shares sit at more than one broker."""
    parts = custody.mix(row)
    if not parts:
        return tr("portfolio.na")
    if len(parts) == 1:
        return broker_name(parts[0][0])
    return " · ".join(f"{broker_name(b)} {share:.0%}" for b, share in parts)


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
        tbl = enriched_positions(DB, db_mtime(DB), REPORT_CCY)
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        card.clear()  # the toast is the whole message; no empty card behind it
        return
    except Exception:
        card.container(border=True).warning(tr("portfolio.data_unavailable"))
        return
    with card.container(border=True):
        st.subheader(tr("portfolio.open_positions_pl", ccy=REPORT_CCY))
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
            cost = tbl["cost"].sum()
            value = tbl["value"].dropna().sum()
            sym = REPORT_SYM
            realized_gain = sum(s.gain for s in realized)
            realized_cost = sum(s.cost for s in realized)
            # Same TIKR-style tiles as the Ticker fundamentals card (and the
            # Home glance): value and its chip on one line, help as a "?" pill.
            st.html(kpi_grid_html([
                (tr("portfolio.cost_basis"), f"{sym}{cost:,.0f}", None, None),
                (
                    tr("portfolio.market_value"),
                    f"{sym}{value:,.0f}",
                    kpi_delta_chip(value / cost - 1 if cost else None),
                    None,
                ),
                (
                    tr("portfolio.unrealised_pl"),
                    f"{sym}{value - cost:+,.0f}",
                    kpi_delta_chip(value / cost - 1 if cost else None),
                    None,
                ),
                (
                    tr("portfolio.realised_pl"),
                    f"{sym}{realized_gain:+,.0f}",
                    kpi_delta_chip(
                        realized_gain / realized_cost if realized_cost else None
                    ),
                    tr("portfolio.realised_pl_help"),
                ),
            ]))

            try:
                vals = basket_history(DB, db_mtime(DB), REPORT_CCY)
            except (YFRateLimitError, URLError) as exc:
                notices.data_toast(exc)
                vals = pd.DataFrame()  # 1w/1m read n/a; the table below stands
            except Exception:
                # Degrade: 1w/1m read n/a; the positions table below still renders.
                st.warning(tr("portfolio.data_unavailable"))
                vals = pd.DataFrame()
            # Regular session closed → sum the per-row day (already overridden
            # to the live pre/after-hours quote, or the last completed session once
            # those windows shut) instead of the basket's close-to-close, which can
            # be a flat premarket 0%. Grey its delta ("off") only when nothing is
            # trading — an extended-hours quote is live.
            mkt_open = us_market_open()
            extended = None if mkt_open else us_extended_session()
            today_closed = None
            if not mkt_open:
                d_base = tbl["day"].dropna().sum()
                base = value - d_base
                today_closed = (d_base, d_base / base if base else 0.0)
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
                        f"{sym}{chg[0]:+,.0f}",
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
            # Custody per row, off the ledger — no extra fetch behind it.
            cust = custody_map(DB, db_mtime(DB), REPORT_CCY)
            tbl["broker"] = [_broker_cell(cust.get(t, {})) for t in tbl.index]
            tbl.insert(0, "ticker", tbl.index)
            tbl = tbl[
                ["ticker", "shares", "ccy", "broker", "cost", "value",
                 "weight", "day", "day_pct", "pnl", "pnl_pct"]
            ]

            fmt = {
                "shares": "{:.4f}",
                "cost": f"{REPORT_SYM}{{:,.0f}}",
                "value": f"{REPORT_SYM}{{:,.0f}}",
                "weight": "{:.1%}",
                "day": f"{REPORT_SYM}{{:+,.0f}}",
                "day_pct": "{:+.1%}",
                "pnl": f"{REPORT_SYM}{{:,.0f}}",
                "pnl_pct": "{:+.1%}",
            }
            pnl_cols = ("day", "day_pct", "pnl", "pnl_pct")
            # Rows with no live quote: dim only the day columns (a last-close move,
            # not live); total P/L stays full color. Crypto is 24/7, and a US name
            # in pre/after-hours is live, so neither dims.
            muted = {t for t in tbl["ticker"] if not market_active(t)}
            day_cols = ("day", "day_pct")
            # The amount and % of the same move belong in one cell,
            # the percentage as a tinted pill. Halves the desktop column count.
            pairs = (("day", "day_pct"), ("pnl", "pnl_pct"))
            labels = {
                "ticker": tr("portfolio.col_position"),
                "shares": tr("portfolio.col_shares"),
                "ccy": tr("portfolio.col_currency"),
                "broker": tr("portfolio.col_broker"),
                "cost": tr("portfolio.cost_basis"),
                "value": tr("portfolio.market_value"),
                "weight": tr("portfolio.col_weight"),
                "day": tr("portfolio.today"),
                "pnl": tr("portfolio.col_total_pl"),
            }

            if _MOBILE:
                # Dense Revolut-style rows — value + day% on the right, total
                # P/L as a pill beside the symbol (the dim line ellipsizes, so a
                # number there got cut), weight below; nothing pans
                # horizontally. Full sortable table one expander below.
                #
                # That dim line already carries the company name and the
                # weight, so it names the custodians without their share
                # split — the expander's table below keeps the percentages.
                mob = tbl.copy()
                mob["brokers"] = [
                    " · ".join(
                        broker_name(b) for b, _ in custody.mix(cust.get(t, {}))
                    ) or tr("portfolio.na")
                    for t in tbl["ticker"]
                ]
                st.html(ticker_table_html(
                    mob, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols,
                    labels=labels,
                    mobile={"value": "value", "delta": "day_pct",
                            "badge": "pnl_pct", "sub": ("weight", "brokers")}))
                with st.expander(tr("portfolio.all_columns")):
                    st.html(ticker_table_html(
                        tbl, fmt=fmt, signed=pnl_cols, muted=muted,
                        muted_cols=day_cols, pairs=pairs, labels=labels,
                        left_cols=("broker",), sortable="positions"))
            else:
                st.html(ticker_table_html(
                    tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols,
                    pairs=pairs, labels=labels, left_cols=("broker",),
                    sortable="positions"))
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


# Drag-zooming the history chart narrows x only (y stays pinned, so a drag
# never squashes the money scale) and Plotly leaves the y range where the full
# span put it: a 2022 window worth 4k draws as a flat line on the floor of a
# 120k axis. Streamlit surfaces no relayout event (only box/lasso selections,
# which would cost the drag-to-zoom gesture and a server round trip per drag),
# so the refit runs in the browser: the plotted band extents ride along in a
# hidden slot's data attributes — Plotly serializes numeric arrays as base64
# ({"dtype", "bdata"}), so gd.data is not readable from JS — and the picked
# window is read off the chart's own layout once the drag ends. Desktop only:
# phones pin both axes, so there is no zoom to follow.
_YFIT_JS = r"""
<script>
(function () {
  if (window.__topstocksYFit) return;  /* wire once per session */
  window.__topstocksYFit = true;
  /* Axis endpoints come back as "2026-06-28 15:07:03.5225" (space, and a
     sub-millisecond fraction outside the ISO grammar Safari holds to); the
     shipped stamps are ISO dates. Normalise both before parsing. */
  const ms = (v) => {
    if (typeof v === "number") return v;
    return Date.parse(String(v).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  };
  /* The history chart is the one carrying a meta="history" trace; every other
     chart on the page keeps its own axes to itself. */
  const chart = () =>
    Array.prototype.find.call(
      document.querySelectorAll(".js-plotly-plot"),
      (gd) => (gd.data || []).some((t) => t.meta === "history")
    );
  const fit = () => {
    /* Both nodes are looked up per event, never captured: Streamlit replaces
       them whenever the range buttons rerun the fragment. */
    const gd = chart();
    const b = document.querySelector(".ts-yfit");
    if (!gd || !b || !window.Plotly) return;
    const ax = (gd.layout || {}).xaxis || {};
    const ya = (gd.layout || {}).yaxis || {};
    if (ax.autorange || !ax.range) {  /* zoom reset: back to the whole span */
      gd.__tsYFit = "";
      if (!ya.autorange) window.Plotly.relayout(gd, {"yaxis.autorange": true});
      return;
    }
    const xs = (b.dataset.x || "").split(",");
    const los = (b.dataset.lo || "").split(",");
    const his = (b.dataset.hi || "").split(",");
    const x0 = Math.min(ms(ax.range[0]), ms(ax.range[1]));
    const x1 = Math.max(ms(ax.range[0]), ms(ax.range[1]));
    let i0 = -1, i1 = -1;
    for (let i = 0; i < xs.length; i++) {
      const t = ms(xs[i]);
      if (t < x0 || t > x1) continue;
      if (i0 < 0) i0 = i;
      i1 = i;
    }
    if (i0 < 0) return;  /* the window fell between two daily points */
    /* The line enters and leaves the window mid-segment: the points just
       outside each edge are drawn too, so they count towards the extents. */
    i0 = Math.max(0, i0 - 1);
    i1 = Math.min(xs.length - 1, i1 + 1);
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i <= i1; i++) {
      const a = parseFloat(los[i]), z = parseFloat(his[i]);
      if (a < lo) lo = a;
      if (z > hi) hi = z;
    }
    if (!isFinite(lo) || !isFinite(hi)) return;
    const pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.06 || 1;
    /* A book is never worth less than nothing: pad down to zero, not past it. */
    lo = lo >= 0 && lo - pad < 0 ? 0 : lo - pad;
    hi = hi + pad;
    const key = lo.toFixed(2) + ":" + hi.toFixed(2);
    if (gd.__tsYFit === key) return;  /* same window as the last drag */
    gd.__tsYFit = key;
    window.Plotly.relayout(gd, {"yaxis.range": [lo, hi]});
  };
  /* Deliberately not gd.on("plotly_relayout"): Streamlit re-plots the same
     div on a fragment rerun, and Plotly.newPlot purges the handlers off it
     while the element (so any "already wired" mark on it) survives — the
     refit would go dead for the rest of the session. Document-level listeners
     outlive every remount. The drag ends on mouseup, the reset on dblclick,
     and the modebar buttons on their own click; the timeout lets Plotly land
     its own relayout before the layout is read.
     The listeners take no target filter: Plotly covers the whole document
     with a .dragcover while a drag is live, so the mouseup that ends a zoom
     lands outside the chart. fit() is a no-op whenever the axis is not
     zoomed, which is every other click on the page. */
  const after = () => setTimeout(fit, 80);
  document.addEventListener("mouseup", after, true);
  document.addEventListener("dblclick", after, true);
})();
</script>
"""


def _yfit_slot(box, hist: pd.DataFrame) -> None:
    """Hidden slot carrying the plotted band extents, read by `_YFIT_JS`."""
    pair = hist[["value", "injected"]]
    xs = ",".join(hist.index.strftime("%Y-%m-%d"))
    los = ",".join(f"{v:.2f}" for v in pair.min(axis=1))
    his = ",".join(f"{v:.2f}" for v in pair.max(axis=1))
    box.html(
        '<div class="ts-yfit" style="display:none"'
        f' data-x="{xs}" data-lo="{los}" data-hi="{his}"></div>' + _YFIT_JS,
        unsafe_allow_javascript=True,
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
            (len(txs), txs[-1].date, date.today()), DB, REPORT_CCY
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

            def _pl_span(pnl: float, p: float) -> str:
                """Nominal P/L (bold) plus the % in one colored span: the
                nominal is the headline, the % the reference."""
                if pd.isna(p):
                    return "—"
                color = GREEN if p >= 0 else RED
                sign = "+" if pnl >= 0 else "-"
                return (
                    f'<span style="color:{color}"><b>{sign}{REPORT_SYM}'
                    f"{abs(pnl):,.0f}</b> ({p:+.1%})</span>"
                )

            customdata = [
                [inj, _pl_span(val - inj, p)]
                for val, inj, p in zip(
                    hist["value"],
                    hist["injected"],
                    hist["pnl_pct"],
                    strict=True,
                )
            ]
            # Split the value line by sign of P/L; masks overlap one point at
            # each crossing so the green and red segments stay connected.
            gain = hist["value"] >= hist["injected"]
            up = hist["value"].where(gain | gain.shift(1, fill_value=False))
            down = hist["value"].where(~gain | (~gain).shift(1, fill_value=False))
            GREEN_FILL, RED_FILL = PROFIT_BAND, LOSS_BAND

            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist["injected"],
                    name=tr("portfolio.series_injected"),
                    line=dict(color=TEXT_MUTED, width=2, shape="hv"),
                    hoverinfo="skip",
                )
            )
            # Green band where value ≥ injected …
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist[["value", "injected"]].max(axis=1),
                    line=dict(width=0), fill="tonexty", fillcolor=GREEN_FILL,
                    hoverinfo="skip", showlegend=False,
                )
            )
            # … re-anchor on injected, then red band where value < injected.
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist["injected"],
                    line=dict(width=0), hoverinfo="skip", showlegend=False,
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=hist.index, y=hist[["value", "injected"]].min(axis=1),
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
                    x=hist.index, y=hist["value"],
                    line=dict(width=0), opacity=0, showlegend=False,
                    meta="history", customdata=customdata,
                    # Plotly's own %{...} fields make str.format choke, so the
                    # currency slot is substituted directly rather than through
                    # tr()'s kwargs.
                    hovertemplate=tr("portfolio.hist_hover_tmpl").replace(
                        "{sym}", REPORT_SYM
                    ),
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
            if not _MOBILE:
                _yfit_slot(st, hist)
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
                        (len(txs), txs[-1].date, date.today()), DB, REPORT_CCY
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
                            hist["value"], flow_series(txs), start=win_start
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
                        # Flow days the value path can't price (an unrecorded
                        # split, say) are excluded rather than left to wreck
                        # every figure above — say which ones.
                        perf_note = [tr("portfolio.real_perf_note")]
                        if skipped := twr.attrs.get("dropped_days"):
                            perf_note.append(
                                tr("portfolio.twr_note_skipped",
                                   days=", ".join(str(d.date()) for d in skipped))
                            )
                        st.caption(" ".join(perf_note))

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
                    pos = ledger_state(db, mtime, REPORT_CCY)[1]
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
                    # Put weights on one currency's footing, then rebuild the
                    # portfolio return series.
                    rep.weights = market_value_weights_base(
                        positions, rep.prices, rep.meta, REPORT_CCY)
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
                    cells = [
                        ("sector", tr("portfolio.alloc_sector")),
                        ("country", tr("portfolio.alloc_geography")),
                        ("currency", tr("portfolio.alloc_currency")),
                    ]
                    # Custody joins the metadata donuts only when the book
                    # actually spans brokers: a single-broker 100% donut says
                    # nothing and would take a quarter of the row for it. The
                    # split reuses rep.weights (EUR market value), so it needs
                    # no fetch of its own — see custody.broker_weights.
                    by_broker = custody.broker_weights(
                        custody_map(DB, db_mtime(DB), REPORT_CCY), rep.weights)
                    if len(by_broker) > 1:
                        cells.append(("broker", tr("portfolio.alloc_broker")))
                    cols = st.columns(len(cells))
                    for col, (key, title) in zip(cols, cells, strict=True):
                        alloc = (
                            pd.Series({broker_name(b): w
                                       for b, w in by_broker.items()})
                            if key == "broker" else rep.allocation(key)
                        )
                        if alloc.empty:
                            col.caption(tr("portfolio.no_alloc_data", kind=title.lower()))
                            continue
                        if key == "country":
                            with col:
                                _geography_cell(alloc, title)
                            continue
                        show_chart(_alloc_pie(alloc, title), container=col)
                    if len(by_broker) > 1:
                        st.caption(tr("portfolio.alloc_broker_caption"))

                # Same full-span history the Positions tab builds, and cold
                # whenever the reader opens this tab first — so the card holds
                # its 420px chart as a shimmer rather than appearing late and
                # shoving the correlation matrix down the page.
                cum_card = skeletons.reserve(
                    "chart", border=True, title=True, height=420, legend=True
                )
                try:
                    _, twr, twr_missing = ledger_history(
                        (len(txs), txs[-1].date, date.today()), DB, REPORT_CCY
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
                        if skipped := twr.attrs.get("dropped_days"):
                            notes.append(
                                tr("portfolio.twr_note_skipped",
                                   days=", ".join(str(d.date()) for d in skipped))
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
        # Everything in this tab follows the Profile's tax residence: the
        # rules, the reporting currency and the wording all come from
        # stocks.web.tax_ui / stocks.portfolio.tax, so adding a country never
        # edits this page. The ledger is replayed *at* the jurisdiction's
        # currency rather than converted afterwards — a US filer's basis is
        # USD at each trade date, which is two rates, not one.
        _jur = tax_ui.jurisdiction()
        _code, _ccy = _jur.code, _jur.currency
        # Germany exempts 30% of a fund's result, so the settings carry which
        # holdings are funds (from the learned quoteType cache, no fetch).
        _tset = tax_ui.with_funds(
            tax_ui.settings(), {t.ticker for t in txs}
        )
        _sym = tax_ui.symbol(_ccy)
        # The jurisdiction picks the matching rule as well as the currency:
        # a UK replay pools shares, so its parcels are not the FIFO ones the
        # rest of the page shows.
        _, _, tax_realized = ledger_state(
            DB, db_mtime(DB), _ccy, _jur.matching)
        # Tax years, not calendar years — the UK's open on 6 April.
        sell_years = sorted(
            {_jur.tax_year_of(s.sell_date) for s in tax_realized}, reverse=True)
        if not sell_years:
            st.caption(tr("portfolio.no_realized_sales"))
        else:
            buy_dates: dict[str, list[str]] = defaultdict(list)
            for t in txs:
                if t.action == "buy":
                    buy_dates[t.ticker].append(t.date)
            year_ty = {
                y: _jur.fiscal_year(tax_realized, y, buy_dates, _tset)
                for y in sorted(sell_years)
            }

            # Period picture first: what each ejercicio adds to the savings
            # base. Gains stack up, deductible losses (and losses recovered
            # from earlier 2-month deferrals) stack down, and the diamond
            # marks the resulting net base — the figure the brackets below
            # tax. The monthly view is the same maths over ISO month
            # prefixes: a breakdown of when the result was booked, not a
            # taxable base of its own (IRPF nets over the ejercicio).
            sell_months = sorted({s.sell_date[:7] for s in tax_realized})
            if len(year_ty) > 1 or len(sell_months) > 1:
                with st.container(border=True):
                    YEAR, MONTH = "year", "month"
                    # A slot for the title lets the control sit beside (or
                    # above) it while the title — which depends on the
                    # control — is written afterwards. Phones stack: a 3:2
                    # split of ~390px leaves the heading two words per line
                    # and the control clipped.
                    if _MOBILE:
                        head, ctl = st.container(), st.container()
                    else:
                        head, ctl = st.columns(
                            [3, 2], vertical_alignment="bottom")
                    with ctl:
                        gran = st.segmented_control(
                            tr("portfolio.realized_granularity"),
                            options=(YEAR, MONTH),
                            default=YEAR if len(year_ty) > 1 else MONTH,
                            format_func=lambda v: (
                                tr("portfolio.granularity_year") if v == YEAR
                                else tr("portfolio.granularity_month")
                            ),
                            key="tax_granularity",
                            label_visibility="collapsed",
                            required=True,
                        )
                    with head:
                        st.subheader(tr(
                            "portfolio.realized_by_year" if gran == YEAR
                            else "portfolio.realized_by_month"))
                    if gran == YEAR:
                        period_ty = year_ty
                    else:
                        # Every month between the first and last sale, quiet
                        # ones included (see month_range).
                        period_ty = {
                            m: _jur.fiscal_period(
                                tax_realized, m, buy_dates, _tset)
                            for m in month_range(
                                sell_months[0], sell_months[-1])
                        }
                    keys = list(period_ty)
                    fig = go.Figure()
                    def _hover(name: str) -> str:
                        # Unified hover drops the trace name unless it is baked
                        # into the template (same trick as the TWR chart).
                        return f"{name}  <b>{_sym}%{{y:,.0f}}</b><extra></extra>"

                    gains_lbl = tax_ui.t(_code, "chart_gains")
                    losses_lbl = tax_ui.t(_code, "chart_losses")
                    recovered_lbl = tax_ui.t(_code, "chart_recovered")
                    net_lbl = tax_ui.t(_code, "chart_net")
                    fig.add_bar(
                        name=gains_lbl, x=keys,
                        y=[period_ty[k].realized_gain for k in keys],
                        marker_color=CANDLE_UP,
                        hovertemplate=_hover(gains_lbl),
                    )
                    fig.add_bar(
                        name=losses_lbl, x=keys,
                        y=[-period_ty[k].deductible_loss for k in keys],
                        marker_color=CANDLE_DOWN,
                        hovertemplate=_hover(losses_lbl),
                    )
                    if any(t.recovered_loss for t in period_ty.values()):
                        fig.add_bar(
                            name=recovered_lbl, x=keys,
                            y=[-period_ty[k].recovered_loss for k in keys],
                            marker_color=WARN_ORANGE,
                            hovertemplate=_hover(recovered_lbl),
                        )
                    fig.add_scatter(
                        name=net_lbl, x=keys,
                        y=[period_ty[k].net_taxable for k in keys],
                        mode="markers",
                        marker=dict(symbol="diamond", size=10, color=INFO_COLOR),
                        hovertemplate=_hover(net_lbl),
                    )
                    # One hover box per period: the year (or month) as header
                    # and every component named inside, so a lone "7,699"
                    # can't float ambiguously between bars.
                    fig.update_layout(
                        # Four legend entries wrap to three rows on a phone and
                        # the slanted month labels claim a bottom band, so the
                        # default 260px would leave the bars a ~100px strip.
                        **chart_layout(
                            top_legend=True, height=340 if _MOBILE else 260),
                        barmode="relative",
                        hovermode="x unified",
                    )
                    # Category axis: the periods are labels, not a numeric
                    # scale. A monthly run gets slanted labels, thinned to what
                    # the width can print without "2025-01" colliding with its
                    # neighbour — the card fits ~14 on desktop, ~5 on a ~390px
                    # phone (DS mobile chart spec). automargin because the
                    # layout margin is b=0: the slant needs its own band or the
                    # labels clip off the bottom of the shorter mobile canvas.
                    if gran == MONTH:
                        _max_ticks = 5 if _MOBILE else 14
                        fig.update_xaxes(
                            type="category",
                            tickangle=-45,
                            dtick=max(1, math.ceil(len(keys) / _max_ticks)),
                            automargin=True,
                        )
                    else:
                        fig.update_xaxes(type="category", tickangle=0)
                    show_chart(fig, key="realized_by_period")
                    st.caption(tax_ui.t(_code, "realized_by_year_caption"))
                    if gran == MONTH:
                        st.caption(tax_ui.t(_code, "realized_by_month_caption"))

            with st.container(border=True):
                # Few ejercicios read faster as buttons than as a dropdown.
                if len(sell_years) <= 3:
                    year = st.segmented_control(
                        tax_ui.t(_code, "fiscal_year"), sorted(sell_years),
                        default=max(sell_years), key="tax_year",
                        format_func=_jur.year_label,
                    ) or max(sell_years)
                else:
                    year = st.selectbox(
                        tax_ui.t(_code, "fiscal_year"), sell_years,
                        key="tax_year", format_func=_jur.year_label)
                ty = year_ty[year]

                st.subheader(
                    tax_ui.t(_code, "tax_header", year=_jur.year_label(year))
                )
                st.caption(tax_ui.t(_code, "tax_caption"))
                # The tiles are whatever the jurisdiction thinks matter: Spain
                # has one base, the US adds its short- and long-term nets.
                st.html(kpi_grid_html([
                    (
                        tax_ui.t(_code, k.key),
                        tax_ui.money(k.value, _ccy),
                        None,
                        tax_ui.t(_code, k.help_key),
                    )
                    for k in ty.kpis()
                ]))
                summary = tr(
                    "portfolio.realized_summary",
                    gain=tax_ui.money(ty.realized_gain, _ccy),
                    loss=tax_ui.money(ty.deductible_loss, _ccy),
                )
                for _note in ty.notes():
                    summary += tax_ui.t(_code, _note.key, **_note.kwargs)
                st.caption(summary)

                rows = [
                    {
                        "ticker": s.ticker, "buy": s.buy_date, "sell": s.sell_date,
                        # Holding period only where the rate turns on it (US
                        # short vs long term); Spain taxes both the same.
                        **(
                            {
                                "term": tax_ui.t(
                                    _code,
                                    "term_long"
                                    if _jur.is_long_term(
                                        s.buy_date, s.sell_date)
                                    else "term_short",
                                )
                            }
                            if _jur.splits_holding_period
                            else {}
                        ),
                        # Under pooling a "cost" can be an average, so the rule
                        # that produced it belongs next to it.
                        **(
                            {"matched": tax_ui.t(_code, f"match_{s.matched}")}
                            if _jur.pools_shares
                            else {}
                        ),
                        "qty": s.quantity, "cost": s.cost,
                        "proceeds": s.proceeds, "gain": s.gain,
                        # Return on the cost of the shares this sale consumed —
                        # the pill beside the symbol on phones, merged into the
                        # gain cell on desktop.
                        "gain_pct": (s.gain / s.cost) if s.cost else None,
                    }
                    for s in ty.sales
                ]
                if rows:
                    sales_df = pd.DataFrame(rows)
                    sales_fmt = {
                        "qty": "{:,.4f}",
                        "cost": f"{_sym}{{:,.2f}}",
                        "proceeds": f"{_sym}{{:,.2f}}",
                        "gain": f"{_sym}{{:+,.2f}}",
                        "gain_pct": "{:+.1%}",
                    }
                    sales_signed = ("gain", "gain_pct")
                    sales_labels = {
                        "ticker": tr("portfolio.col_position"),
                        "buy": tr("portfolio.col_bought"),
                        "sell": tr("portfolio.col_sold"),
                        "term": tax_ui.t(_code, "col_term"),
                        "matched": tax_ui.t(_code, "col_matched"),
                        "qty": tr("portfolio.col_shares"),
                        "cost": tr("portfolio.cost_basis"),
                        "proceeds": tr("portfolio.col_proceeds"),
                        "gain": tr("portfolio.col_gain"),
                    }
                    # Seven columns pan off a 390px screen, so narrow
                    # viewports get the same dense rows as the Positions tab:
                    # proceeds and the signed gain on the right, the return as
                    # a pill beside the symbol, dates + shares on the wrapping
                    # dim line (company names dropped — that line is already
                    # three items long). Both renderings ship; a 640px media
                    # query picks by live width, so a resized desktop window
                    # and desktop-UA tablets adapt too — not only "Mobi" UAs.
                    _left_cols = (
                        ("buy", "sell")
                        + (("term",) if _jur.splits_holding_period else ())
                        + (("matched",) if _jur.pools_shares else ())
                    )
                    st.html(responsive_ticker_table_html(
                        sales_df, fmt=sales_fmt, signed=sales_signed,
                        labels=sales_labels,
                        left_cols=_left_cols, sortable="realized",
                        pairs=(("gain", "gain_pct"),),
                        mobile={
                            "value": "proceeds", "delta": "gain",
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
                                left_cols=_left_cols,
                                sortable="realized_all",
                                pairs=(("gain", "gain_pct"),),
                                labels=sales_labels))

                # Mark to market from the cached positions table — shares the
                # Positions tab's price burst; unpriced names fall back to cost.
                try:
                    ptbl = positions_table(DB, db_mtime(DB), REPORT_CCY)
                except (YFRateLimitError, URLError) as exc:
                    notices.data_toast(exc)  # else-block skipped: no breakdown
                except Exception:
                    st.warning(tr("portfolio.data_unavailable"))
                else:
                    foreign = (
                        float(ptbl["value"].fillna(ptbl["cost"]).sum())
                        if not ptbl.empty
                        else 0.0
                    )
                    # The priced table is EUR and the thresholds are in the
                    # jurisdiction's own currency, so a non-EUR filer's total
                    # converts at spot: this is a threshold check, not a basis
                    # (FBAR's year-end Treasury rate isn't worth a second
                    # replay for a line that says "may apply").
                    _fx = 1.0 if _ccy == "EUR" else (eur_spot(_ccy) or 0.0)
                    if _fx:
                        for _flag in _jur.reporting_flags(
                            foreign * _fx, _tset
                        ):
                            st.caption(
                                tax_ui.flag_caption(_code, _flag, _ccy))
                st.caption(tax_ui.t(_code, "planning_aid"))

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
                        "year": yr, "gross": d.gross,
                        "withheld": d.withheld,
                        "net": d.net, "creditable": d.creditable,
                        "reclaimable": d.reclaimable,
                    }
                    for yr, d in sorted(years.items())
                ]
                div_frame = (
                    pd.DataFrame(rows)
                    .set_index("year")
                    .rename_axis(tr("portfolio.col_year"))
                    .rename(columns={
                        "gross": tr("portfolio.col_gross"),
                        "withheld": tr("portfolio.col_withheld"),
                        "net": tr("portfolio.col_net"),
                        "creditable": tr("portfolio.col_creditable"),
                        "reclaimable": tr("portfolio.col_reclaimable"),
                    })
                )
                # Five money columns pan off a phone: there each year becomes a
                # card with one "label — amount" line per column.
                data_table(
                    div_frame,
                    index_title=True,
                    fmt=dict.fromkeys(div_frame.columns, f"{REPORT_SYM}{{:,.0f}}"),
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
                volume = sum(b.volume for b in brokers.values())
                explicit = sum(b.explicit for b in brokers.values())
                spread = sum(s.spread for s in spreads.values())
                total = explicit + spread
                st.html(kpi_grid_html([
                    (tr("portfolio.fees_explicit"), f"{REPORT_SYM}{explicit:,.2f}",
                     None, tr("portfolio.fees_explicit_help")),
                    (tr("portfolio.fees_spread"),
                     f"{REPORT_SYM}{spread:,.2f}" if bars_ok else tr("portfolio.na"),
                     None, tr("portfolio.fees_spread_help")),
                    (tr("portfolio.fees_pct_volume"),
                     f"{total / volume:.2%}" if volume else tr("portfolio.na"),
                     None, tr("portfolio.fees_pct_volume_help")),
                ]))

                any_other = any(b.other_fees for b in brokers.values())
                rows = []
                for name in sorted(brokers, key=lambda n: -brokers[n].volume):
                    b, s = brokers[name], spreads.get(name)
                    row = {
                        "broker": broker_name(name),
                        "trades": b.trades,
                        "volume": b.volume,
                        "commission": b.commission,
                    }
                    if any_other:
                        row["other"] = b.other_fees
                    if bars_ok:
                        row["spread"] = s.spread if s else 0.0
                        row["spread_bps"] = s.spread_bps if s else 0.0
                    row["total"] = b.explicit + (s.spread if s else 0.0)
                    row["cost_pct"] = (
                        row["total"] / b.volume if b.volume else None
                    )
                    rows.append(row)
                fee_frame = pd.DataFrame(rows).set_index("broker").rename_axis(
                    tr("portfolio.col_broker"))
                fee_fmt = {
                    tr("portfolio.col_trades"): "{:,.0f}",
                    tr("portfolio.col_volume"): f"{REPORT_SYM}{{:,.0f}}",
                    tr("portfolio.col_commissions"): f"{REPORT_SYM}{{:,.2f}}",
                    tr("portfolio.col_other_fees"): f"{REPORT_SYM}{{:,.2f}}",
                    tr("portfolio.col_spread"): f"{REPORT_SYM}{{:,.2f}}",
                    tr("portfolio.col_spread_bps"): "{:,.1f}",
                    tr("portfolio.col_total_cost"): f"{REPORT_SYM}{{:,.2f}}",
                    tr("portfolio.col_cost_pct"): "{:.2%}",
                }
                fee_frame = fee_frame.rename(columns={
                    "trades": tr("portfolio.col_trades"),
                    "volume": tr("portfolio.col_volume"),
                    "commission": tr("portfolio.col_commissions"),
                    "other": tr("portfolio.col_other_fees"),
                    "spread": tr("portfolio.col_spread"),
                    "spread_bps": tr("portfolio.col_spread_bps"),
                    "total": tr("portfolio.col_total_cost"),
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
                    outside = sum(s.outside_range for s in spreads.values())
                    if outside > 0.005:
                        st.caption(tr(
                            "portfolio.fees_outside_range",
                            val=f"{REPORT_SYM}{outside:,.2f}",
                        ))
                st.caption(tr("portfolio.fees_caption"))
