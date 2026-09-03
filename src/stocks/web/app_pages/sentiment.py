"""Market pulse — where the market is heading, read against your own book.

Nine blocks, cheapest first so the page paints top-down while the slow fetches
run: the composite risk-appetite score, a row of derived trend readings, then
indices, volatility gauges, rates, inflation, rotation, cross-asset, and — for
a signed-in account with a ledger — what all of it does to their actual
positions.

**Direction over level.** Almost every row here is the same shape: the level,
the change over four horizons, a sparkline, and where the series sits in its
own trend. That is deliberate. A 10-year yield at 4.79% is a fact; "+33bp over
three months while the curve went nowhere" is the reading, and a smooth drift
and a spike-and-round-trip land on the identical three-month delta, which is
what the sparkline is for. `stocks.analysis.sentiment` owns that arithmetic
and `stocks.web.trend_ui` owns the row.

**Personalisation is the point, not a decoration.** A VIX percentile is the
same number for everyone; "your book is 71% dollar-priced and the dollar fell
1.2% this month" is not. So the indices are reordered by the geography the
reader holds, the rotation table is joined to their own sector weights, and
the last block measures their basket's own sensitivities and whether those
sensitivities are drifting.

Every block degrades on its own. Yahoo throttles datacenter IPs (the hosted
deploy hits this routinely) and FRED tarpits some User-Agents, so a failed
fetch toasts and blanks its own card instead of taking the page down.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from urllib.error import URLError

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.analysis import sentiment as sm
from stocks.analysis.portfolio import (
    allocation,
    beta,
    holdings_from_positions,
    load_closes,
    load_meta,
    market_value_weights_base,
    portfolio_returns,
    returns_frame,
)
from stocks.data import macro
from stocks.data.funds import sector_weights
from stocks.web import auth, css, notices, skeletons, trend_ui
from stocks.web.ds import (
    BRAND_ACCENT,
    CANDLE_DOWN,
    CANDLE_UP,
    FS_2XS,
    FS_SM,
    FS_XS,
    RADIUS_PILL,
    SURFACE_CARD,
    SURFACE_SUNKEN,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TRANSPARENT,
    WARN_COLOR,
    chart_layout,
    show_chart,
)
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import db_mtime, ledger_state
from stocks.web.tables import data_table, kpi_grid_html
from stocks.web.trend_ui import TrendRow

REPORT_CCY = "EUR"

# Two years of prices: the trailing-year percentile needs a full year of
# history before it can score anything, and the 200-session trend average
# needs most of another.
HISTORY = "2y"

# Windows quoted across the page, in trading sessions. Named because these
# numbers appear in a dozen places and must mean the same thing in all of them.
DAY, WEEK, MONTH, QUARTER, YEAR = 1, 5, 21, 63, 252
# How much of a series the sparklines draw. A quarter is long enough to show a
# shape and short enough that the current move is not a flat line at the right
# edge of two years.
SPARK_DAYS = 90
# Lookback for "is this rolling statistic drifting?" — one quarter, matching
# the longest horizon in the change columns.
DRIFT_DAYS = QUARTER
# Rolling window for the correlation and beta series. 60 sessions is short
# enough to move inside a regime and long enough not to be noise.
ROLL = 60

st.title(tr("sentiment.title"))

# The change columns, in one place: every block renders the same four, so the
# columns line up down the whole page and the reader learns them once.
HORIZONS = {"week": WEEK, "month": MONTH, "quarter": QUARTER, "year": YEAR}
CHIP_LABELS = [tr(f"sentiment.h_{name}") for name in HORIZONS]
STATE_NAMES = {
    key: tr(f"sentiment.state_{key}")
    for key in ("up", "turning_up", "turning_down", "down", "unknown")
}


# --------------------------------------------------------------- cached loads
@st.cache_data(ttl=900, show_spinner=False)
def _closes(period: str) -> dict[str, pd.Series]:
    """Close series for every symbol on the page, in ONE bulk request.

    15-minute ttl: this is a page about right now, and one download of ~50
    symbols is the whole price side of it.
    """
    return load_closes(sm.all_tickers(), period=period)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _rates() -> dict[str, pd.Series]:
    """The FRED block: yields, curve slopes, spreads, breakevens, policy rates."""
    return macro.fred_many(list(RATE_ROWS) + ["NFCI"])


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _inflation() -> pd.DataFrame:
    return macro.inflation()


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _benchmark_sectors() -> dict[str, float]:
    """SPY's sector split — the benchmark the reader's tilt is measured against.

    Yahoo's own fund look-through, so the buckets are already spelled the way a
    stock's `info["sector"]` spells them and join straight onto the book's
    allocation with no mapping layer.
    """
    return sector_weights("SPY")


@st.cache_data(ttl=3600, show_spinner=False)
def _book(tickers: tuple[str, ...], db: str, mtime: float):
    """Weights, return frame and the three allocation splits for one ledger.

    `db`/`mtime` are arguments rather than closure reads on purpose: cache
    entries are shared across sessions, and keying on the session's positions
    through a closure would serve one account's book to another whose ticker
    tuple happened to match.
    """
    positions = [
        p for p in ledger_state(db, mtime, REPORT_CCY)[1] if p.ticker in tickers
    ]
    if not positions:
        return None
    holdings = holdings_from_positions(positions)
    held = [h.ticker for h in holdings]
    closes = load_closes(held, period="2y")
    prices = {t: float(s.iloc[-1]) for t, s in closes.items() if not s.empty}
    meta = load_meta(held)
    weights = market_value_weights_base(positions, prices, meta, REPORT_CCY)
    return {
        "weights": weights,
        "returns": returns_frame(closes),
        "sector": allocation(weights, meta, "sector"),
        "country": allocation(weights, meta, "country"),
        "currency": allocation(weights, meta, "currency"),
    }


# The rates block, as (FRED id, i18n suffix, whether a rise is the unwelcome
# direction). `up_is_bad` is not decoration: rising yields and widening spreads
# are the unwelcome direction, but a *steepening* curve is the healthy one —
# inversion is the warning there — so colouring every rise red would paint the
# curve rows backwards. Policy rates are neutral: they are a fact about the
# central bank, not a market move.
RATE_ROWS: dict[str, tuple[str, int]] = {
    "DGS10": ("us10y", -1),
    "DFII10": ("us10y_real", -1),
    "T10Y2Y": ("curve_2s10s", +1),
    "T10Y3M": ("curve_3m10y", +1),
    "BAMLH0A0HYM2": ("hy_spread", -1),
    "BAMLC0A0CM": ("ig_spread", -1),
    "T5YIE": ("breakeven5y", -1),
    "DFEDTARU": ("policy_fed", 0),
    "ECBDFR": ("policy_ecb", 0),
}


# ----------------------------------------------------------------------- CSS
# One sheet for the page's own blocks plus the shared trend rows. No "<"
# anywhere in here, comments included: DOMPurify silently drops a whole style
# block that contains one (see stocks.web.css).
css.inject(
    f"""
    .ag-pulse {{ display: flex; flex-direction: column; gap: 0.7rem; }}
    .ag-pulse-head {{ display: flex; align-items: baseline; gap: 0.6rem;
                      flex-wrap: wrap; }}
    .ag-pulse-score {{ font-family: Epilogue, sans-serif; font-weight: 800;
                       font-size: 40px; line-height: 1; color: {TEXT_PRIMARY}; }}
    .ag-pulse-regime {{ font-size: {FS_SM}; font-weight: 600;
                        padding: 0.15rem 0.55rem; border-radius: {RADIUS_PILL}; }}
    .ag-pulse-delta {{ font-size: {FS_XS}; font-variant-numeric: tabular-nums; }}
    .ag-pulse-asof {{ font-size: {FS_XS}; color: {TEXT_MUTED};
                      margin-left: auto; text-align: right; }}
    .ag-meter {{ position: relative; height: 10px; border-radius: {RADIUS_PILL};
                 background: linear-gradient(90deg,
                   {CANDLE_DOWN} 0%, {WARN_COLOR} 40%,
                   {TEXT_MUTED} 50%, {CANDLE_UP} 100%); }}
    .ag-meter-pin {{ position: absolute; top: -4px; width: 3px; height: 18px;
                     border-radius: 2px; background: {TEXT_PRIMARY};
                     box-shadow: 0 0 0 2px {SURFACE_CARD}; }}
    .ag-meter-ghost {{ position: absolute; top: -1px; width: 2px; height: 12px;
                       border-radius: 2px; background: {TEXT_MUTED}; }}
    .ag-meter-scale {{ display: flex; justify-content: space-between;
                       font-size: {FS_2XS}; color: {TEXT_MUTED};
                       text-transform: uppercase; letter-spacing: 0.04em; }}
    .ag-comp {{ display: grid; grid-template-columns: 8.5rem 1fr 4.5rem;
                align-items: center; gap: 0.5rem; padding: 0.28rem 0;
                border-top: 1px solid {SURFACE_SUNKEN}; }}
    .ag-comp:first-child {{ border-top: 0; }}
    .ag-comp-l {{ font-size: {FS_SM}; color: {TEXT_SECONDARY}; }}
    .ag-comp-track {{ position: relative; height: 6px;
                      border-radius: {RADIUS_PILL};
                      background: {SURFACE_SUNKEN}; }}
    .ag-comp-fill {{ position: absolute; top: 0; left: 0; height: 100%;
                     border-radius: {RADIUS_PILL}; }}
    .ag-comp-mark {{ position: absolute; top: -2px; width: 2px; height: 10px;
                     border-radius: 1px; background: {TEXT_MUTED}; }}
    .ag-comp-v {{ font-size: {FS_SM}; color: {TEXT_PRIMARY}; text-align: right;
                  font-variant-numeric: tabular-nums; }}
    .ag-sub {{ font-size: {FS_SM}; font-weight: 600; color: {TEXT_SECONDARY};
               margin: 0.6rem 0 0.2rem 0; }}
    @media (max-width: 640px) {{
      .ag-comp {{ grid-template-columns: 7rem 1fr 4rem; }}
      .ag-pulse-score {{ font-size: 34px; }}
      .ag-pulse-asof {{ margin-left: 0; width: 100%; text-align: left; }}
    }}
    """
    + trend_ui.css(chip_labels=CHIP_LABELS)
)

_REGIME_TINT = {
    "stress": CANDLE_DOWN,
    "caution": WARN_COLOR,
    "neutral": TEXT_MUTED,
    "appetite": CANDLE_UP,
    "euphoria": CANDLE_UP,
    "unknown": TEXT_MUTED,
}


def _pct(v: float, digits: int = 1) -> str:
    return "n/a" if v != v else f"{v:+.{digits}%}"


def _num(v: float, digits: int = 2) -> str:
    return "n/a" if v != v else f"{v:.{digits}f}"


def _label(key: str, fallback: str) -> str:
    """Translation for `key`, or `fallback` when the catalogs have no entry.

    `i18n.t` returns the key itself for a miss, which is the right default for
    a hardcoded key (a missing string shows up loudly in review) and the wrong
    one for keys built from upstream data: Yahoo can invent a sector spelling
    tomorrow, and this page must print that spelling rather than
    "sentiment.sector_whatever".
    """
    text = tr(key)
    return fallback if text == key else text


def _score_color(score: float) -> str:
    """Colour for a 0-100 risk-appetite score: red at the fear end, green at
    the appetite end, neutral grey through the middle band."""
    if score != score:
        return TEXT_MUTED
    if score < 40:
        return CANDLE_DOWN
    if score < 60:
        return TEXT_MUTED
    return CANDLE_UP


def _pct_chips(series: pd.Series, *, welcome: int = 1) -> list[tuple[str, int]]:
    """Percent-change chips for a price series, one per horizon.

    `welcome` is +1 when a rise is the good direction and -1 when a fall is, so
    a falling volatility index and a rising equity index both read green.
    """
    moves = sm.pct_changes(series, HORIZONS)
    out = []
    for name in HORIZONS:
        value = moves.get(name)
        if value is None:
            continue
        direction = 0 if value == 0 else welcome * (1 if value > 0 else -1)
        out.append((f"{value:+.1%}", direction))
    return out


def _bp_chips(series: pd.Series, *, welcome: int) -> list[tuple[str, int]]:
    """Basis-point chips for a rate or spread, one per horizon.

    A yield's move is quoted in basis points, not percent: "10y +33bp" is how
    it is discussed everywhere, and a percent change of a percentage rate
    ("+7.4%") is a number that reads like a price move and is not one.
    """
    moves = sm.changes(series, HORIZONS)
    out = []
    for name in HORIZONS:
        value = moves.get(name)
        if value is None:
            continue
        points = value * 100
        direction = 0 if points == 0 else welcome * (1 if points > 0 else -1)
        out.append((f"{points:+.0f}bp", direction))
    return out


def _tail(series: pd.Series, days: int = SPARK_DAYS) -> list[float]:
    return [float(v) for v in series.dropna().iloc[-days:]]


# ----------------------------------------------------------------- the slots
# Every card is reserved here, in page order, before a single byte is fetched.
# Streamlit sends the elements a script emits in the order it emits them, so
# these nine shimmering cards reach the browser in the first delta batch and
# each one is then replaced in place as the fetch it waits on lands. The reader
# sees the page's whole shape immediately and watches it fill, instead of
# watching one spinner and then having nine cards appear at once.
#
# The shapes are sized to the content that replaces them (thirteen index rows,
# seven gauges, nine rates) so the fill is a swap and not a relayout — a
# skeleton that is the wrong height moves everything below it when it resolves,
# which is worse than no skeleton at all. `skeletons._table` forks to stacked
# rows on phones by itself, matching how the trend rows collapse there.
SLOTS = {
    "pulse": skeletons.reserve("metrics", n=3, title=True, border=True),
    "snapshot": skeletons.reserve("cards", n=4, lines=2, title=True, border=True),
    "indices": skeletons.reserve(
        "table", rows=len(sm.INDICES), cols=8, title=True, border=True
    ),
    "gauges": skeletons.reserve(
        "table", rows=len(sm.GAUGES), cols=8, title=True, border=True
    ),
    "rates": skeletons.reserve(
        "table", rows=len(RATE_ROWS), cols=8, title=True, border=True
    ),
    "inflation": skeletons.reserve(
        "table", rows=len(macro.INFLATION_AREAS) + 1, cols=8, title=True, border=True
    ),
    "rotation": skeletons.reserve(
        "table", rows=len(sm.SECTOR_ETFS), cols=5, title=True, border=True
    ),
    "cross": skeletons.reserve(
        "table", rows=len(sm.MACRO_ASSETS), cols=8, title=True, border=True
    ),
    "book": skeletons.reserve("metrics", n=5, title=True, border=True),
}


def _blank(box, title_key: str, message_key: str) -> None:
    """Resolve a reserved slot with an explanation instead of content.

    Every path out of a reserved slot has to end in `container()` or `clear()`
    or the shimmer outlives the load, and a failed fetch is a path. The card
    keeps its heading so the page's structure survives one dead source.
    """
    with box.container(border=True):
        st.subheader(tr(title_key))
        st.caption(tr(message_key))


def _rows_block(
    box,
    rows: list[TrendRow],
    *,
    title_key: str,
    label_key: str,
    value_key: str,
    chip_labels: list[str],
    captions: Sequence[str] = (),
) -> None:
    """Fill one slot with a heading, a trend table and its captions."""
    with box.container(border=True):
        st.subheader(tr(title_key))
        st.html(
            trend_ui.rows_html(
                rows,
                chip_labels=chip_labels,
                label_label=tr(label_key),
                value_label=tr(value_key),
                spark_label=tr("sentiment.col_shape"),
                state_label=tr("sentiment.col_trend"),
                state_names=STATE_NAMES,
            )
        )
        for caption in captions:
            st.caption(caption)


# ------------------------------------------------------------------- renderers
def render_pulse(box, closes: dict[str, pd.Series], rates: dict[str, pd.Series]) -> None:
    """The composite: score, how it moved, how long it has held its band."""
    pulse = sm.pulse(closes, hy_spread=rates.get("BAMLH0A0HYM2"))
    with box.container(border=True):
        st.subheader(tr("sentiment.pulse_title"))
        tint = _REGIME_TINT[pulse.regime]
        score_text = "n/a" if pulse.score != pulse.score else f"{pulse.score:.0f}"
        # The pin sits at the score's own position on the 0-100 track; a NaN
        # score parks it mid-scale under an "n/a" headline rather than at zero,
        # which would read as "maximum fear".
        pin = 50.0 if pulse.score != pulse.score else max(0.0, min(100.0, pulse.score))

        history = pulse.history

        # Where the score stood a week and a month back, and how long it has
        # held its band. A 66 that crossed into "appetite" yesterday and a 66
        # that has sat there for two months are the same number and not the
        # same market, and only these facts say which one this is.
        def _delta(ago: int) -> float:
            if len(history) <= ago:
                return float("nan")
            return float(history.iloc[-1]) - float(history.iloc[-ago - 1])

        deltas = [(name, _delta(HORIZONS[name])) for name in ("week", "month")]
        delta_html = "".join(
            f'<span class="ag-pulse-delta" style="color:'
            f'{CANDLE_UP if value > 0 else CANDLE_DOWN if value < 0 else TEXT_MUTED}">'
            f"{html.escape(tr(f'sentiment.h_{name}'))} {value:+.1f}</span>"
            for name, value in deltas
            if value == value
        )
        run = sm.regime_run(history)
        asof = "" if pulse.as_of is None else pulse.as_of.strftime("%Y-%m-%d")
        stamp = tr("sentiment.regime_run", n=run) if run else ""
        # A ghost pin at the month-ago score: the delta as a distance on the
        # same track, which is easier to read than a signed number beside it.
        ghost = ""
        month_ago = dict(deltas).get("month", float("nan"))
        if month_ago == month_ago:
            ghost_pos = max(0.0, min(100.0, pin - month_ago))
            ghost = (
                f'<div class="ag-meter-ghost" style="left:calc({ghost_pos:.1f}% '
                '- 1px)"></div>'
            )
        st.html(
            '<div class="ag-pulse">'
            '<div class="ag-pulse-head">'
            f'<span class="ag-pulse-score">{score_text}</span>'
            f'<span class="ag-pulse-regime" style="background:{tint}22;'
            f'color:{tint}">{html.escape(tr(f"sentiment.regime_{pulse.regime}"))}'
            "</span>"
            f"{delta_html}"
            f'<span class="ag-pulse-asof">{html.escape(asof)}'
            f'{"<br>" + html.escape(stamp) if stamp else ""}</span>'
            "</div>"
            '<div class="ag-meter">'
            f"{ghost}"
            f'<div class="ag-meter-pin" style="left:calc('
            f'{pin:.1f}% - 1.5px)"></div></div>'
            '<div class="ag-meter-scale">'
            f'<span>{html.escape(tr("sentiment.regime_stress"))}</span>'
            f'<span>{html.escape(tr("sentiment.regime_neutral"))}</span>'
            f'<span>{html.escape(tr("sentiment.regime_euphoria"))}</span>'
            "</div></div>"
        )
        st.caption(tr("sentiment.pulse_help"))

        if len(history) > 5:
            spark = history.iloc[-SPARK_DAYS:]
            fig = go.Figure(
                go.Scatter(
                    x=spark.index, y=spark.to_numpy(), mode="lines",
                    line=dict(color=BRAND_ACCENT, width=2),
                    hovertemplate=tr("sentiment.hover_pulse"),
                    name="",
                )
            )
            fig.update_layout(
                **chart_layout(height=110),
                paper_bgcolor=TRANSPARENT, plot_bgcolor=TRANSPARENT,
                showlegend=False,
                xaxis=dict(visible=False),
                yaxis=dict(
                    range=[0, 100], showgrid=False, zeroline=False,
                    tickvals=[0, 50, 100],
                    tickfont=dict(size=9, color=TEXT_MUTED),
                ),
            )
            show_chart(fig, key="pulse_spark")

        # Each component's score with a tick at where it stood a month ago:
        # the same "level plus direction" contract as every row below, in the
        # width a progress bar has.
        comp_rows = []
        for comp in pulse.components:
            colour = _score_color(comp.score)
            mark = ""
            if comp.then == comp.then:
                mark = (
                    '<span class="ag-comp-mark" style="left:calc('
                    f'{max(0.0, min(100.0, comp.then)):.0f}% - 1px)"></span>'
                )
            comp_rows.append(
                '<div class="ag-comp">'
                '<span class="ag-comp-l">'
                f'{html.escape(tr(f"sentiment.comp_{comp.key}"))}</span>'
                '<span class="ag-comp-track">'
                f'<span class="ag-comp-fill" style="width:{comp.score:.0f}%;'
                f'background:{colour}"></span>{mark}</span>'
                f'<span class="ag-comp-v">{html.escape(comp.text)}</span>'
                "</div>"
            )
        st.html("".join(comp_rows))
        st.caption(tr("sentiment.comp_mark_help"))
        if pulse.missing:
            st.caption(
                tr(
                    "sentiment.pulse_missing",
                    names=", ".join(
                        tr(f"sentiment.comp_{k}") for k in pulse.missing
                    ),
                )
            )


def render_snapshot(
    box, closes: dict[str, pd.Series], rates: dict[str, pd.Series]
) -> None:
    """Four readings that only exist as trends: none has a level worth printing."""
    cards: list[tuple[str, str, str]] = []

    # Trend breadth, twice over. Not today's advance-decline — how many markets
    # are in an uptrend at all. An index at a high with a third of its sectors
    # below trend is a narrowing market, and the index level cannot say so.
    hits, total = sm.above_ma_share(closes, [i.ticker for i in sm.INDICES], YEAR - 52)
    if total:
        cards.append((
            tr("sentiment.breadth_indices"),
            f"{hits}/{total}",
            tr("sentiment.breadth_indices_note", n=YEAR - 52),
        ))
    s_hits, s_total = sm.above_ma_share(
        closes, list(sm.SECTOR_ETFS.values()), sm.TREND_SLOW
    )
    if s_total:
        cards.append((
            tr("sentiment.breadth_sectors"),
            f"{s_hits}/{s_total}",
            tr("sentiment.breadth_sectors_note", n=sm.TREND_SLOW),
        ))

    # Stock/bond correlation: the level IS the story. Negative means bonds
    # cushion an equity drawdown; positive means both legs fall together and
    # the diversification a reader thinks they have is not there.
    if "SPY" in closes and "TLT" in closes:
        corr = sm.rolling_correlation(closes["SPY"], closes["TLT"], window=ROLL)
        now, then = sm.drift(corr, ago=DRIFT_DAYS)
        if now == now:
            cards.append((
                tr("sentiment.stock_bond_corr"),
                f"{now:+.2f}",
                tr("sentiment.drift_note", value=_num(then))
                if then == then
                else tr("sentiment.stock_bond_corr_note"),
            ))

    # The rates quadrant: a slope move crossed with a yield move. Four regimes,
    # four different macro stories, and the yield level tells none of them.
    if "DGS10" in rates and "T10Y2Y" in rates:
        quad = sm.rate_quadrant(rates["DGS10"], rates["T10Y2Y"], days=QUARTER)
        if quad != "unknown":
            y_move = sm.changes(rates["DGS10"], {"q": QUARTER}).get("q", 0.0) * 100
            s_move = sm.changes(rates["T10Y2Y"], {"q": QUARTER}).get("q", 0.0) * 100
            cards.append((
                tr("sentiment.rates_regime"),
                tr(f"sentiment.quad_{quad}"),
                tr(
                    "sentiment.quad_note",
                    yields=f"{y_move:+.0f}bp",
                    slope=f"{s_move:+.0f}bp",
                ),
            ))

    if not cards:
        _blank(box, "sentiment.snapshot_title", "sentiment.prices_unavailable")
        return
    with box.container(border=True):
        st.subheader(tr("sentiment.snapshot_title"))
        st.html(trend_ui.quad_html(cards))
        st.caption(tr("sentiment.snapshot_help"))


def render_indices(
    box, closes: dict[str, pd.Series], country_weights: pd.Series | None
) -> None:
    """Headline indices, ordered by the geography the reader actually holds."""
    order = sm.INDICES
    pinned = country_weights is not None and not country_weights.empty
    if pinned:
        order = tuple(sm.pin_order(sm.INDICES, country_weights))
    rows = []
    for idx in order:
        series = closes.get(idx.ticker)
        if series is None or series.dropna().empty:
            continue
        clean = series.dropna()
        rows.append(
            TrendRow(
                label=idx.name,
                value=f"{float(clean.iloc[-1]):,.0f}",
                chips=_pct_chips(clean),
                spark=_tail(clean),
                state=sm.trend_state(clean),
            )
        )
    if not rows:
        _blank(box, "sentiment.indices_title", "sentiment.prices_unavailable")
        return
    captions = [tr("sentiment.trend_help", n=sm.TREND_SLOW)]
    if pinned:
        captions.insert(0, tr("sentiment.indices_pinned"))
    _rows_block(
        box, rows,
        title_key="sentiment.indices_title",
        label_key="sentiment.col_index",
        value_key="sentiment.col_last",
        chip_labels=CHIP_LABELS,
        captions=captions,
    )


def render_gauges(box, closes: dict[str, pd.Series]) -> None:
    """Volatility gauges: level, direction, and where in their own year."""
    # A tile is "stale" relative to the freshest bar in the row, not to the
    # calendar: on a Monday morning every gauge is Friday's and none of them is
    # stale. Taken across the whole row so it does not hinge on any one symbol
    # being present.
    last_bars = [
        closes[g.ticker].dropna().index[-1]
        for g in sm.GAUGES
        if g.ticker in closes and not closes[g.ticker].dropna().empty
    ]
    newest = max(last_bars) if last_bars else None
    rows = []
    for gauge in sm.GAUGES:
        series = closes.get(gauge.ticker)
        if series is None or series.dropna().empty:
            continue
        clean = series.dropna()
        welcome = -1 if gauge.high_is_fear else 1
        # The last column is the percentile pair rather than a 12-month change:
        # for a volatility index "where in its own year" and "where it was a
        # month ago" is the reading, and a 12-month percent change on a
        # mean-reverting series is close to meaningless.
        chips = _pct_chips(clean, welcome=welcome)[:3]
        now = sm.percentile_now(clean)
        then = sm.percentile_then(clean, ago=MONTH)
        if now == now:
            appetite = 100.0 - now if gauge.high_is_fear else now
            text = f"p{now:.0f}" if then != then else f"p{now:.0f} ({then:.0f})"
            chips.append((text, 1 if appetite >= 60 else -1 if appetite < 40 else 0))
        stale = newest is not None and (newest - clean.index[-1]).days > 7
        rows.append(
            TrendRow(
                label=gauge.name,
                value=gauge.fmt.format(float(clean.iloc[-1])),
                chips=chips,
                spark=_tail(clean),
                state=None if stale else sm.trend_state(clean),
                note=(
                    tr(
                        "sentiment.gauge_stale",
                        date=clean.index[-1].strftime("%Y-%m-%d"),
                    )
                    if stale
                    else None
                ),
            )
        )
    if not rows:
        _blank(box, "sentiment.risk_title", "sentiment.prices_unavailable")
        return
    _rows_block(
        box, rows,
        title_key="sentiment.risk_title",
        label_key="sentiment.col_gauge",
        value_key="sentiment.col_level",
        chip_labels=[*CHIP_LABELS[:3], tr("sentiment.col_pctl")],
        captions=[tr("sentiment.risk_help")],
    )


def render_rates(box, rates: dict[str, pd.Series]) -> None:
    """Yields, curve slopes and credit spreads, in basis points per horizon."""
    rows = []
    for sid, (suffix, welcome) in RATE_ROWS.items():
        series = rates.get(sid)
        if series is None or series.dropna().empty:
            continue
        clean = series.dropna()
        rows.append(
            TrendRow(
                label=tr(f"sentiment.{suffix}"),
                hint=tr(f"sentiment.{suffix}_help"),
                value=f"{float(clean.iloc[-1]):.2f}%",
                chips=_bp_chips(clean, welcome=welcome),
                spark=_tail(clean),
                # A policy rate steps when a committee decides and is flat in
                # between, so a moving-average trend label on it would be noise
                # dressed as a signal.
                state=None if welcome == 0 else sm.trend_state(clean),
            )
        )
    if not rows:
        _blank(box, "sentiment.rates_title", "sentiment.macro_unavailable")
        return
    captions = [tr("sentiment.rates_help")]
    nfci = rates.get("NFCI")
    if nfci is not None and not nfci.dropna().empty:
        clean = nfci.dropna()
        captions.append(
            tr(
                "sentiment.nfci_note",
                value=_num(float(clean.iloc[-1])),
                change=_num(sm.changes(clean, {"q": QUARTER}).get("q", float("nan"))),
            )
        )
    _rows_block(
        box, rows,
        title_key="sentiment.rates_title",
        label_key="sentiment.col_rate",
        value_key="sentiment.col_level",
        chip_labels=CHIP_LABELS,
        captions=captions,
    )


def render_inflation(box, infl: pd.DataFrame) -> None:
    """Annual inflation per area, with the direction the trend has turned."""
    if infl.empty:
        _blank(box, "sentiment.inflation_title", "sentiment.macro_unavailable")
        return
    rows = []
    for _, row in infl.iterrows():
        chips: list[tuple[str, int]] = []
        core = row["core"]
        chips.append((f"{core:.1f}%" if core == core else "n/a", 0))
        for column in ("prior", "six_months"):
            past = row[column]
            if past != past:
                chips.append(("n/a", 0))
                continue
            delta = float(row["headline"]) - float(past)
            # Inflation rising is the unwelcome direction, so the sign is
            # inverted relative to a price move.
            chips.append((
                f"{delta:+.1f}pp",
                0 if delta == 0 else (-1 if delta > 0 else 1),
            ))
        chips.append((str(row["period"]), 0))
        rows.append(
            TrendRow(
                label=_label(f"sentiment.area_{row['area']}", str(row["area"])),
                value=f"{float(row['headline']):.1f}%",
                chips=chips,
                spark=list(row["path"]),
            )
        )
    with box.container(border=True):
        st.subheader(tr("sentiment.inflation_title"))
        st.html(
            trend_ui.rows_html(
                rows,
                chip_labels=[
                    tr("sentiment.col_core"),
                    tr("sentiment.col_vs_prior"),
                    tr("sentiment.col_vs_six"),
                    tr("sentiment.col_period"),
                ],
                label_label=tr("sentiment.col_area"),
                value_label=tr("sentiment.col_headline"),
                spark_label=tr("sentiment.col_shape"),
                state_label="",
                state_names=STATE_NAMES,
            )
        )
        st.caption(tr("sentiment.inflation_help"))
        st.caption(tr("sentiment.inflation_momentum_help"))


def render_rotation(
    box, closes: dict[str, pd.Series], book: dict | None
) -> None:
    """Sector and factor leadership, joined to the reader's own weights."""
    excess_m = sm.relative_strength(closes, sm.SECTOR_ETFS, "SPY", MONTH)
    excess_q = sm.relative_strength(closes, sm.SECTOR_ETFS, "SPY", QUARTER)
    if excess_m.empty:
        _blank(box, "sentiment.rotation_title", "sentiment.prices_unavailable")
        return
    # A table rather than trend rows: the reading here is a comparison down a
    # column ("which sector led"), which is what a sortable numeric grid is
    # for. The trend column carries the direction the sparklines carry
    # elsewhere.
    bench = _benchmark_sectors()
    table = pd.DataFrame(
        {"month": excess_m, "quarter": excess_q.reindex(excess_m.index)}
    )
    table["trend"] = [
        STATE_NAMES[sm.trend_state(closes[sm.SECTOR_ETFS[name]])]
        if sm.SECTOR_ETFS.get(name) in closes
        else ""
        for name in table.index
    ]
    cols = ["month", "quarter", "trend"]
    labels = {
        "month": tr("sentiment.col_excess_month"),
        "quarter": tr("sentiment.col_excess_quarter"),
        "trend": tr("sentiment.col_trend"),
    }
    weights = None
    if book is not None and not book["sector"].empty:
        weights = book["sector"]
        table["yours"] = weights.reindex(table.index).fillna(0.0)
        cols.insert(2, "yours")
        labels["yours"] = tr("sentiment.col_your_weight")
        if bench:
            table["tilt"] = sm.tilt(
                weights, pd.Series(bench, dtype=float)
            ).reindex(table.index)
            cols.insert(3, "tilt")
            labels["tilt"] = tr("sentiment.col_tilt")
    table.index = [
        _label(f"sentiment.sector_{name.lower().replace(' ', '_')}", name)
        for name in table.index
    ]
    percent_cols = [c for c in cols if c != "trend"]

    pair_rows = []
    for key, first, second in sm.FACTOR_PAIRS:
        if first not in closes or second not in closes:
            continue
        ratio = (closes[first] / closes[second]).dropna()
        pair_rows.append(
            TrendRow(
                label=tr(f"sentiment.pair_{key}"),
                value=f"{float(ratio.iloc[-1]):.3f}",
                chips=_pct_chips(ratio),
                spark=_tail(ratio),
                state=sm.trend_state(ratio),
            )
        )

    with box.container(border=True):
        st.subheader(tr("sentiment.rotation_title"))
        data_table(
            table[cols],
            index_title=True,
            fmt={c: "{:+.2%}" if c != "yours" else "{:.1%}" for c in percent_cols},
            signed=tuple(c for c in percent_cols if c != "yours"),
            labels=labels,
            width="stretch",
            column_config={
                **{
                    c: st.column_config.NumberColumn(labels[c], format="percent")
                    for c in percent_cols
                },
                "trend": st.column_config.TextColumn(labels["trend"]),
            },
        )
        if weights is not None:
            caught = sm.rotation_capture(weights, excess_m)
            if caught == caught:
                st.caption(tr("sentiment.rotation_capture", value=_pct(caught, 2)))
        st.caption(tr("sentiment.rotation_help"))
        if pair_rows:
            st.html(
                '<div class="ag-sub">'
                f'{html.escape(tr("sentiment.factors_title"))}</div>'
            )
            st.html(
                trend_ui.rows_html(
                    pair_rows,
                    chip_labels=CHIP_LABELS,
                    label_label=tr("sentiment.col_pair"),
                    value_label=tr("sentiment.col_ratio"),
                    spark_label=tr("sentiment.col_shape"),
                    state_label=tr("sentiment.col_trend"),
                    state_names=STATE_NAMES,
                )
            )
            st.caption(tr("sentiment.factors_help"))


