"""Ticker page — per-company dashboard: price, fundamentals, insiders, comps.

Page config, CSS, navigation and the top-bar ticker search live in web/app.py;
this module is content only and reads the shared selection. The price
section is a fragment so period changes redraw the chart without re-running
fundamentals, insiders and comparables below it.
"""

from __future__ import annotations

import html
import re
from datetime import date
from urllib.error import URLError

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.analysis.fundamentals import (
    KPI_SOURCES,
    annual_financials,
    comp_medals,
    comparables_table,
    compute_metrics,
    format_value,
    quarterly_eps,
    verdict,
    verdict_md,
)
from stocks.analysis.indicators import add_indicators
from stocks.analysis.moat import PILLAR_WEIGHTS, MoatScore, moat_score
from stocks.analysis.pe_history import pe_vs_history, window_stats
from stocks.config import load_watchlist
from stocks.data.bafin import insider_transactions as bafin_transactions
from stocks.data.crypto import is_crypto, split_pair
from stocks.data.edgar import cik_for
from stocks.data.estimates import (
    RawEstimates,
    estimate_currency,
    fetch_estimates,
    projection,
)
from stocks.data.fetch import fetch_history
from stocks.data.fundamentals import fetch_fundamentals
from stocks.data.funds import FundProfile, fetch_profile, is_fund
from stocks.data.fx import spot
from stocks.data.insiders import (
    BUY_CODE,
    insider_transactions,
    summarize,
    transactions_frame,
)
from stocks.formatting import compact_money
from stocks.portfolio.custody import Custody, by_position, mix
from stocks.portfolio.ledger import all_transactions
from stocks.portfolio.positions import build as build_positions
from stocks.web import auth, notices, skeletons
from stocks.web.i18n import active_language
from stocks.web.i18n import t as tr
from stocks.web.kpi_text import kpi_desc, sources_table
from stocks.web.widgets import (
    ACCENT_AREA,
    ACCENT_BAND,
    BORDER,
    BRAND_ACCENT,
    CANDLE_DOWN,
    CANDLE_UP,
    DOWN_COLOR,
    DOWN_FILL,
    EVENT_LINE,
    FS_3XL,
    FS_BASE,
    FS_LG,
    FS_SM,
    FS_XS,
    PURPLE_300,
    PURPLE_800,
    RADIUS_MD,
    RADIUS_PILL,
    RADIUS_SM,
    RADIUS_XS,
    SMA_FAST,
    SMA_SLOW,
    SUCCESS_FILL,
    SURFACE_PAGE,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TRANSPARENT,
    UP_COLOR,
    WARN_COLOR,
    broker_chips_html,
    chart_layout,
    company_name,
    data_table,
    hover_wrap,
    is_mobile,
    kpi_grid_html,
    metric_cells,
    show_chart,
    stacked_table_html,
    ticker_actions,
    ticker_cell,
    ticker_pill_md,
    ticker_table_html,
)
from stocks.web.widgets import logo as _logo

_MOBILE = is_mobile()
# The account's reporting currency: the "≈" line under a position's native
# value is converted into it, and the ledger replay behind that value uses it
# as its base (see web/portfolio_data.py).
REPORT_CCY = auth.reporting_currency()
REPORT_SYM = auth.CURRENCY_SYMBOL[REPORT_CCY]

# Selection comes from the top-bar search (web/app.py); the ?ticker=
# deep-link handling there feeds the same session key.
ticker = (st.session_state.get("picker_selected") or "").strip().upper()
if not ticker:
    st.info(tr("ticker.pick_prompt"))
    st.stop()

holdings = load_watchlist(auth.watchlist_path())
labels = {h.ticker: (h.name or h.ticker) for h in holdings}
tickers = [h.ticker for h in holdings]

# The viewed ticker is written back to the URL, so the current view is
# bookmarkable/shareable and survives a browser refresh.
st.session_state["_url_ticker"] = ticker
if st.query_params.get("ticker") != ticker:
    st.query_params["ticker"] = ticker

# label -> (yfinance fetch period, interval). Short ranges use intraday bars
# so the candles have enough points to be readable. The fetch period is longer
# than the display window so SMA20/50 (and RSI) have warm-up bars before the
# range starts and the lines span the whole chart; _trim cuts the frame back
# to the label's window after the indicators are computed.
PERIODS = {
    "1d": ("5d", "5m"),
    "1w": ("1mo", "30m"),
    "1m": ("6mo", "1d"),
    "3m": ("1y", "1d"),
    "6m": ("1y", "1d"),
    "1y": ("2y", "1d"),
    "2y": ("5y", "1d"),
    "5y": ("10y", "1d"),
}

# Display window per label for the daily-interval ranges, anchored at the
# last bar. Intraday labels (1d/1w) trim by trading session instead.
_WINDOW = {
    "1m": pd.DateOffset(months=1),
    "3m": pd.DateOffset(months=3),
    "6m": pd.DateOffset(months=6),
    "1y": pd.DateOffset(years=1),
    "2y": pd.DateOffset(years=2),
    "5y": pd.DateOffset(years=5),
}