def render_cross(box, closes: dict[str, pd.Series]) -> None:
    """The dollar, metals, energy and crypto — the read behind the equity move."""
    rows = []
    for ticker, name, fmt in sm.MACRO_ASSETS:
        series = closes.get(ticker)
        if series is None or series.dropna().empty:
            continue
        clean = series.dropna()
        rows.append(
            TrendRow(
                label=name,
                value=fmt.format(float(clean.iloc[-1])),
                chips=_pct_chips(clean),
                spark=_tail(clean),
                state=sm.trend_state(clean),
            )
        )
    if not rows:
        _blank(box, "sentiment.cross_title", "sentiment.prices_unavailable")
        return
    _rows_block(
        box, rows,
        title_key="sentiment.cross_title",
        label_key="sentiment.col_asset",
        value_key="sentiment.col_last",
        chip_labels=CHIP_LABELS,
        captions=[tr("sentiment.cross_help")],
    )


def render_book(box, closes: dict[str, pd.Series], book: dict | None) -> None:
    """The payoff: the same regime, restated as what it does to these positions.

    And, because this is a page about direction, whether those sensitivities
    are themselves drifting. Signed-in with a ledger only — there is no honest
    version of this for an empty book, so the card invites the import instead
    of inventing one.
    """
    if not auth.is_logged_in():
        _blank(box, "sentiment.book_title", "sentiment.book_signed_out")
        return
    if book is None:
        _blank(box, "sentiment.book_title", "sentiment.book_empty")
        return
    if not closes:
        _blank(box, "sentiment.book_title", "sentiment.prices_unavailable")
        return

    # Fixed weights on purpose: this reads what the account owns NOW under the
    # current regime, not how it has performed (the Portfolio page owns that).
    # Both sides go through naive_index — a book of European and US names
    # carries two exchange timezones, and beta() intersects on the index, so
    # leaving the zones on would silently regress over an empty overlap.
    port = sm.naive_index(portfolio_returns(book["returns"], book["weights"]))
    bench_returns = {
        ticker: sm.naive_index(closes[ticker].dropna().pct_change().iloc[1:])
        for ticker in sm.BENCHMARKS
        if ticker in closes and not closes[ticker].dropna().empty
    }

    def _beta_tile(ticker: str, suffix: str) -> tuple:
        """A beta KPI whose chip is the drift, not the level.

        A beta of 1.05 is unremarkable; a beta that was 0.82 a quarter ago
        means the book got materially more market-sensitive without the reader
        buying anything, because the regime moved under it. The level is the
        value and the change is the chip.
        """
        series = bench_returns.get(ticker)
        label = tr(f"sentiment.beta_{suffix}")
        help_text = tr(f"sentiment.beta_{suffix}_help")
        if series is None or port.empty:
            return label, "n/a", None, help_text
        level = beta(port, series)
        rolling = sm.rolling_beta(port, series, window=ROLL)
        now, then = sm.drift(rolling, ago=DRIFT_DAYS)
        chip = None
        if now == now and then == then:
            change = now - then
            chip = (
                f"{change:+.2f}",
                "gray" if abs(change) < 0.05 else ("red" if change > 0 else "green"),
            )
        return label, "n/a" if level != level else f"{level:.2f}", chip, help_text

    # FX: what part of the last month's return came from currency rather than
    # from the assets. A EUR investor holding US names is short EUR whether
    # they meant to be or not.
    fx_moves = {}
    eurusd = closes.get("EURUSD=X")
    if eurusd is not None and not eurusd.dropna().empty:
        # EURUSD=X is dollars per euro, so the dollar's move against the euro
        # is the inverse of the pair's move — inverting here is the difference
        # between a drag and a tailwind.
        move = sm.pct_over(eurusd, MONTH)
        if move == move:
            fx_moves["USD"] = 1.0 / (1.0 + move) - 1.0
    drag, contributions = sm.fx_exposure(book["currency"], fx_moves, base=REPORT_CCY)
    usd_share = float(book["currency"].get("USD", 0.0))

    with box.container(border=True):
        st.subheader(tr("sentiment.book_title"))
        st.html(
            kpi_grid_html([
                _beta_tile("^GSPC", "equity"),
                _beta_tile("TLT", "duration"),
                _beta_tile("HYG", "credit"),
                _beta_tile("EEM", "em"),
                (
                    tr("sentiment.usd_share"),
                    f"{usd_share:.0%}",
                    (
                        (f"{drag:+.2%}", "green" if drag >= 0 else "red")
                        if drag == drag
                        else None
                    ),
                    tr("sentiment.usd_share_help"),
                ),
            ])
        )
        st.caption(tr("sentiment.beta_drift_help", n=DRIFT_DAYS))

        notes = []
        # Is the book's own diversification working? The stock/bond correlation
        # in the snapshot is the market's; this one is theirs.
        if "TLT" in bench_returns and not port.empty:
            own = sm.rolling_correlation(
                (1 + port).cumprod(), closes["TLT"], window=ROLL
            )
            now, then = sm.drift(own, ago=DRIFT_DAYS)
            if now == now:
                notes.append(
                    tr(
                        "sentiment.book_bond_corr",
                        value=_num(now),
                        prior=_num(then) if then == then else "n/a",
                    )
                )
        if drag == drag and not contributions.empty:
            notes.append(tr("sentiment.fx_note", value=_pct(drag, 2)))
        if not book["sector"].empty and "SPY" in closes:
            top = book["sector"].head(3)
            excess = sm.relative_strength(closes, sm.SECTOR_ETFS, "SPY", MONTH)
            leading = [
                _label(f"sentiment.sector_{name.lower().replace(' ', '_')}", name)
                for name in top.index
                if name in excess.index and excess[name] > 0
            ]
            lagging = [
                _label(f"sentiment.sector_{name.lower().replace(' ', '_')}", name)
                for name in top.index
                if name in excess.index and excess[name] <= 0
            ]
            if leading:
                notes.append(tr("sentiment.note_leading", names=", ".join(leading)))
            if lagging:
                notes.append(tr("sentiment.note_lagging", names=", ".join(lagging)))
        for note in notes:
            st.caption(note)
        st.caption(tr("sentiment.book_help"))