def _trim(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Cut an extended-history frame back to the label's display window."""
    if df.empty:
        return df
    if label in ("1d", "1w"):
        sessions = df.index.normalize()
        keep = sessions.unique()[-1 if label == "1d" else -5:]
        return df[sessions >= keep[0]]
    return df[df.index >= df.index[-1] - _WINDOW[label]]


@st.cache_data(ttl=300, show_spinner=False)
def _history(t: str, label: str) -> pd.DataFrame:
    period, interval = PERIODS[label]
    df = _trim(add_indicators(fetch_history(t, period=period, interval=interval)), label)
    # Plotly.js has no timezone support: keep exchange-local wall time so the
    # hour-based rangebreaks below line up with what the axis shows.
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _rangebreaks(df: pd.DataFrame, interval: str) -> list[dict]:
    """Axis breaks hiding closed-market time so candles render contiguous.

    Weekends and holidays come from the days actually missing in the data,
    overnight hours (intraday bars only) from the observed session open/close
    — so US and EU tickers both work without an exchange calendar. Markets
    with weekend bars (crypto) get no breaks at all.
    """
    if df.empty or (df.index.dayofweek >= 5).any():
        return []
    breaks = [dict(bounds=["sat", "mon"])]
    sessions = df.index.normalize().unique()
    holidays = pd.bdate_range(sessions[0], sessions[-1]).difference(sessions)
    if len(holidays):
        breaks.append(dict(values=holidays.tolist()))
    if interval.endswith(("m", "h")):
        t = df.index.to_series()
        day = t.dt.normalize()
        hours = t.dt.hour + t.dt.minute / 60
        open_h = hours.groupby(day).min().median()
        close_h = (
            hours.groupby(day).max() + pd.Timedelta(interval) / pd.Timedelta(hours=1)
        ).median()
        if close_h != open_h:
            breaks.append(dict(bounds=[close_h % 24, open_h], pattern="hour"))
    return breaks


@st.cache_data(ttl=60, show_spinner=False)
def _ledger(db: str):
    """This user's transactions; `db` keys the cache per account."""
    from pathlib import Path

    return all_transactions(Path(db))


# Corporate-event verticals on the price chart: letter tag, line/marker color.
_EVENT_KINDS = {
    "d": (tr("ticker.ev_dividends"), WARN_COLOR),
    "r": (tr("ticker.ev_results"), SMA_SLOW),
}


@st.cache_data(ttl=3600, show_spinner=False)
def _earnings_events(t: str):
    """All known earnings dates + reported results, from the earnings module
    (one yfinance pass, cached an hour; empty on any fetch failure)."""
    from stocks.data.earnings import fetch_earnings

    try:
        return fetch_earnings(t)
    except Exception:
        return [], []


def _event_markers(
    df: pd.DataFrame, ticker: str, *, daily: bool
) -> list[tuple[str, pd.Timestamp, str]]:
    """(kind, bar timestamp, hover html) per dividend / results event in view.

    Dividends ride along in the history frame (yfinance actions column), so
    they cost nothing; results dates come from the cached earnings calendar.
    Each event is snapped to a bar of the trimmed frame so the unified hover
    box picks its text up on that date's column.
    """
    events: list[tuple[str, pd.Timestamp, str]] = []
    if df.empty or is_crypto(ticker):
        return events

    div = df.get("Dividends")
    if div is not None:
        for ts, amt in div[div > 0].items():
            txt = tr("ticker.hover_dividend", amt=f"{amt:,.4g}", date=f"{ts:%Y-%m-%d}")
            if close := float(df.at[ts, "Close"]):
                txt += tr("ticker.hover_div_yield", pct=f"{amt / close * 100:.2f}")
            events.append(("d", ts, txt))

    # A fund pays distributions (marked above, they ride in the history frame)
    # but never reports, so the earnings calendar is a wasted round trip that
    # answers "No earnings dates found" for every ETF.
    if is_fund(ticker):
        return events

    dates, results = _earnings_events(ticker)
    by_date = {r.date: r for r in results}
    lo, hi = df.index[0].normalize(), df.index[-1]
    for d in dates:
        ts = pd.Timestamp(d)
        if not (lo <= ts <= hi):
            continue
        # First bar of the event's session; a report on a non-trading day
        # (or after the close) lands on the next bar, where the gap shows.
        pos = min(df.index.searchsorted(ts), len(df) - 1)
        r = by_date.get(d)
        parts = [tr("ticker.hover_results", date=f"{d:%Y-%m-%d}")]
        if r and r.reported_eps is not None:
            line = f"EPS {r.reported_eps:.2f}"
            if r.eps_estimate is not None:
                line += tr("ticker.hover_vs_est", est=f"{r.eps_estimate:.2f}")
            if r.surprise_pct is not None:
                color = UP_COLOR if r.surprise_pct >= 0 else DOWN_COLOR
                line += (
                    f" · <span style='color:{color}'>"
                    f"<b>{r.surprise_pct:+.1f}%</b></span> " + tr("ticker.hover_surprise")
                )
            parts.append(line)
        # Two-session move spanning the print (report timing pre/post market
        # is unknown) — same window as earnings.price_reaction, but free here.
        if daily and 0 < pos < len(df) - 1:
            before = float(df["Close"].iloc[pos - 1])
            after = float(df["Close"].iloc[pos + 1])
            if before:
                parts.append(
                    tr("ticker.hover_move", pct=f"{(after / before - 1) * 100:+.1f}")
                )
        events.append(("r", df.index[pos], "<br>".join(parts)))
    return events


@st.cache_data(ttl=60, show_spinner=False)
def _held(db: str):
    """Open positions with *native-currency* cost (identity FX: no network)."""
    try:
        positions, _ = build_positions(_ledger(db), to_base=lambda a, c, d: a)
        return {p.ticker: p for p in positions}
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def _custody(db: str) -> dict[str, dict[str, Custody]]:
    """Open shares per (ticker, broker): which broker's account holds them.

    Identity FX like `_held` — the header only needs the share split, not a
    EUR basis, so this stays off the network."""
    try:
        return by_position(_ledger(db), to_base=lambda a, c, d: a)
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def _position_values(db: str, base: str = "EUR") -> dict[str, float]:
    """Live market value per open position in `base` (price × spot)."""
    from stocks.analysis.portfolio import market_values

    held = _held(db)
    return market_values(list(held.values()), base=base) if held else {}


def _live_quote(ticker: str) -> dict | None:
    """Quote snapshot (`price`/`pct`/`session`) while `ticker` is outside its
    regular session, else None — during the session the fetched history's last
    close already tracks the live price. Crypto trades 24/7, so it never
    overrides. See `analysis.portfolio.session_quote`."""
    from stocks.analysis.portfolio import market_live
    from stocks.web.portfolio_data import last_session_quote

    return None if market_live(ticker) else last_session_quote(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def _raw(t: str):
    return fetch_fundamentals(t)


@st.cache_data(ttl=3600, show_spinner=False)
def _estimates(t: str) -> RawEstimates:
    return fetch_estimates(t)


def _projection(t: str, last_fy: int) -> pd.DataFrame:
    """3y forward consensus path for the charts.

    Empty when the estimates are quoted in a different currency than the
    financial statements (ADRs: statements local, estimates per USD ADS) —
    mixing them on one axis would be wrong.
    """
    try:
        raw_est = _estimates(t)
        fin_ccy = _raw(t).info.get("financialCurrency")
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        return pd.DataFrame()  # no projection overlay this run
    except Exception:
        return pd.DataFrame()
    est_ccy = estimate_currency(raw_est.revenue_estimate) or estimate_currency(
        raw_est.earnings_estimate
    )
    if fin_ccy and est_ccy and fin_ccy != est_ccy:
        return pd.DataFrame()
    return projection(raw_est, last_fy)


# Divider between reported and forecast regions of a chart. Reuses the
# corporate-event rule token, so every non-data line on a chart is neutral-600.
_FORECAST_DIVIDER = dict(line_dash="dot", line_color=EVENT_LINE)


_fmt_money = compact_money  # shared compact currency label, e.g. $394.3B


def _legend(label: str, s: pd.Series) -> str:
    """Trace name enriched with latest value and per-year CAGR over the span."""
    s = s.dropna()
    if s.empty:
        return label
    parts = [tr("ticker.legend_latest", label=label, val=_fmt_money(s.iloc[-1]))]
    first, last, n = s.iloc[0], s.iloc[-1], len(s)
    if n > 1 and first > 0 and last > 0:  # CAGR only meaningful for positive spans
        cagr = ((last / first) ** (1 / (n - 1)) - 1) * 100
        parts.append(tr("ticker.legend_cagr", pct=f"{cagr:+.0f}"))
    return " · ".join(parts)


# --- phone price summary (the design's "Valor (móvil)" frame) --------------
# On phones the seven-tile desktop KPI strip reads as a wall; the design shows
# a big price hero (32px Epilogue + day pill + range change) over a 2×2 grid
# of position tiles, with RSI folded into a tile caption. Rendered as one
# st.html block — metric_cells' fixed-width wrap can't produce this shape.


def _pill_html(pct: float, *, small: bool = False) -> str:
    """Filled day-change pill, same pair as the stMetricDelta CSS in app.py."""
    up = pct >= 0
    return (
        '<span style="display:inline-flex;align-items:center;gap:3px;'
        f"background:{SUCCESS_FILL if up else DOWN_FILL};"
        f"color:{UP_COLOR if up else DOWN_COLOR};"
        f"font-size:{FS_XS if small else FS_SM};font-weight:600;"
        f'border-radius:{RADIUS_PILL};padding:{"1px 7px" if small else "2px 8px"}">'
        f"{'↑' if up else '↓'} {pct:+.2f}%</span>"
    )


def _muted(txt: str) -> str:
    return (
        f'<span style="font-size:{FS_XS};color:{TEXT_MUTED}">'
        f"{html.escape(txt)}</span>"
    )


def _tile(label: str, value_html: str, note_html: str = "") -> str:
    """One grid tile: muted label over a 16px value, optional caption line.
    Page-tone fill so the tile reads inset inside the section card."""
    return (
        f'<div style="background:{SURFACE_PAGE};border:1px solid {BORDER};'
        f"border-radius:{RADIUS_MD};"
        'padding:12px;display:flex;flex-direction:column;gap:4px;align-items:flex-start">'
        f'<span style="font-size:{FS_XS};font-weight:500;color:{TEXT_SECONDARY}">'
        f"{html.escape(label)}</span>"
        f'<span style="font-size:{FS_LG};font-weight:600;line-height:1.2">'
        f"{value_html}</span>"
        + note_html
        + "</div>"
    )


def _plain(md: str) -> str:
    """Markdown verdict → plain text for HTML captions (:color[**x**] → x)."""
    return re.sub(r":\w+\[(.*?)\]", r"\1", md).replace("**", "")


def _mobile_summary_html(
    df: pd.DataFrame,
    sel: str,
    my_pos,
    db: str,
    price_now: float,
    day_pct: float,
    session_note: str = "",
) -> str:
    last = float(df["Close"].iloc[-1])
    first = float(df["Close"].iloc[0])
    period_pct = (last - first) / first * 100
    rsi_val = float(df["RSI14"].iloc[-1])
    sma20 = float(df["SMA20"].iloc[-1])
    rsi_txt = f"RSI {rsi_val:.1f} · {_plain(verdict_md('rsi', rsi_val))}"

    parts = [
        '<div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap">'
        "<span style=\"font-family:'Epilogue','Instrument Sans',sans-serif;"
        f'font-weight:700;font-size:{FS_3XL};line-height:1.1">{price_now:,.2f}</span>'
        + _pill_html(day_pct)
        + (_muted(session_note) if session_note else "")
        + f'<span style="font-size:{FS_SM};padding-bottom:2px;'
        f'color:{UP_COLOR if period_pct >= 0 else DOWN_COLOR}">'
        + html.escape(
            tr(
                "ticker.period_change",
                pct=f"{period_pct:+.2f}%",
                period=tr(f"ticker.period_{sel}"),
            )
        )
        + "</span></div>"
    ]
    if my_pos:
        values_base = _position_values(db, REPORT_CCY)
        total = sum(values_base.values())
        value = values_base.get(my_pos.ticker)
        value_native = my_pos.quantity * last
        pnl_native = value_native - my_pos.cost_native
        pnl_pct = (
            (last / my_pos.avg_cost_native - 1) * 100 if my_pos.avg_cost_native else 0.0
        )
        weight = f"{value / total * 100:.1f}%" if value and total else "—"
        parts.append(
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
            + _tile(
                tr("ticker.position_value"),
                f"{value_native:,.2f} {my_pos.currency}",
                _muted(f"≈ {REPORT_SYM}{value:,.2f}") if value else "",
            )
            + _tile(
                tr("ticker.unrealised_pl"),
                f'<span style="color:{UP_COLOR if pnl_native >= 0 else DOWN_COLOR}">'
                f"{pnl_native:+,.2f} {my_pos.currency}</span>",
                _pill_html(pnl_pct, small=True),
            )
            + _tile(tr("ticker.pct_portfolio"), weight, _muted(rsi_txt))
            + _tile(
                tr("ticker.avg_buy_price"),
                f"{my_pos.avg_cost_native:,.2f} {my_pos.currency}",
                _muted(tr("ticker.n_shares", n=f"{my_pos.quantity:,.4f}")),
            )
            + "</div>"
        )
    else:
        trend = tr("ticker.price_above") if last >= sma20 else tr("ticker.price_below")
        parts.append(_muted(f"{rsi_txt} · SMA20 {sma20:,.2f} · {trend}"))
    return (
        '<div style="display:flex;flex-direction:column;gap:14px">'
        + "".join(parts)
        + "</div>"
    )


def _position_metrics(cols, pos, last: float) -> None:
    """Position metrics: market value (native + EUR), portfolio weight,
    unrealised P/L vs FIFO average cost, and average buy price."""
    value_native = pos.quantity * last
    pnl_native = value_native - pos.cost_native
    pnl_pct = (last / pos.avg_cost_native - 1) * 100 if pos.avg_cost_native else 0.0
    values_base = _position_values(str(auth.db_path()), REPORT_CCY)
    total = sum(values_base.values())
    value = values_base.get(pos.ticker)

    p1, p2, p3, p4 = cols
    p1.metric(tr("ticker.position_value"), f"{value_native:,.2f} {pos.currency}")
    if value:
        p1.caption(f"≈ {REPORT_SYM}{value:,.2f}")
    p2.metric(
        tr("ticker.pct_portfolio"),
        f"{value / total * 100:.1f}%" if value and total else "—",
        help=tr("ticker.pct_portfolio_help"),
    )
    p3.metric(
        tr("ticker.unrealised_pl"),
        f"{pnl_native:+,.2f} {pos.currency}",
        f"{pnl_pct:+.2f}%",
        help=tr("ticker.unrealised_pl_help"),
    )
    p4.metric(tr("ticker.avg_buy_price"), f"{pos.avg_cost_native:,.2f} {pos.currency}")
    p4.caption(tr("ticker.n_shares", n=f"{pos.quantity:,.4f}"))


# Drag-zooming the price chart picks a custom window, but Plotly reports that
# as a client-side relayout and Streamlit surfaces no relayout event (only
# box/lasso *selections*, which would cost the drag-to-zoom gesture and a
# server round-trip per drag). So the change over the picked window is computed
# in the browser: the price series rides along in the readout slot's data
# attributes — Plotly serializes numeric arrays as base64 ({"dtype", "bdata"}),
# so gd.data is not readable from JS without decoding it — and the picked
# window is read off the chart's own layout once the drag ends. No rerun, so
# the line lands as the mouse comes up. Desktop only: phones pin both axes.
_RANGE_JS = r"""
<script>
(function () {
  if (window.__topstocksRangeChange) return;  /* wire once per session */
  window.__topstocksRangeChange = true;
  /* Axis range endpoints arrive as "2026-06-28 15:07:03.5225" (space, and a
     sub-millisecond fraction outside the ISO grammar Safari holds to); our own
     stamps are ISO. Neither carries a zone — the frame is exchange-local wall
     time, as the chart's — so both parse alike and only their span is used. */
  const ms = (v) => {
    if (typeof v === "number") return v;
    return Date.parse(String(v).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  };
  /* The price chart is the one carrying a meta="price" trace; every other
     chart on the page keeps its own zoom to itself. */
  const chart = () =>
    Array.prototype.find.call(
      document.querySelectorAll(".js-plotly-plot"),
      (gd) => (gd.data || []).some((t) => t.meta === "price")
    );
  const clear = (b) => {
    if (b) { b.textContent = ""; b.style.display = "none"; }
  };
  const read = () => {
    /* Both nodes are looked up per event, never captured: Streamlit replaces
       them on every rerun (period or chart-type switch), and the fresh slot
       starts out empty. */
    const gd = chart();
    const b = document.querySelector(".ts-range-change");
    if (!gd || !b) return;
    const ax = (gd.layout || {}).xaxis || {};
    if (ax.autorange || !ax.range) { clear(b); return; }  /* back to the window */
    const xs = (b.dataset.x || "").split(",");
    const ys = (b.dataset.y || "").split(",");
    const lo = Math.min(ms(ax.range[0]), ms(ax.range[1]));
    const hi = Math.max(ms(ax.range[0]), ms(ax.range[1]));
    let i0 = -1, i1 = -1;
    for (let i = 0; i < xs.length; i++) {
      if (!ys[i]) continue;  /* gap in the series */
      const t = ms(xs[i]);
      if (!(t >= lo && t <= hi)) continue;
      if (i0 < 0) i0 = i;
      i1 = i;
    }
    if (i0 < 0 || i1 === i0) { clear(b); return; }  /* under two bars in view */
    const a = parseFloat(ys[i0]), z = parseFloat(ys[i1]);
    if (!a || isNaN(z)) { clear(b); return; }
    const pct = (z / a - 1) * 100;
    /* Intraday windows land inside one date: label those with the clock. */
    const fmt = new Intl.DateTimeFormat(b.dataset.locale || undefined,
      ms(xs[i1]) - ms(xs[i0]) < 2 * 864e5
        ? {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}
        : {day: "numeric", month: "short", year: "2-digit"});
    b.textContent = (b.dataset.tmpl || "{pct} {start} {end}")
      .replace("{pct}", (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%")
      .replace("{start}", fmt.format(new Date(ms(xs[i0]))))
      .replace("{end}", fmt.format(new Date(ms(xs[i1]))));
    b.style.color = pct >= 0 ? b.dataset.up : b.dataset.down;
    b.style.display = "";
  };
  /* Deliberately not gd.on("plotly_relayout"): Streamlit re-plots the same
     div on a fragment rerun, and Plotly.newPlot purges the handlers off it
     while the element (so any "already wired" mark on it) survives — the
     readout then goes dead for the rest of the session. Document-level
     listeners outlive every remount. The drag ends on mouseup, the reset on
     dblclick, and the modebar buttons on their own click; the timeout lets
     Plotly land the relayout before the layout is read.
     The listeners take no target filter: Plotly covers the whole document
     with a .dragcover while a drag is live, so the mouseup that ends a zoom
     lands outside the chart. read() is a no-op whenever the axis is not
     zoomed, which is every other click on the page. */
  const after = () => setTimeout(read, 80);
  document.addEventListener("mouseup", after, true);
  document.addEventListener("dblclick", after, true);
})();
</script>
"""


def _range_readout(box, df: pd.DataFrame) -> None:
    """Empty slot under the period change, filled by _RANGE_JS on zoom."""
    xs = ",".join(df.index.strftime("%Y-%m-%dT%H:%M"))
    ys = ",".join("" if pd.isna(v) else f"{v:.4f}" for v in df["Close"])
    # The catalog string formatted with its own placeholders back in, so the
    # browser substitutes the numbers into a translated template.
    tmpl = tr("ticker.range_change", pct="{pct}", start="{start}", end="{end}")
    box.html(
        '<div class="ts-range-change"'
        f' style="display:none;font-size:{FS_XS};line-height:1.6"'
        f' data-x="{xs}" data-y="{ys}"'
        f' data-locale="{html.escape(active_language(), quote=True)}"'
        f' data-up="{UP_COLOR}" data-down="{DOWN_COLOR}"'
        f' data-tmpl="{html.escape(tmpl, quote=True)}"></div>' + _RANGE_JS,
        unsafe_allow_javascript=True,
    )


@st.fragment
def _price_section(ticker: str) -> None:
    """Price metrics, period selector and candlestick chart.

    A fragment: switching the period reruns only this block, leaving the
    fundamentals / insiders / comps sections below untouched.
    """
    # Reserve the metrics slot above the period buttons: widget values return
    # inline where the widget is defined, but the metrics need the fetched df.
    # It shimmers meanwhile, so switching period leaves the pills sitting on a
    # placeholder instead of jumping to the top of an empty card.
    metrics_slot = skeletons.reserve("metrics", n=(1, 2, 2) if _MOBILE else 3)
    # Phones: 8 period pills overflow a 360px viewport — drop the two
    # in-between ranges (6m/2y) there.
    period_opts = (
        [p for p in PERIODS if p not in ("6m", "2y")] if _MOBILE else list(PERIODS)
    )
    # Desktop: period pills and the candles/line toggle share one full-width
    # row — "distribute" pins the pills left and the toggle to the right edge.
    # Phones stack them (each pill set already fills a 360px viewport alone).
    default_chart = "line" if _MOBILE else "candles"
    row = st.container(
        horizontal=not _MOBILE,
        horizontal_alignment="distribute",
        vertical_alignment="center",
    )

    with row:
        sel = st.segmented_control(
            tr("ticker.period"),
            period_opts,
            default="1y",
            format_func=lambda p: tr(f"ticker.period_{p}"),
            key="period_sel",
            label_visibility="collapsed",
        )
        sel = sel or "1y"  # segmented_control returns None if the user clears it.

        # Candlesticks pack ~250 bars into a ~390px phone canvas — the bodies
        # collapse to sub-pixel mush. Default phones to a Close line (readable),
        # desktop to candles (OHLC useful); either way the toggle lets you flip.
        chart_type = st.segmented_control(
            tr("ticker.chart_type"),
            ["candles", "line"],
            default=default_chart,
            format_func=lambda o: tr(f"ticker.chart_{o}"),
            key="chart_type_sel",
            label_visibility="collapsed",
        )
        chart_type = chart_type or default_chart

    # Second slot, below the controls: the candlestick canvas at its final
    # height, so the sections under it keep their place across the fetch.
    chart_slot = skeletons.reserve("chart", height=440, shape="bars", bars=30)
    try:
        df = _history(ticker, sel)
    except (YFRateLimitError, URLError) as exc:
        # A fragment rerun (period switch) lands here with app.py off the
        # stack — re-raising would surface Streamlit's crash card.
        notices.data_toast(exc)
        metrics_slot.clear()
        chart_slot.clear()
        return
    except Exception:
        metrics_slot.clear()
        chart_slot.container().warning(tr("ticker.history_failed"))
        return
    # Delisted tickers return an empty frame without raising; the metrics
    # below index the last two closes.
    if df.empty or len(df["Close"].dropna()) < 2:
        metrics_slot.clear()
        chart_slot.container().info(tr("ticker.history_empty"))
        return
    db = str(auth.db_path())
    ledger_txs = _ledger(db)
    my_trades = [
        t for t in ledger_txs if t.ticker == ticker and t.action in ("buy", "sell")
    ]
    my_pos = _held(db).get(ticker)

    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2])
    # The last two daily closes miss the extended-hours move entirely, so a
    # -13% premarket gap on earnings would still render as yesterday's session.
    # Off-session the hero's level and day % come from the quote instead: the
    # pre/after-hours price while that window trades, else the last close.
    live = _live_quote(ticker)
    extended = bool(live and live["session"])
    price_now = live["price"] if extended else last
    day_pct = live["pct"] * 100 if live else (last - prev) / prev * 100
    # The level is no longer a close, so the hero says which session it is.
    session_note = tr(f"ticker.session_{live['session']}") if extended else ""
    if _MOBILE:
        # Phone summary per the design: price hero + 2×2 position tiles.
        # Built before the slot is claimed: _mobile_summary_html triggers its
        # own position-values fetch, which belongs under the shimmer.
        summary = _mobile_summary_html(
            df, sel, my_pos, db, price_now, day_pct, session_note
        )
        metrics_slot.container().html(summary)
    else:
        with metrics_slot.container():
            # One row: price/RSI/SMA plus the position block when the ticker
            # is held.
            cols = metric_cells(7 if my_pos else 3)
            c1, c2, c3 = cols[:3]
            c1.metric(
                tr("ticker.price") + (f" · {session_note}" if session_note else ""),
                f"{price_now:,.2f}",
                f"{day_pct:+.2f}%",
            )
            # Change over the selected range: df is already trimmed to the
            # display window, so the first Close is the period's start.
            first = float(df["Close"].iloc[0])
            period_pct = (last - first) / first * 100
            pct_md = f":{'green' if period_pct >= 0 else 'red'}[**{period_pct:+.2f}%**]"
            c1.caption(
                tr(
                    "ticker.period_change",
                    pct=pct_md,
                    period=tr(f"ticker.period_{sel}"),
                )
            )
            # Second line, hidden until the reader drag-zooms the chart to a
            # window of their own (see _RANGE_JS).
            _range_readout(c1, df)

            rsi_val = float(df["RSI14"].iloc[-1])
            c2.metric(
                tr("ticker.rsi_label"), f"{rsi_val:.1f}", help=tr("ticker.rsi_help")
            )
            c2.caption(verdict_md("rsi", rsi_val))

            sma20 = float(df["SMA20"].iloc[-1])
            c3.metric(
                tr("ticker.sma20_label"), f"{sma20:,.2f}", help=tr("ticker.sma20_help")
            )
            # Price vs its 20-day average: a quick trend read beside the level.
            c3.caption(
                f":green[{tr('ticker.price_above')}]"
                if last >= sma20
                else f":red[{tr('ticker.price_below')}]"
            )

            if my_pos:
                _position_metrics(cols[3:], my_pos, last)

    fig = go.Figure()
    if chart_type == "line":
        # DS line-chart spec: 2px accent price line over a vertical gradient
        # area (accent 22% at the line fading to 0). fill="tozeroy" would drag
        # the y-autorange down to 0, so the fade runs against an invisible
        # baseline pinned at the window's low instead.
        floor = float(df["Close"].min())
        fig.add_trace(
            go.Scatter(
                x=df.index, y=[floor] * len(df.index),
                mode="lines", line=dict(width=0),
                hoverinfo="skip", showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=df.index, y=df["Close"], name=tr("ticker.price"),
                mode="lines", meta="price",
                line=dict(color=BRAND_ACCENT, width=2),
                fill="tonexty",
                fillgradient=dict(
                    type="vertical",
                    colorscale=[(0.0, TRANSPARENT), (1.0, ACCENT_AREA)],
                ),
                hovertemplate=tr("ticker.price") + "  <b>%{y:,.2f}</b><extra></extra>",
            )
        )
    else:
        fig.add_trace(
            go.Candlestick(
                x=df.index,
                open=df["Open"],
                high=df["High"],
                low=df["Low"],
                close=df["Close"],
                name=tr("ticker.price"),
                meta="price",
                increasing_line_color=CANDLE_UP,
                increasing_fillcolor=CANDLE_UP,
                decreasing_line_color=CANDLE_DOWN,
                decreasing_fillcolor=CANDLE_DOWN,
            )
        )
    fig.add_trace(
        go.Scatter(
            x=df.index, y=df["SMA20"], name="SMA20",
            line=dict(color=SMA_FAST, width=1.5),
            hovertemplate="SMA20  <b>%{y:,.2f}</b><extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df.index, y=df["SMA50"], name="SMA50",
            line=dict(color=SMA_SLOW, width=1.5),
            hovertemplate="SMA50  <b>%{y:,.2f}</b><extra></extra>",
        )
    )

    # Your own buys/sells from the imported ledger, at trade date × trade price.
    if my_trades:
        def _chart_ts(day: str) -> pd.Timestamp:
            ts = pd.Timestamp(day)
            return ts.tz_localize(df.index.tz) if df.index.tz is not None else ts

        chart_start = df.index[0]
        avg_cost = my_pos.avg_cost_native if my_pos else 0.0
        # White triangles with a canvas-dark outline per the design — the
        # markers must read against candles of either color.
        for action, symbol, color in (
            ("buy", "triangle-up", TEXT_PRIMARY),
            ("sell", "triangle-down", DOWN_COLOR),
        ):
            pts = [
                (t, _chart_ts(t.date))
                for t in my_trades
                if t.action == action and _chart_ts(t.date) >= chart_start
            ]
            if not pts:
                continue
            if action == "buy":
                # Per-buy return vs today's close, plus the position's average
                # cost so each lot can be compared against the blended entry.
                hover = []
                for t, _ in pts:
                    pct = (last / t.price - 1) * 100 if t.price else 0.0
                    pct_color = UP_COLOR if pct >= 0 else DOWN_COLOR
                    text = tr(
                        "ticker.hover_buy",
                        qty=f"{t.quantity:.4f}",
                        price=f"{t.price:,.2f}",
                        date=t.date,
                        color=pct_color,
                        pct=f"{pct:+.2f}",
                        last=f"{last:,.2f}",
                    )
                    if avg_cost:
                        text += tr("ticker.hover_avg_buy", avg=f"{avg_cost:,.2f}")
                    hover.append(hover_wrap(text))
                customdata = hover
                hovertemplate = "%{customdata}<extra></extra>"
            else:
                customdata = [t.quantity for t, _ in pts]
                hovertemplate = hover_wrap(tr("ticker.hover_sell_tmpl"))
            fig.add_trace(
                go.Scatter(
                    x=[x for _, x in pts],
                    y=[t.price for t, _ in pts],
                    mode="markers",
                    name=(
                        tr("ticker.my_buys")
                        if action == "buy"
                        else tr("ticker.my_sells")
                    ),
                    marker=dict(
                        symbol=symbol, size=12, color=color,
                        line=dict(width=1.5, color=SURFACE_PAGE),
                    ),
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                )
            )

    # Dividends (d) and results dates (r) as dotted verticals with a letter
    # tag on top, plus a small diamond at the bar high whose hover carries the
    # detail (amount & yield, EPS vs estimate & surprise, move across print).
    ev = _event_markers(df, ticker, daily=PERIODS[sel][1] == "1d")
    for kind, (name, color) in _EVENT_KINDS.items():
        pts = [(ts, txt) for k, ts, txt in ev if k == kind]
        if not pts:
            continue
        for ts, _ in pts:
            # Neutral dashed vertical, colored letter tag on top (the design
            # keeps the guide line quiet and lets the tag carry the meaning).
            fig.add_vline(
                x=ts,
                line_dash="dot",
                line_width=1,
                line_color=EVENT_LINE,
                opacity=0.8,
                annotation_text=kind,
                annotation_position="top",
                annotation_font=dict(size=11, color=color),
            )
        fig.add_trace(
            go.Scatter(
                x=[ts for ts, _ in pts],
                y=[float(df.at[ts, "High"]) * 1.01 for ts, _ in pts],
                mode="markers",
                name=name,
                marker=dict(
                    symbol="diamond", size=7, color=color,
                    line=dict(width=1, color=SURFACE_PAGE),
                ),
                customdata=[hover_wrap(txt) for _, txt in pts],
                hovertemplate="%{customdata}<extra></extra>",
            )
        )

    fig.update_layout(
        **chart_layout(height=440, top_legend=True),
        xaxis_rangeslider_visible=False,
        yaxis=dict(fixedrange=True),
        # One schematic box per date: OHLC, both SMAs and any trade markers
        # together, instead of chasing each trace with the cursor.
        hovermode="x unified",
    )
    fig.update_xaxes(rangebreaks=_rangebreaks(df, PERIODS[sel][1]))
    if _MOBILE:
        # DS mobile chart spec: the date axis thins to ~3 labels on a phone.
        fig.update_xaxes(nticks=3)
    show_chart(fig, container=chart_slot.container())

    # Card footer per the design: the most recent buy at a glance — size @
    # price · date · return to the current price (· blended average cost).
    buys = [t for t in my_trades if t.action == "buy" and t.price]
    if buys:
        last_buy = max(buys, key=lambda t: t.date)
        pct = (last / last_buy.price - 1) * 100
        if _MOBILE:
            pct_md = (
                f'<span style="color:{UP_COLOR if pct >= 0 else DOWN_COLOR};'
                f'font-weight:600">{pct:+.2f}%</span>'
            )
        else:
            pct_md = f":{'green' if pct >= 0 else 'red'}[**{pct:+.2f}%**]"
        line = tr(
            "ticker.last_buy",
            qty=f"{last_buy.quantity:.4f}",
            price=f"{last_buy.price:,.2f}",
            date=last_buy.date,
            pct=pct_md,
            last=f"{last:,.2f}",
        )
        if my_pos and my_pos.avg_cost_native:
            line += tr("ticker.last_buy_avg", avg=f"{my_pos.avg_cost_native:,.2f}")
        if _MOBILE:
            # The design's phone frame boxes this line in its own inset tile.
            line = re.sub(
                r"\*\*(.+?)\*\*",
                rf'<strong style="color:{TEXT_PRIMARY}">\1</strong>',
                line,
            )
            st.html(
                '<div style="display:flex;align-items:flex-start;gap:8px;'
                f"background:{SURFACE_PAGE};border:1px solid {BORDER};"
                f"border-radius:{RADIUS_MD};"
                f'padding:12px;font-size:{FS_SM};color:{TEXT_SECONDARY};'
                'line-height:1.45">'
                '<span style="flex-shrink:0">▲</span><span>' + line + "</span></div>"
            )
        else:
            st.divider()
            st.caption(f"▲ {line}")


# Header + favorite/tag actions. Outside the price fragment on purpose:
# toggling the star or editing tags then reruns the whole app, so the
# sidebar list (favorites-first order, star marks, tag search) stays in sync.
# Off-watchlist symbols (SEC search / held-only) fall back to the map name.
# One flex row per the design: logo chip · bold symbol · muted name ·
# "in portfolio" badge, with the actions pinned to the right edge.
label = labels.get(ticker) or company_name(ticker) or ticker
src = _logo(ticker)


def _header_html() -> str:
    logo_px = 30 if _MOBILE else 36
    parts = []
    if src:
        parts.append(
            f'<img src="{html.escape(src)}" alt="" style="width:{logo_px}px;'
            f"height:{logo_px}px;border-radius:{RADIUS_SM};"
            # Opaque plate behind transparent brand marks — neutral-50, the
            # lightest step on the DS ramp.
            f"background:{TEXT_PRIMARY};"
            f"border:1px solid {BORDER};box-sizing:border-box;"
            f'padding:{4 if _MOBILE else 5}px;object-fit:contain">'
        )
    show_name = label.upper() != ticker
    if _MOBILE:
        # Phones stack symbol over the name (the design's compact app bar);
        # min-width:0 + ellipsis keep long names from pushing the badge off.
        name_line = (
            f'<span style="font-size:{FS_SM};color:{TEXT_MUTED};overflow:hidden;'
            f'text-overflow:ellipsis;white-space:nowrap">{html.escape(label)}</span>'
            if show_name
            else ""
        )
        parts.append(
            '<div style="display:flex;flex-direction:column;min-width:0">'
            f'<span style="font-size:{FS_LG};font-weight:600">'
            f"{html.escape(ticker)}</span>"
            + name_line
            + "</div>"
        )
    else:
        parts.append(
            f'<h1 style="font-size:{FS_3XL};font-weight:600;line-height:1.21;'
            f'padding:0;margin:0">{html.escape(ticker)}</h1>'
        )
        if show_name:
            parts.append(
                f'<span style="font-size:{FS_BASE};color:{TEXT_MUTED}">'
                f"{html.escape(label)}</span>"
            )
    db = str(auth.db_path())
    if ticker in _held(db):
        # Purple-800 fill / purple-300 text — the DS brand badge pair on dark.
        parts.append(
            f'<span style="background:{PURPLE_800};color:{PURPLE_300};'
            f"font-size:{FS_XS};font-weight:600;border-radius:{RADIUS_XS};"
            "padding:2px 8px;white-space:nowrap;"
            f'{"margin-left:auto" if _MOBILE else ""}">'
            f'{html.escape(tr("ticker.in_portfolio"))}</span>'
        )
        # Whose account the shares sit in, right beside the badge: one brand
        # mark per custodian (two when a holding is split), each one's share
        # of the position on its tooltip.
        parts.append(
            broker_chips_html(
                mix(_custody(db).get(ticker, {})), size=20 if _MOBILE else 22
            )
        )
    return (
        '<div style="display:flex;align-items:center;gap:'
        + ("10px" if _MOBILE else "12px")
        + (";flex-wrap:wrap" if not _MOBILE else "")
        + '">'
        + "".join(parts)
        + "</div>"
    )