# ------------------------------------------------------------------- the load
# Four stages, each filling the slots it unblocks the moment its fetch returns.
# The order is by what the page owes the reader soonest, not by page order:
# prices arrive first and light up the two blocks that need nothing else, then
# the FRED pull completes the composite, then the ledger personalises what it
# can, then Eurostat fills the last card. A stage that fails resolves its own
# slots with a reason and the rest of the page carries on.


# The slots the price fetch unblocks, with the heading each keeps if it fails.
PRICE_BLOCKS = [
    ("gauges", "sentiment.risk_title"),
    ("cross", "sentiment.cross_title"),
    ("indices", "sentiment.indices_title"),
    ("pulse", "sentiment.pulse_title"),
    ("snapshot", "sentiment.snapshot_title"),
    ("rotation", "sentiment.rotation_title"),
    ("book", "sentiment.book_title"),
]

# --- stage 1: prices. One bulk download of ~50 symbols, and the two blocks
# that depend on nothing else are filled before anything else is asked for.
closes: dict[str, pd.Series] = {}
try:
    closes = _closes(HISTORY)
except (YFRateLimitError, URLError) as exc:
    # Throttled or unreachable: the toast says which, and every card that
    # needed prices says so in place of its content.
    notices.data_toast(exc)
except Exception:
    closes = {}