row = st.container(horizontal=True, vertical_alignment="center")
row.container(width="stretch").html(_header_html())
ticker_actions(ticker, container=row, key="page")

with st.container(border=True):
    _price_section(ticker)


@st.cache_data(ttl=3600, show_spinner=False)
def _crypto_info(t: str) -> dict:
    """Snapshot info for a crypto pair (market cap, volume, supply)."""
    import yfinance as yf

    from stocks.data.fetch import resolve

    try:
        return yf.Ticker(resolve(t)).info or {}
    except Exception:
        return {}


def _crypto_section(t: str) -> None:
    """Asset stats for a coin — the crypto stand-in for the fundamentals,
    insiders and comps blocks, none of which exist for crypto."""
    st.subheader(tr("ticker.asset_stats"))
    # The coin snapshot is a live yfinance info call; four tiles held open.
    box = skeletons.reserve("metrics", n=4)
    info = _crypto_info(t)
    _, quote = split_pair(t) or (t, "USD")
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(quote, "")
    cap = info.get("marketCap")
    vol = info.get("volume24Hr") or info.get("volume")
    supply = info.get("circulatingSupply")
    hi, lo = info.get("fiftyTwoWeekHigh"), info.get("fiftyTwoWeekLow")
    na = tr("ticker.na")

    with box.container():
        st.html(kpi_grid_html([
            (tr("ticker.market_cap"),
             compact_money(cap, sym) if cap else na, None, None),
            (tr("ticker.volume_24h"),
             compact_money(vol, sym) if vol else na, None, None),
            (tr("ticker.circulating_supply"),
             compact_money(supply, "") if supply else na, None, None),
            (tr("ticker.range_52w"),
             f"{lo:,.0f} – {hi:,.0f}" if lo and hi else na, None, None),
        ]))
        st.caption(tr("ticker.crypto_caption", quote=quote))