if closes:
    render_gauges(SLOTS["gauges"], closes)
    render_cross(SLOTS["cross"], closes)
else:
    # Covers the exceptions above and the third path neither of them sees: a
    # fetch that succeeded and returned nothing. Every reserved slot has to be
    # resolved or its shimmer outlives the load.
    for _name, _title in PRICE_BLOCKS:
        _blank(SLOTS[_name], _title, "sentiment.prices_unavailable")

# --- stage 2: FRED. Small CSVs behind a six-hour disk cache, so this is
# usually instant; the composite's credit leg and the whole rates card wait on
# it, and the composite falls back to an ETF proxy if it never arrives.
rates: dict[str, pd.Series] = {}
if closes:
    try:
        rates = _rates()
    except Exception:
        rates = {}
    render_pulse(SLOTS["pulse"], closes, rates)
    render_snapshot(SLOTS["snapshot"], closes, rates)
if rates:
    render_rates(SLOTS["rates"], rates)
else:
    _blank(SLOTS["rates"], "sentiment.rates_title", "sentiment.macro_unavailable")

# --- stage 3: the ledger. The slowest stage by far — a share-matching replay,
# a second price download for the held names, and one Yahoo profile fetch per
# holding for the sector/country/currency splits. Three blocks wait on it, and
# the indices only when there is an account whose geography could reorder them:
# an anonymous visitor gets that card at stage 1 speed instead.
book = None
pins_possible = auth.is_logged_in()
if closes and not pins_possible:
    render_indices(SLOTS["indices"], closes, None)

if pins_possible:
    _db = str(auth.user_paths().db)
    try:
        _mtime = db_mtime(_db)
        _held = tuple(
            sorted({p.ticker for p in ledger_state(_db, _mtime, REPORT_CCY)[1]})
        )
        if _held:
            book = _book(_held, _db, _mtime)
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
    except Exception:
        book = None
    if closes:
        render_indices(
            SLOTS["indices"], closes, book["country"] if book else None
        )

if closes:
    render_rotation(SLOTS["rotation"], closes, book)
render_book(SLOTS["book"], closes, book)

# --- stage 4: Eurostat. Nothing else depends on it, so it goes last and its
# card is the only one still shimmering while it runs.
try:
    _infl = _inflation()
except Exception:
    _infl = pd.DataFrame()
render_inflation(SLOTS["inflation"], _infl)

st.caption(tr("sentiment.sources", stamp=macro.as_of()))