# Crypto pairs stop after price + stats: statements, Form 4 filings and
# comparables below are meaningless for a coin (and each costs a fetch).
if is_crypto(ticker):
    with st.container(border=True):
        _crypto_section(ticker)
    st.stop()


# ------------------------------------------------------------------ funds
@st.cache_data(ttl=86400, show_spinner=False)
def _fund(t: str) -> FundProfile | None:
    """Fund profile for `t`, None when it's a stock.

    `is_fund` answers from the catalog or the learned type cache for every
    symbol seen before, so a stock pays one `.info` lookup ever and a fund
    pays a second one for its basket — cached for a day, which is faster than
    holdings and costs actually move.
    """
    return fetch_profile(t) if is_fund(t) else None


def _fund_pct(value: float | None, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%" if value is not None else tr("ticker.na")


def _fund_kpis(p: FundProfile) -> None:
    """Cost, size and income tiles — the fund's answer to the KPI grid.

    Yahoo's coverage of UCITS lines is thinner than of US funds (no AUM, no
    category, rarely a yield), so every tile stands on its own and prints n/a
    rather than the section hiding when one field is missing. A bond sleeve
    swaps the top-10 concentration tile — meaningless when Yahoo publishes no
    basket for it — for effective duration, the number that actually decides
    what a rate move does to it.
    """
    sym = auth.CURRENCY_SYMBOL.get(str(p.currency or "").upper(), "")
    tiles = [
        (tr("ticker.fund_ter"), _fund_pct(p.expense_ratio, 2), None,
         tr("ticker.fund_ter_help")),
        (tr("ticker.fund_aum"),
         compact_money(p.aum, sym) if p.aum else tr("ticker.na"), None,
         tr("ticker.fund_aum_help")),
        (tr("ticker.fund_yield"), _fund_pct(p.dividend_yield, 2), None,
         tr("ticker.fund_yield_help")),
        (tr("ticker.fund_turnover"), _fund_pct(p.turnover, 1), None,
         tr("ticker.fund_turnover_help")),
    ]
    if p.is_bond_fund or (p.bond_duration and not p.holdings):
        tiles.append((
            tr("ticker.fund_duration"),
            f"{p.bond_duration:.2f}" if p.bond_duration else tr("ticker.na"),
            None,
            tr("ticker.fund_duration_help"),
        ))
    else:
        tiles.append((
            tr("ticker.fund_top10"),
            _fund_pct(p.disclosed_weight, 1) if p.holdings else tr("ticker.na"),
            None,
            tr("ticker.fund_top10_help"),
        ))
    st.html(kpi_grid_html(tiles))


def _fund_holdings_table(p: FundProfile) -> None:
    """The disclosed basket, each line linked to its own page.

    Yahoo publishes the top ten only, so the caption says what the rows add up
    to — a reader who sees ten names must not read them as the whole fund.
    """
    if not p.holdings:
        st.caption(tr("ticker.fund_no_holdings"))
        return
    frame = pd.DataFrame(
        [{"ticker": h.symbol, "weight": h.weight} for h in p.holdings]
    )
    st.html(
        ticker_table_html(
            frame,
            fmt={"weight": "{:.2%}"},
            labels={"ticker": tr("ticker.fund_holding"),
                    "weight": tr("ticker.fund_weight")},
            mobile={"value": "weight"},
        )
    )
    st.caption(
        tr(
            "ticker.fund_holdings_caption",
            n=len(p.holdings),
            pct=_fund_pct(p.disclosed_weight, 1),
        )
    )


def _fund_exposure_chart(p: FundProfile) -> None:
    """Sector weights as a horizontal bar — the basket's real bet.

    Bars, not a donut: the reader's question here is "how big is the tech
    sleeve", which is a length comparison, and sector names are too long to
    ride slices.
    """
    if not p.sectors:
        return
    labels = [label for label, _ in p.sectors][::-1]
    values = [weight * 100 for _, weight in p.sectors][::-1]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker=dict(color=BRAND_ACCENT),
            hovertemplate="<b>%{y}</b>  %{x:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        # Height grows with the bucket count so eleven sectors don't compress
        # into unreadable 8px bars.
        **chart_layout(
            title=tr("ticker.fund_sectors"), height=max(220, 26 * len(labels) + 90)
        ),
        xaxis=dict(ticksuffix="%", fixedrange=True),
        yaxis=dict(fixedrange=True),
        showlegend=False,
    )
    show_chart(fig)


def _fund_section(p: FundProfile) -> None:
    """Everything a fund has instead of fundamentals: cost, basket, exposure."""
    st.subheader(tr("ticker.fund_profile"))
    meta = " · ".join(
        x for x in (p.legal_type, p.category, p.family) if x
    )
    if meta:
        st.caption(meta)
    _fund_kpis(p)
    if p.asset_classes:
        st.caption(
            tr("ticker.fund_assets") + "  "
            + " · ".join(f"{label} {w * 100:.1f}%" for label, w in p.asset_classes)
        )
    _fund_exposure_chart(p)
    _fund_holdings_table(p)
    st.caption(tr("ticker.fund_caption"))


# Funds stop here for the same reason coins do: a wrapper has no statements,
# no insider filings and no comparables, and every one of those sections
# costs a fetch to render an empty card.
_fund_profile = _fund(ticker)
if _fund_profile is not None:
    with st.container(border=True):
        _fund_section(_fund_profile)
    st.stop()


def _yoy_pct(cur: float | None, prev: float | None) -> float | None:
    """Growth measured against the magnitude of the base, so a loss deepening
    from -56M to -187M reads -233% instead of the +233% a plain cur/prev - 1
    reports once the two negatives cancel. None when there is no usable base."""
    if cur is None or prev is None or pd.isna(cur) or pd.isna(prev) or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def _annual_combined_chart(fin: pd.DataFrame, proj: pd.DataFrame) -> None:
    """Grouped revenue / net income bars with diluted EPS overlaid on a
    right-hand axis; forward consensus appended for both (hatched translucent
    bars, dashed line with a shaded low-high analyst range)."""
    years = [str(y) for y in fin.index]
    rev_est = proj["Revenue"].dropna() if "Revenue" in proj else pd.Series(dtype=float)
    if "Revenue" not in fin:
        rev_est = pd.Series(dtype=float)  # forecast bars only extend actual ones
    eps = fin["EPS"].dropna() if "EPS" in fin else pd.Series(dtype=float)
    eps_est = proj["EPS"].dropna() if "EPS" in proj else pd.Series(dtype=float)
    if eps.empty:
        eps_est = pd.Series(dtype=float)
    # Only forecast years actually carrying an estimate become x categories.
    est_years = [
        y
        for y in (list(proj.index) if not proj.empty else [])
        if y in rev_est.index or y in eps_est.index
    ]

    fig = go.Figure()
    has_bars = False
    for col, label in (
        ("Revenue", tr("ticker.revenue")),
        ("Net Income", tr("ticker.net_income")),
    ):
        if col not in fin:
            continue
        has_bars = True
        s = fin[col]
        # Year-over-year growth as the per-bar label (blank for first year).
        yoy = [_yoy_pct(cur, prev) for prev, cur in zip(s.shift(), s, strict=True)]
        x = list(years)
        y = list(s)
        text = [f"{v:+.0f}%" if v is not None else "" for v in yoy]
        kind = ["reported"] * len(x)
        pattern = [""] * len(x)
        opacity = [1.0] * len(x)
        # Forecast only exists for revenue — net income has no consensus row.
        if col == "Revenue" and not rev_est.empty:
            prev = s.dropna().iloc[-1] if s.notna().any() else None
            for lbl, v in rev_est.items():
                x.append(lbl)
                y.append(v)
                pct = _yoy_pct(v, prev)
                text.append(f"{pct:+.0f}%" if pct is not None else "")
                kind.append(
                    tr("ticker.k_extrapolated")
                    if proj.loc[lbl, "RevenueExt"]
                    else tr("ticker.k_consensus")
                )
                pattern.append("/")
                opacity.append(0.5)
                prev = v
        fig.add_trace(
            go.Bar(
                x=x,
                y=y,
                name=_legend(label, s),
                text=text,
                textposition="outside",
                # [kind, yoy-string] per bar; "—" when no prior year to compare.
                customdata=list(zip(kind, [t or "—" for t in text], strict=True)),
                marker=dict(pattern=dict(shape=pattern), opacity=opacity),
                hovertemplate=hover_wrap(
                    f"{label}  <b>%{{y:$.3s}}</b>"
                    " · YoY %{customdata[1]} · %{customdata[0]}<extra></extra>"
                ),
            )
        )

    # EPS lives on its own axis — dollars-per-share next to billions would
    # flatline on the bar scale. One hue for all EPS traces, distinct from
    # the bar colors (color follows the series, not the trace slot).
    eps_axis = "y2" if has_bars else "y"
    violet = BRAND_ACCENT
    if not eps.empty:
        eps_years = [str(y) for y in eps.index]
        if not eps_est.empty:
            band = proj.loc[eps_est.index, ["EPSLow", "EPSHigh"]].dropna()
            if not band.empty:
                # Anchored at the last reported point so the range fans out of it.
                bx = [eps_years[-1]] + list(band.index)
                last = float(eps.iloc[-1])
                fig.add_trace(
                    go.Scatter(
                        x=bx,
                        y=[last] + list(band["EPSHigh"]),
                        yaxis=eps_axis,
                        mode="lines",
                        line=dict(width=0),
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=bx,
                        y=[last] + list(band["EPSLow"]),
                        yaxis=eps_axis,
                        mode="lines",
                        line=dict(width=0),
                        fill="tonexty",
                        fillcolor=ACCENT_BAND,
                        hoverinfo="skip",
                        showlegend=False,
                    )
                )
        fig.add_trace(
            go.Scatter(
                x=eps_years,
                y=eps,
                yaxis=eps_axis,
                name=tr("ticker.eps_reported"),
                mode="lines+markers",
                line=dict(color=violet, width=2),
                marker=dict(size=7),
                hovertemplate=hover_wrap(tr("ticker.eps_hover_reported")),
            )
        )
        if not eps_est.empty:
            kind = [
                tr("ticker.k_extrapolated")
                if proj.loc[i, "EPSExt"]
                else tr("ticker.k_consensus")
                for i in eps_est.index
            ]
            fig.add_trace(
                go.Scatter(
                    # Anchor at the last reported point so the dashed line
                    # continues the solid one instead of floating.
                    x=[eps_years[-1]] + list(eps_est.index),
                    y=[eps.iloc[-1]] + list(eps_est.values),
                    yaxis=eps_axis,
                    name=tr("ticker.eps_consensus"),
                    mode="lines+markers",
                    line=dict(color=violet, width=2, dash="dash"),
                    marker=dict(
                        size=8,
                        symbol="circle-open",
                        opacity=[0.0] + [1.0] * len(eps_est),
                    ),
                    customdata=[tr("ticker.k_reported")] + kind,
                    hovertemplate=hover_wrap(
                        "EPS  <b>%{y:.2f}</b> · %{customdata}<extra></extra>"
                    ),
                )
            )

    if not rev_est.empty or not eps_est.empty:
        fig.add_vline(x=len(years) - 0.5, **_FORECAST_DIVIDER)
    layout = dict(
        **chart_layout(
            title=tr("ticker.chart_annual_title"),
            height=320,
            top_legend=True,
        ),
        barmode="group",
        # One box per year listing revenue, net income and EPS together.
        hovermode="x unified",
        # Numeric-looking year strings must stay categories: on an inferred
        # linear axis the vline at index 3.5 stretches the range to ~2029.
        xaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=years + est_years,
        ),
        yaxis=dict(fixedrange=True, tickformat="~s"),
    )
    if has_bars and not eps.empty:
        layout["yaxis2"] = dict(
            overlaying="y",
            side="right",
            fixedrange=True,
            showgrid=False,
            title=dict(text="EPS", font=dict(size=11)),
        )
    fig.update_layout(**layout)
    show_chart(fig)
    if not rev_est.empty or not eps_est.empty:
        st.caption(tr("ticker.annual_caption"))


@st.fragment
def _financials_section(ticker: str, fin: pd.DataFrame, proj: pd.DataFrame) -> None:
    """Annual view: one combined revenue / net income / EPS chart with the
    consensus path. Quarterly view: EPS as reported. A fragment so flipping
    the view reruns only this block."""
    eps_q = quarterly_eps(_raw(ticker))
    has_annual = bool({"Revenue", "Net Income"} & set(fin.columns)) or (
        "EPS" in fin and fin["EPS"].notna().any()
    )

    v_annual = tr("ticker.view_annual")
    v_quarterly = tr("ticker.view_quarterly")
    views = []
    if has_annual:
        views.append(v_annual)
    if not eps_q.empty:
        views.append(v_quarterly)
    if not views:
        return
    view = views[0]
    if len(views) > 1:
        view = (
            st.segmented_control(
                tr("ticker.financials_view"),
                views,
                default=views[0],
                key="eps_view",
                label_visibility="collapsed",
            )
            or views[0]
        )

    if view == v_quarterly:
        ef = go.Figure()
        ef.add_trace(
            go.Scatter(
                x=[str(p) for p in eps_q.index],
                y=eps_q["EPS"],
                name="EPS",
                mode="lines+markers",
                line=dict(dash="dot"),
                hovertemplate="<b>%{x}</b><br>EPS  <b>%{y:.2f}</b><extra></extra>",
            )
        )
        ef.update_layout(
            **chart_layout(title=tr("ticker.chart_quarterly_title")),
            yaxis=dict(fixedrange=True),
        )
        show_chart(ef)
        return

    _annual_combined_chart(fin, proj)


# Annual revenue / profit / EPS trend, straight under the price chart. The
# statement pull and the consensus projection are two more round trips, so the
# card shimmers its grouped bars until both are in.
_fin_card = skeletons.reserve(
    "chart", border=True, height=320, shape="bars", bars=12, legend=True
)
try:
    fin = annual_financials(_raw(ticker))
except (YFRateLimitError, URLError) as exc:
    notices.data_toast(exc)
    fin = pd.DataFrame()  # the section below self-hides on empty
except Exception:
    fin = pd.DataFrame()  # the section below self-hides on empty
if fin.empty:
    _fin_card.clear()  # no statements for this name — the card never appears
else:
    # Fetched before the slot is claimed so the estimates round trip also
    # happens under the shimmer.
    proj = _projection(ticker, int(fin.index[-1]))
    with _fin_card.container(border=True):
        _financials_section(ticker, fin, proj)


# ---------------------------------------------------------------- fundamentals
@st.cache_data(ttl=3600, show_spinner=False)
def _metrics(t: str) -> dict:
    return compute_metrics(_raw(t))


@st.cache_data(ttl=3600, show_spinner=False)
def _fx_usd_base(base: str) -> tuple[float, str]:
    """(rate, as_of) for USD -> the account's reporting currency."""
    return spot("USD", base)


def _kpi(label: str, key: str, help: str | None = None) -> tuple:
    """One `kpi_grid_html` tile spec: (label, value, verdict, tooltip).

    Tooltip defaults to the plain-language definition in KPI_SOURCES; pass
    `help` only to override it (e.g. the PEG reliability warning).
    """
    return (
        label,
        format_value(key, mets[key]),
        verdict(key, mets[key]),
        help or (kpi_desc(key) if key in KPI_SOURCES else None),
    )


# Nine KPI tiles off one metrics computation — reserved at full width so the
# valuation card below doesn't ride up while it lands.
_fund_card = skeletons.reserve("metrics", border=True, title=True, n=9)
try:
    mets = _metrics(ticker)
except (YFRateLimitError, URLError) as exc:
    notices.data_toast(exc)
    mets = {}
except Exception:
    mets = {}

with _fund_card.container(border=True):
    st.subheader(tr("ticker.fundamentals"))
    if not mets:
        st.caption(tr("ticker.fundamentals_failed"))
    else:
        # One HTML grid, not nine Streamlit tiles: the verdict has to sit on
        # the value's line to be unmistakably ITS verdict, and Streamlit's
        # wrapping metric row can't do that (see kpi_grid_html).
        st.html(kpi_grid_html([
            _kpi(tr("ticker.kpi_pe_ttm"), "pe_ttm"),
            _kpi(tr("ticker.kpi_pe_fwd"), "pe_fwd"),
            _kpi(tr("ticker.kpi_peg"), "peg", help=tr("ticker.kpi_peg_help")),
            _kpi(tr("ticker.kpi_ev_ebitda"), "ev_ebitda"),
            _kpi(tr("ticker.kpi_ev_sales"), "ev_sales"),
            _kpi(tr("ticker.kpi_roic"), "roic"),
            _kpi(tr("ticker.kpi_fcf_yield"), "fcf_yield"),
            _kpi(tr("ticker.kpi_net_debt_ebitda"), "net_debt_ebitda"),
            _kpi(tr("ticker.kpi_dilution"), "share_dilution"),
        ]))

        if mets.get("market_cap") and mets.get("currency") == "USD":
            try:
                rate, as_of = _fx_usd_base(REPORT_CCY)
                cap = float(mets["market_cap"]) * rate
                st.caption(
                    tr(
                        "ticker.market_cap_fx",
                        usd=format_value("market_cap", mets["market_cap"]),
                        conv=format_value("market_cap", cap),
                        ccy=REPORT_CCY,
                        rate=f"{rate:.4f}",
                        as_of=as_of,
                    )
                )
            except Exception:
                st.caption(tr("ticker.fx_unavailable"))


# ------------------------------------------------------- valuation history
# Display window per range label, in calendar days (window_stats convention).
_PE_RANGES = {"1y": 365, "3y": 1095, "5y": 1825, "10y": 3650}


@st.cache_data(ttl=3600, show_spinner=False)
def _pe_history(t: str) -> tuple[str | None, pd.Series]:
    """(source, daily P/E series) over up to 10y, via analysis.pe_history:
    split-adjusted close over the TTM diluted EPS known each day, rebuilt
    from quarterly filings (SEC EDGAR primary, FMP fallback). Empty series
    when no filings source covers the ticker or the fetch fails."""
    try:
        close = fetch_history(t, period="10y", interval="1d")["Close"]
        out = pe_vs_history(t, close=close)
        return out["source"], out["pe"]
    except Exception:
        return None, pd.Series(dtype=float)


@st.fragment
def _valuation_section(ticker: str) -> None:
    """Historic P/E chart with its own range selector, plus current / average
    / premium-vs-average KPIs recomputed for the selected range. A fragment:
    flipping the range redraws only this block."""
    st.subheader(tr("ticker.valuation_history"))
    # Rebuilding the P/E series walks ten years of closes against the TTM EPS
    # known on each of those days — slow enough on a cold cache that the card
    # would otherwise sit as a bare heading. _pe_history swallows its own
    # failures (empty series), so the slot below is always claimed.
    box = skeletons.reserve("chart", height=260, title=True)
    source, pe = _pe_history(ticker)
    with box.container():
        if pe.empty:
            st.caption(tr("ticker.pe_insufficient"))
            return

        rng = (
            st.segmented_control(
                tr("ticker.pe_range"),
                list(_PE_RANGES),
                default="5y",
                key="pe_range_sel",
                label_visibility="collapsed",
            )
            or "5y"
        )
        row = window_stats(pe, windows={rng: _PE_RANGES[rng]}).loc[rng]
        view = pe[pe.index >= pe.index.max() - pd.Timedelta(days=_PE_RANGES[rng])]
        cur, avg = float(row["current"]), float(row["mean"])
        prem, pctl = row["premium"] * 100, float(row["percentile"])

        # Own-history read off the percentile (same 80/20 bands as
        # pe_history.interpret, but localized). The verbal read rides the
        # chip's tooltip — the pill itself stays short enough for a tile.
        pctl_txt = tr("ticker.pe_percentile", p=f"{pctl:.0f}")
        if pctl >= 80:
            pctl_chip = (pctl_txt, "red")
            pctl_tip = tr("ticker.pe_above_avg")
        elif pctl <= 20:
            pctl_chip = (pctl_txt, "green")
            pctl_tip = tr("ticker.pe_below_avg")
        else:
            pctl_chip = (pctl_txt, "gray")
            pctl_tip = tr("ticker.pe_inline_avg")
        st.html(kpi_grid_html([
            (tr("ticker.kpi_pe_current"), f"{cur:.1f}",
             verdict("pe_ttm", cur), tr("ticker.kpi_pe_current_help")),
            (tr("ticker.kpi_pe_avg", rng=rng), f"{avg:.1f}",
             None, tr("ticker.kpi_pe_avg_help")),
            (tr("ticker.kpi_pe_premium"), f"{prem:+.1f}%", pctl_chip,
             f"{tr('ticker.kpi_pe_premium_help')} — {pctl_tip}"),
        ]))

        # Around earnings the two P/Es legitimately diverge: Yahoo's TTM EPS picks
        # up a press-released quarter weeks before its 10-Q/10-K lands in EDGAR,
        # while this series only steps on filing dates. Flag gaps >20% so the
        # mismatch reads as data vintage, not a bug.
        try:
            fund_pe = _metrics(ticker).get("pe_ttm")
        except Exception:
            fund_pe = None
        if fund_pe and fund_pe > 0 and cur > 0 and abs(cur - fund_pe) / fund_pe > 0.20:
            st.warning(
                tr(
                    "ticker.pe_divergence",
                    cur=f"{cur:.1f}",
                    fund=f"{fund_pe:.1f}",
                    diff=f"{abs(cur - fund_pe) / fund_pe * 100:.0f}",
                ),
                icon=":material/warning:",
            )

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=view.index,
                y=view.values,
                name="P/E",
                mode="lines",
                line=dict(color=BRAND_ACCENT, width=1.8),
                hovertemplate="P/E  <b>%{y:.1f}</b><extra></extra>",
            )
        )
        fig.add_hline(
            y=avg,
            line_dash="dash",
            line_width=1,
            line_color=TEXT_MUTED,
            annotation_text=tr("ticker.pe_avg_line", avg=f"{avg:.1f}"),
            annotation_position="top left",
            annotation_font=dict(size=11, color=TEXT_MUTED),
        )
        fig.update_layout(
            **chart_layout(title=tr("ticker.chart_pe_title"), height=260),
            hovermode="x unified",
            yaxis=dict(fixedrange=True),
        )
        show_chart(fig)
        st.caption(tr("ticker.pe_caption", source=source))


with st.container(border=True):
    _valuation_section(ticker)


# ------------------------------------------------------------------ moat
@st.cache_data(ttl=3600, show_spinner=False)
def _moat(t: str) -> MoatScore:
    return moat_score(_raw(t))


# One tile for the composite plus one per pillar — the count is known from
# PILLAR_WEIGHTS before the score is, so the placeholder is the exact row.
_moat_card = skeletons.reserve(
    "metrics", border=True, title=True, n=len(PILLAR_WEIGHTS) + 1
)
try:
    moat = _moat(ticker)
except (YFRateLimitError, URLError) as exc:
    notices.data_toast(exc)
    moat = None
except Exception:
    moat = None
with _moat_card.container(border=True):
    st.subheader(tr("ticker.moat"))
    if moat is None or moat.score is None:
        st.caption(tr("ticker.moat_insufficient"))
    else:
        moat_tiles = [(
            tr("ticker.moat_score"),
            format_value("moat", moat.score),
            verdict("moat", moat.score),
            kpi_desc("moat"),
        )]
        for pillar in moat.pillars:
            moat_tiles.append((
                pillar.label,
                tr("ticker.na") if pillar.score is None else f"{pillar.score:.0f}",
                (tr("ticker.weight", pct=f"{PILLAR_WEIGHTS[pillar.key]:.0%}"), "gray")
                if pillar.score is not None else None,
                pillar.detail,
            ))
        st.html(kpi_grid_html(moat_tiles))
        st.caption(tr("ticker.moat_caption", years=moat.years))

# ---------------------------------------------------------- insider activity
@st.cache_data(ttl=3600, show_spinner=False)
def _insiders(t: str):
    return insider_transactions(t)


@st.cache_data(ttl=86400, show_spinner=False)
def _sec_filer(t: str) -> bool | None:
    """Does this symbol appear in the SEC ticker map? None if the lookup failed.

    An empty Form 4 list means two different things and they read very
    differently: a US filer whose insiders simply haven't traded, versus a
    foreign issuer that never files Form 4 at all. The CIK map is the same
    thing `insider_transactions` keys off, so asking it here costs nothing
    beyond the cached lookup.
    """
    try:
        return cik_for(t) is not None
    except (URLError, TimeoutError, OSError):
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def _insiders_de(t: str, issuer: str | None):
    return bafin_transactions(t, issuer=issuer)


# Form 4 filings come from EDGAR, not the yfinance cache, so this is its own
# round trip — four summary tiles reserved while it runs.
_ins_card = skeletons.reserve("metrics", border=True, title=True, n=4)
txs = _insiders(ticker)
# A symbol EDGAR has never heard of is not a symbol without insider trades: the
# EU discloses the same thing under MAR Art. 19, and BaFin republishes the
# German half. Only reached when EDGAR came back empty AND the SEC ticker map
# has no CIK, so a US filer in a quiet quarter never pays for this request.
if not txs and _sec_filer(ticker) is False:
    issuer = _raw(ticker).info.get("longName") or company_name(ticker)
    txs = _insiders_de(ticker, issuer)
# Every row of one list shares a currency: EDGAR is USD, BaFin is EUR.
_ccy = txs[0].currency if txs else "USD"
_sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(_ccy, "")
_from_bafin = bool(txs) and txs[0].source == "BaFin"

with _ins_card.container(border=True):
    st.subheader(tr("ticker.insider_activity"))
    if not txs:
        # Nothing filed anywhere we can reach: the Form 4 methodology blurb
        # explains a table that isn't there, so the empty state stands alone
        # (same shape as the P/E-history section). Which empty state depends on
        # whether the SEC has ever heard of the symbol.
        filer = _sec_filer(ticker)
        st.caption(
            tr("ticker.no_form4_non_us") if filer is False
            else tr("ticker.no_form4")
        )
    else:
        st.caption(
            tr("ticker.insider_caption_bafin") if _from_bafin
            else tr("ticker.insider_caption")
        )
        summ = summarize(txs, ref=date.today())
        st.html(kpi_grid_html([
            (
                tr("ticker.buys_open_market"),
                str(summ.buy_count),
                (f"+{_fmt_money(summ.buy_value, _sym)}", "green")
                if summ.buy_value else None,
                None,
            ),
            (
                tr("ticker.sells_open_market"),
                str(summ.sell_count),
                (f"-{_fmt_money(summ.sell_value, _sym)}", "red")
                if summ.sell_value else None,
                None,
            ),
            (
                tr("ticker.net_window", days=summ.window_days),
                _fmt_money(summ.net_value, _sym),
                None,
                None,
            ),
            (
                tr("ticker.distinct_buyers_sellers"),
                f"{summ.buyers} / {summ.sellers}",
                None,
                None,
            ),
        ]))

        if summ.cluster_buy:
            st.success(tr("ticker.cluster_buying"), icon=":material/trending_up:")
        elif summ.sell_value > summ.buy_value * 3 and summ.sell_count:
            st.warning(tr("ticker.selling_dominates"), icon=":material/trending_down:")

        # Monthly open-market buy vs sell value — the buy/sell balance over time.
        om = [t for t in txs if t.is_open_market and t.date and t.value]
        if om:
            flow = pd.DataFrame(
                {
                    "month": [
                        pd.Timestamp(t.date).to_period("M").to_timestamp() for t in om
                    ],
                    "side": ["Buy" if t.code == BUY_CODE else "Sell" for t in om],
                    "value": [t.value for t in om],
                }
            )
            pivot = (
                flow.groupby(["month", "side"])["value"].sum().unstack(fill_value=0)
            )
            bar = go.Figure()
            # Keep "Buy"/"Sell" as the pivot data keys; only the display name is
            # localized (translated labels would break the pivot column lookup).
            side_labels = {"Buy": tr("ticker.buy"), "Sell": tr("ticker.sell")}
            for side, color in (("Buy", UP_COLOR), ("Sell", DOWN_COLOR)):
                if side in pivot.columns:
                    bar.add_trace(
                        go.Bar(
                            x=pivot.index, y=pivot[side], name=side_labels[side],
                            marker_color=color,
                            hovertemplate=(
                                f"{side_labels[side]}  <b>{_sym}%{{y:.3s}}</b>"
                                "<extra></extra>"
                            ),
                        )
                    )
            bar.update_layout(
                **chart_layout(
                    title=tr("ticker.chart_insider_title", ccy=_sym),
                    top_legend=True,
                    height=220,
                ),
                barmode="group",
                hovermode="x unified",
                xaxis=dict(hoverformat="%b %Y"),
                yaxis=dict(fixedrange=True, tickformat="~s"),
            )
            show_chart(bar)

        # Seven columns pan off a phone: there each trade becomes a card
        # headed by the insider's name, one line per field.
        data_table(
            transactions_frame(txs).head(30),
            title="Insider",
            fmt={
                "Shares": "{:+,.0f}",
                "Price": f"{_sym}{{:,.2f}}",
                "Value": f"{_sym}{{:+,.0f}}",
            },
            signed=("Shares", "Value"),
            labels={
                "Date": tr("ticker.col_date"),
                "Shares": tr("ticker.col_shares"),
                "Price": tr("ticker.col_price"),
                "Value": tr("ticker.col_value"),
            },
            hide_index=True,
            height=280,
            column_config={
                "Date": st.column_config.DateColumn(
                    tr("ticker.col_date"), format="YYYY-MM-DD"
                ),
                "Shares": st.column_config.NumberColumn(
                    tr("ticker.col_shares"), format="%d"
                ),
                "Price": st.column_config.NumberColumn(
                    tr("ticker.col_price"), format=f"{_sym}%.2f"
                ),
                "Value": st.column_config.NumberColumn(
                    tr("ticker.col_value"), format=f"{_sym}%.0f"
                ),
            },
        )
        st.caption(tr("ticker.signed_caption"))

# Comparables: framework wants 2-3 direct competitors side by side.
@st.cache_data(ttl=86400, show_spinner=False)
def _related(t: str) -> list[str]:
    from stocks.data.related import related_tickers

    return related_tickers(t)


def _peer_label(t: str) -> str:
    name = company_name(t)
    return f"{t} — {name}" if name and name.upper() != t.upper() else t


with st.container(border=True):
    st.subheader(tr("ticker.comparables"))
    # Related tickers stay pills (Markdown labels render logo + symbol + name)
    # for fast one-tap comparison. The full watchlist is too long to dump as a
    # pill wall, so it lives in a searchable, collapsed multiselect instead —
    # options are stable per viewed ticker, so a selected value never drops out
    # mid-rerun. Crypto pairs never belong in a stock comps table.
    peer_pool = [t for t in tickers if t != ticker and not is_crypto(t)]

    peers = list(
        st.multiselect(
            tr("ticker.peers_watchlist"),
            peer_pool,
            format_func=_peer_label,
            key=f"peers_{ticker}",
            placeholder=tr("ticker.peers_search"),
        )
    )
    suggested = [s for s in _related(ticker) if s not in peer_pool]
    picked = (
        st.pills(
            tr("ticker.related_tickers"),
            suggested,
            selection_mode="multi",
            format_func=ticker_pill_md,
            key=f"related_{ticker}",
            help=tr("ticker.related_help"),
        )
        if suggested
        else []
    )
    extra = st.text_input(tr("ticker.extra_peers"), "")
    peers += [p for p in picked if p not in peers]
    peers += [
        p.strip().upper()
        for p in extra.split(",")
        if p.strip() and p.strip().upper() not in peers
    ]
    if peers:
        # One metrics pull per peer, serial — the table shimmers with a column
        # per picked name so adding a peer doesn't blank the comparison.
        # The placeholder takes the shape the comps will land in — a wide
        # grid on desktop, one card per peer on a phone — so the swap is a
        # fill, not a relayout.
        _comp_slot = (
            skeletons.reserve("cards", n=len(peers) + 1, lines=8)
            if _MOBILE
            else skeletons.reserve("table", rows=8, cols=len(peers) + 1)
        )
        rows = [mets] + [_metrics(p) for p in peers]
        # Tickers run across the columns here, so the logo+symbol cell goes in
        # the header (no company name — comps stay compact); KPI labels keep the
        # index column. Medals mark the best composite cross-sectional ranks.
        medals = comp_medals(rows)
        comp = comparables_table(rows)
        comp.columns = [
            (f"{medals[t]}&nbsp;" if t in medals else "") + ticker_cell(t, name=False)
            for t in comp.columns
        ]
        # Peers run across the columns, which pans off a phone — there the
        # grid transposes into one card per peer, one KPI per line (the
        # medal + logo header cell becomes the card's title).
        _comp_slot.container().html(
            stacked_table_html(comp.T, index_title=True, title_html=True)
            if _MOBILE
            else ticker_table_html(comp, ticker_col=None, show_index=True)
        )
        if medals:
            st.caption(tr("ticker.medals_caption"))
    else:
        st.caption(tr("ticker.pick_peers"))

with st.expander(tr("ticker.kpi_sources_title")):
    _sources = sources_table()
    data_table(_sources, title=_sources.columns[0], hide_index=True)
    st.caption(tr("ticker.kpi_sources_caption", n=len(KPI_SOURCES)))
