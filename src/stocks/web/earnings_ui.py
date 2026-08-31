"""Shared earnings-calendar UI: the past-result dialog and the clickable
calendar component.

Both the full Earnings page and the home dashboard's mini-calendar render past
prints as green/red beat/miss chips that open a result overview on click. That
overview and the CCv2 plumbing that flows chip clicks back to Python live here
so the two pages stay identical instead of drifting.

The overview opens on the headline (EPS vs estimate, price reaction) and then
breaks the print down section by section — revenue, margins, GAAP result, EPS
quality, next-quarter consensus. Each section leads with a visual (bars, gauges,
a dispersion range) and parks the numbers in an expander, so the dialog answers
"how did the quarter land?" at a glance without becoming a spreadsheet.
"""

from __future__ import annotations

import html
from datetime import date
from urllib.error import URLError

import pandas as pd
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.data.earnings import (
    Quarter,
    fetch_quarters,
    fetch_statement_currency,
    match_quarter,
    pct_change,
    price_reaction,
    prior_quarter,
    year_ago,
)
from stocks.data.estimates import (
    CURRENT_FY,
    CURRENT_Q,
    NEXT_FY,
    NEXT_Q,
    QuarterOutlook,
    fetch_estimates,
    quarter_outlook,
)
from stocks.web import notices, skeletons
from stocks.web.auth import CURRENCY_SYMBOL
from stocks.web.i18n import t as tr
from stocks.web.widgets import (
    BORDER,
    BRAND_ACCENT,
    CRITICAL_FILL,
    DOWN_COLOR,
    FS_SM,
    FS_XS,
    INFO_DEEP,
    LOSS_BAND,
    PROFIT_BAND,
    RADIUS_PILL,
    RADIUS_SM,
    SUCCESS_FILL,
    SURFACE_SUNKEN,
    TEXT_FAINT,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    UP_COLOR,
    data_table,
    kpi_grid_html,
)

_ESC = html.escape

# Quarters shown in the trend visuals. yfinance publishes five quarters of the
# income statement, so a taller stack would just draw empty rows.
TREND_QUARTERS = 5
SURPRISE_QUARTERS = 8


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def reaction(ticker: str, iso: str) -> float | None:
    """% price move across a past print, cached (shared by both calendars)."""
    return price_reaction(ticker, date.fromisoformat(iso))


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def quarter_detail(ticker: str) -> tuple[list[Quarter], str | None, QuarterOutlook]:
    """(quarters newest-first, statement currency, next-quarter consensus).

    One cache entry for the whole breakdown: the dialog needs all three at once
    and opens on a click, so three separate caches would only add spinners.
    """
    quarters = fetch_quarters(ticker)
    currency = fetch_statement_currency(ticker) if quarters else None
    return quarters, currency, quarter_outlook(fetch_estimates(ticker))


# Clickable grid: Python hands the finished table HTML over as data, JS wires
# every chip carrying data-ticker/data-date (the past-result chips) to a trigger
# with {ticker, date}. Future chips carry neither attribute, so they stay inert
# — matching the calendar's "past prints click, upcoming chips don't" rule.
# Re-runs on every data change, so handlers re-attach when the month/window pages.
_PICK_JS = """
export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const root = parentElement.querySelector("#root")
  if (!root) return
  root.innerHTML = (data && data.html) || ""
  for (const el of root.querySelectorAll("[data-ticker][data-date]")) {
    el.style.cursor = "pointer"
    el.onclick = () => {
      setTriggerValue("pick", { ticker: el.dataset.ticker, date: el.dataset.date })
    }
  }
}
"""


def calendar_component(name: str, css: str):
    """A CCv2 grid whose past chips (data-ticker/data-date) click back to Python.

    `name` must be unique per mount site (the full calendar and the mini
    calendar use different names); `css` styles that grid's chips.
    """
    return st.components.v2.component(
        name, html='<div id="root"></div>', js=_PICK_JS, css=css
    )


# ------------------------------------------------------------------ formatting


def _money(value: float | None, currency: str | None = None) -> str:
    """Compact money: 81.6B / 1.24T, prefixed with the currency's symbol."""
    if value is None:
        return "—"
    prefix = CURRENCY_SYMBOL.get(currency or "") or (f"{currency} " if currency else "")
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= div:
            return f"{prefix}{value / div:,.2f}{suffix}"
    return f"{prefix}{value:,.0f}"


def _count(value: float | None) -> str:
    """Compact plain count (share counts) — no currency prefix."""
    return _money(value)


def _signed_pct(fraction: float | None, digits: int = 1) -> str:
    return "—" if fraction is None else f"{fraction * 100:+.{digits}f}%"


def _pct(fraction: float | None, digits: int = 1) -> str:
    return "—" if fraction is None else f"{fraction * 100:.{digits}f}%"


def _eps(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _tone(value: float | None) -> str:
    """Verdict color for kpi_grid_html: green up, red down, gray unknown."""
    if value is None:
        return "gray"
    return "green" if value >= 0 else "red"


def _bps_delta(current: float | None, previous: float | None) -> float | None:
    """Margin move in basis points (both inputs are fractions)."""
    if current is None or previous is None:
        return None
    return (current - previous) * 10_000


def _quarter_label(end: date) -> str:
    """Short fiscal-quarter tag for axes and tables: 'Apr 26'."""
    return f"{tr(f'earnings.mon_{end.month}')} {end.year % 100:02d}"


def _long_date(day: date) -> str:
    return f"{tr(f'earnings.mon_{day.month}')} {day.day:02d}, {day.year}"


# --------------------------------------------------------------------- visuals
# Every block below is ONE self-contained HTML element with inline styles: a
# <style> block would be dropped whole by st.html's sanitizer as soon as any
# text in the same call contained a "<", and these carry user-facing figures.


def _section_head(title: str, sub: str | None = None) -> str:
    """Hairline-topped section label, so the dialog reads as stacked cards."""
    caption = (
        f'<div style="font-size:{FS_SM};color:{TEXT_MUTED};margin-top:3px">'
        f"{_ESC(sub)}</div>"
        if sub
        else ""
    )
    return (
        f'<div style="margin:18px 0 10px;border-top:1px solid {BORDER};padding-top:12px">'
        f'<div style="font-size:{FS_XS};font-weight:600;letter-spacing:.08em;'
        f'text-transform:uppercase;color:{TEXT_FAINT}">{_ESC(title)}</div>'
        f"{caption}</div>"
    )


def _bars_html(rows: list[tuple[str, float | None, str, str]]) -> str:
    """Horizontal bar stack: (label, value, formatted value, fill color) rows.

    Widths are relative to the largest absolute value in the set, so the shape
    of the trend is readable without an axis.
    """
    magnitudes = [abs(v) for _, v, _, _ in rows if v is not None]
    top = max(magnitudes) if magnitudes else 0.0
    cells = []
    for label, value, text, fill in rows:
        width = 0.0 if not top or value is None else max(2.0, abs(value) / top * 100)
        cells.append(
            f'<span style="font-size:{FS_XS};color:{TEXT_MUTED};'
            f'white-space:nowrap">{_ESC(label)}</span>'
            f'<span style="height:10px;border-radius:{RADIUS_PILL};'
            f'background:{SURFACE_SUNKEN};display:block">'
            f'<span style="display:block;height:10px;width:{width:.1f}%;'
            f'border-radius:{RADIUS_PILL};background:{fill}"></span></span>'
            f'<span style="font-size:{FS_XS};color:{TEXT_SECONDARY};'
            f'white-space:nowrap;text-align:right">{_ESC(text)}</span>'
        )
    return (
        '<div style="display:grid;grid-template-columns:auto 1fr auto;'
        f'gap:7px 10px;align-items:center">{"".join(cells)}</div>'
    )


def _chip_html(text: str, tone: str) -> str:
    fill, ink = {
        "green": (PROFIT_BAND, UP_COLOR),
        "red": (LOSS_BAND, DOWN_COLOR),
    }.get(tone, (SURFACE_SUNKEN, TEXT_MUTED))
    return (
        f'<span style="font-size:{FS_XS};font-weight:600;padding:1px 7px;'
        f'border-radius:{RADIUS_PILL};background:{fill};color:{ink};'
        f'white-space:nowrap">{_ESC(text)}</span>'
    )


def _meters_html(items: list[tuple[str, float | None, str, str]]) -> str:
    """Margin gauges: (label, fraction of revenue, chip text, chip tone).

    A margin is a share of one denominator, so it gets a filled track rather
    than a bar scaled against its neighbours — 75% gross next to 65% operating
    then reads as two levels of the same 100%, which is what they are.
    """
    cells = []
    for label, fraction, chip, tone in items:
        width = 0.0 if fraction is None else min(max(fraction * 100, 0.0), 100.0)
        cells.append(
            f'<div style="background:{SURFACE_SUNKEN};border:1px solid {BORDER};'
            f'border-radius:{RADIUS_SM};padding:9px 11px;min-width:0">'
            f'<div style="font-size:{FS_XS};color:{TEXT_MUTED}">{_ESC(label)}</div>'
            f'<div style="display:flex;align-items:baseline;gap:7px;'
            f'flex-wrap:wrap;margin:2px 0 7px">'
            f'<span style="font-size:18px;font-weight:700;color:{TEXT_PRIMARY}">'
            f"{_ESC(_pct(fraction))}</span>"
            f"{_chip_html(chip, tone) if chip else ''}</div>"
            f'<span style="height:6px;border-radius:{RADIUS_PILL};'
            f'background:{BORDER};display:block">'
            f'<span style="display:block;height:6px;width:{width:.1f}%;'
            f'border-radius:{RADIUS_PILL};background:{BRAND_ACCENT}"></span></span>'
            "</div>"
        )
    return (
        '<div style="display:grid;gap:8px;'
        f'grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">{"".join(cells)}</div>'
    )


def _range_html(
    label: str,
    low: float | None,
    avg: float | None,
    high: float | None,
    fmt,
    note: str = "",
) -> str:
    """Consensus dispersion: the low-high band with the mean marked on it."""
    span = None if low is None or high is None else high - low
    if avg is None or span is None or span <= 0:
        position = 50.0
    else:
        position = min(max((avg - low) / span * 100, 4.0), 96.0)
    tail = (
        f'<span style="font-size:{FS_XS};color:{TEXT_MUTED}">{_ESC(note)}</span>'
        if note
        else ""
    )
    return (
        f'<div style="background:{SURFACE_SUNKEN};border:1px solid {BORDER};'
        f'border-radius:{RADIUS_SM};padding:10px 12px;margin-bottom:8px">'
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:baseline;gap:8px;flex-wrap:wrap">'
        f'<span style="font-size:{FS_XS};color:{TEXT_MUTED}">{_ESC(label)}</span>'
        f"{tail}</div>"
        f'<div style="font-size:18px;font-weight:700;color:{TEXT_PRIMARY};'
        f'margin:2px 0 9px">{_ESC(fmt(avg))}</div>'
        # Flex, not absolute positioning: a spacer sized to the mean's place in
        # the range pushes the marker there, so nothing depends on st.html's
        # sanitizer keeping position/left. The solid background is the fallback
        # for the same reason — the gradient only decorates it.
        f'<span style="display:flex;align-items:center;height:8px;'
        f'border-radius:{RADIUS_PILL};background:{INFO_DEEP};'
        f'background-image:linear-gradient(90deg,{INFO_DEEP},{BRAND_ACCENT})">'
        f'<span style="flex:none;width:{position:.1f}%;height:8px"></span>'
        f'<span style="flex:none;width:3px;height:16px;border-radius:2px;'
        f'background:{TEXT_PRIMARY}"></span></span>'
        f'<div style="display:flex;justify-content:space-between;'
        f'font-size:{FS_XS};color:{TEXT_MUTED};margin-top:5px">'
        f"<span>{_ESC(fmt(low))}</span><span>{_ESC(fmt(high))}</span></div>"
        "</div>"
    )


# -------------------------------------------------------------------- sections


def _revenue_section(quarters: list[Quarter], q: Quarter, currency: str | None) -> None:
    prev_y, prev_q = year_ago(quarters, q), prior_quarter(quarters, q)
    yoy = pct_change(q.revenue, prev_y.revenue if prev_y else None)
    qoq = pct_change(q.revenue, prev_q.revenue if prev_q else None)
    trend = quarters[:TREND_QUARTERS]
    # TTM only means something with four consecutive quarters in hand.
    ttm = (
        sum(x.revenue for x in quarters[:4])
        if len(quarters) >= 4 and all(x.revenue is not None for x in quarters[:4])
        else None
    )

    st.html(_section_head(tr("earnings.sec_revenue"), tr("earnings.sub_revenue")))
    st.html(
        kpi_grid_html(
            [
                (
                    tr("earnings.tile_revenue"),
                    _money(q.revenue, currency),
                    (tr("earnings.chip_yoy", pct=_signed_pct(yoy)), _tone(yoy)),
                    None,
                ),
                (
                    tr("earnings.tile_qoq"),
                    _signed_pct(qoq),
                    None,
                    None,
                ),
                (
                    tr("earnings.tile_ttm_revenue"),
                    _money(ttm, currency),
                    None,
                    tr("earnings.tip_ttm"),
                ),
            ]
        )
    )
    st.html(
        _bars_html(
            [
                (
                    _quarter_label(x.end),
                    x.revenue,
                    _money(x.revenue, currency),
                    BRAND_ACCENT if x.end == q.end else INFO_DEEP,
                )
                for x in trend
            ]
        )
    )
    with st.expander(tr("earnings.detail")):
        rows = []
        for x in trend:
            back_y, back_q = year_ago(quarters, x), prior_quarter(quarters, x)
            rows.append(
                {
                    tr("earnings.col_quarter"): _quarter_label(x.end),
                    tr("earnings.col_revenue"): _money(x.revenue, currency),
                    tr("earnings.col_yoy"): _signed_pct(
                        pct_change(x.revenue, back_y.revenue if back_y else None)
                    ),
                    tr("earnings.col_qoq"): _signed_pct(
                        pct_change(x.revenue, back_q.revenue if back_q else None)
                    ),
                    tr("earnings.col_gross_profit"): _money(x.gross_profit, currency),
                }
            )
        data_table(
            pd.DataFrame(rows),
            title=tr("earnings.col_quarter"),
            hide_index=True,
        )


def _margin_section(quarters: list[Quarter], q: Quarter) -> None:
    prev_y = year_ago(quarters, q)

    def gauge(label_key: str, current: float | None, previous: float | None):
        bps = _bps_delta(current, previous)
        chip = (
            tr("earnings.chip_bps", bps=f"{bps:+.0f}") if bps is not None else ""
        )
        return (tr(label_key), current, chip, _tone(bps))

    st.html(_section_head(tr("earnings.sec_margins"), tr("earnings.sub_margins")))
    st.html(
        _meters_html(
            [
                gauge(
                    "earnings.tile_gross_margin",
                    q.gross_margin,
                    prev_y.gross_margin if prev_y else None,
                ),
                gauge(
                    "earnings.tile_operating_margin",
                    q.operating_margin,
                    prev_y.operating_margin if prev_y else None,
                ),
                gauge(
                    "earnings.tile_net_margin",
                    q.net_margin,
                    prev_y.net_margin if prev_y else None,
                ),
            ]
        )
    )
    with st.expander(tr("earnings.detail")):
        frame = pd.DataFrame(
            [
                {
                    tr("earnings.col_quarter"): _quarter_label(x.end),
                    tr("earnings.col_gross"): _pct(x.gross_margin),
                    tr("earnings.col_operating"): _pct(x.operating_margin),
                    tr("earnings.col_net"): _pct(x.net_margin),
                    tr("earnings.col_rnd"): _pct(x.rnd_intensity),
                }
                for x in quarters[:TREND_QUARTERS]
            ]
        )
        data_table(frame, title=tr("earnings.col_quarter"), hide_index=True)
        st.caption(tr("earnings.margins_caption"))


def _gaap_section(quarters: list[Quarter], q: Quarter, currency: str | None) -> None:
    prev_y = year_ago(quarters, q)
    ni_yoy = pct_change(q.net_income, prev_y.net_income if prev_y else None)
    share_change = pct_change(
        q.diluted_shares, prev_y.diluted_shares if prev_y else None
    )

    st.html(_section_head(tr("earnings.sec_gaap"), tr("earnings.sub_gaap")))
    st.html(
        kpi_grid_html(
            [
                (
                    tr("earnings.tile_net_income"),
                    _money(q.net_income, currency),
                    (tr("earnings.chip_yoy", pct=_signed_pct(ni_yoy)), _tone(ni_yoy)),
                    None,
                ),
                (
                    tr("earnings.tile_pretax"),
                    _money(q.pretax_income, currency),
                    None,
                    None,
                ),
                (
                    tr("earnings.tile_tax_rate"),
                    _pct(q.tax_rate),
                    None,
                    tr("earnings.tip_tax_rate"),
                ),
                (
                    tr("earnings.tile_shares"),
                    _count(q.diluted_shares),
                    (
                        tr("earnings.chip_yoy", pct=_signed_pct(share_change)),
                        # Dilution is the bad direction here: fewer shares
                        # (buybacks) lifts per-share value, more dilutes it.
                        _tone(-share_change if share_change is not None else None),
                    ),
                    tr("earnings.tip_shares"),
                ),
            ]
        )
    )
    with st.expander(tr("earnings.detail")):
        labels = [
            (tr("earnings.col_revenue"), "revenue", _money),
            (tr("earnings.col_gross_profit"), "gross_profit", _money),
            (tr("earnings.col_operating_income"), "operating_income", _money),
            (tr("earnings.col_pretax"), "pretax_income", _money),
            (tr("earnings.col_tax"), "tax_provision", _money),
            (tr("earnings.col_net_income"), "net_income", _money),
            (tr("earnings.col_gaap_eps"), "diluted_eps", None),
        ]
        trend = quarters[:TREND_QUARTERS]
        frame = pd.DataFrame(
            [
                {
                    tr("earnings.col_line"): label,
                    **{
                        _quarter_label(x.end): (
                            fmt(getattr(x, field), currency)
                            if fmt
                            else _eps(getattr(x, field))
                        )
                        for x in trend
                    },
                }
                for label, field, fmt in labels
            ]
        )
        # Line items run down the rows and quarters across the columns, so the
        # phone cards transpose it: one card per quarter, one line per item.
        data_table(
            frame.set_index(tr("earnings.col_line")).T,
            index_title=True,
            hide_index=True,
        )


def _eps_section(result, q: Quarter | None, results: list) -> None:
    """Headline (often adjusted) EPS vs the filed GAAP figure, plus the record."""
    gaap = q.diluted_eps if q else None
    gap = (
        result.reported_eps - gaap
        if result.reported_eps is not None and gaap is not None
        else None
    )
    history = [x for x in results if x.ticker == result.ticker]

    st.html(_section_head(tr("earnings.sec_eps"), tr("earnings.sub_eps")))
    st.html(
        kpi_grid_html(
            [
                (
                    tr("earnings.reported_eps"),
                    _eps(result.reported_eps),
                    (
                        (
                            tr(
                                "earnings.surprise_vs_est",
                                pct=f"{result.surprise_pct:+.2f}",
                            ),
                            _tone(result.surprise_pct),
                        )
                        if result.surprise_pct is not None
                        else None
                    ),
                    tr("earnings.tip_headline_eps"),
                ),
                (
                    tr("earnings.tile_gaap_eps"),
                    _eps(gaap),
                    (
                        (tr("earnings.chip_vs_gaap", delta=f"{gap:+.2f}"), "gray")
                        if gap is not None
                        else None
                    ),
                    tr("earnings.tip_gaap_eps"),
                ),
                (
                    tr("earnings.eps_estimate"),
                    _eps(result.eps_estimate),
                    None,
                    None,
                ),
            ]
        )
    )
    surprises = [x for x in history if x.surprise_pct is not None][-SURPRISE_QUARTERS:]
    if surprises:
        st.caption(tr("earnings.eps_surprise_trend"))
        st.html(
            _bars_html(
                [
                    (
                        _quarter_label(x.date),
                        x.surprise_pct,
                        f"{x.surprise_pct:+.1f}%",
                        SUCCESS_FILL if x.surprise_pct >= 0 else CRITICAL_FILL,
                    )
                    for x in reversed(surprises)
                ]
            )
        )
    if len(history) > 1:
        with st.expander(tr("earnings.detail")):
            frame = pd.DataFrame(
                {
                    "Date": [x.date for x in history],
                    "EPS est": [x.eps_estimate for x in history],
                    "Reported": [x.reported_eps for x in history],
                    "Surprise": [x.surprise_pct for x in history],
                }
            )
            data_table(
                frame,
                title="Date",
                fmt={
                    "EPS est": "{:.2f}",
                    "Reported": "{:.2f}",
                    "Surprise": "{:+.1f}%",
                },
                signed=("Surprise",),
                labels={
                    "EPS est": tr("earnings.col_eps_est"),
                    "Reported": tr("earnings.col_reported"),
                    "Surprise": tr("earnings.col_surprise"),
                },
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn(tr("earnings.col_date")),
                    "EPS est": st.column_config.NumberColumn(
                        tr("earnings.col_eps_est"), format="%.2f"
                    ),
                    "Reported": st.column_config.NumberColumn(
                        tr("earnings.col_reported"), format="%.2f"
                    ),
                    "Surprise": st.column_config.NumberColumn(
                        tr("earnings.col_surprise"), format="%+.1f%%"
                    ),
                },
            )


_PERIOD_LABELS = {
    CURRENT_Q: "earnings.period_0q",
    NEXT_Q: "earnings.period_1q",
    CURRENT_FY: "earnings.period_0y",
    NEXT_FY: "earnings.period_1y",
}


def _outlook_section(ticker: str, outlook: QuarterOutlook) -> None:
    """Forward view. Company guidance is in no free feed — this is consensus."""
    ccy = outlook.currency
    st.html(_section_head(tr("earnings.sec_outlook"), tr("earnings.sub_outlook")))
    if outlook.rev_avg is not None:
        st.html(
            _range_html(
                tr("earnings.outlook_revenue"),
                outlook.rev_low,
                outlook.rev_avg,
                outlook.rev_high,
                lambda v: _money(v, ccy),
                note=" · ".join(
                    x
                    for x in (
                        tr("earnings.chip_yoy", pct=_signed_pct(outlook.rev_growth))
                        if outlook.rev_growth is not None
                        else "",
                        tr("earnings.chip_analysts", n=outlook.rev_analysts)
                        if outlook.rev_analysts
                        else "",
                    )
                    if x
                ),
            )
        )
    if outlook.eps_avg is not None:
        st.html(
            _range_html(
                tr("earnings.outlook_eps"),
                outlook.eps_low,
                outlook.eps_avg,
                outlook.eps_high,
                _eps,
                note=" · ".join(
                    x
                    for x in (
                        tr("earnings.chip_yoy", pct=_signed_pct(outlook.eps_growth))
                        if outlook.eps_growth is not None
                        else "",
                        tr("earnings.chip_analysts", n=outlook.eps_analysts)
                        if outlook.eps_analysts
                        else "",
                    )
                    if x
                ),
            )
        )
    with st.expander(tr("earnings.detail")):
        raw = _estimates(ticker)
        rows = []
        for period, key in _PERIOD_LABELS.items():
            view = quarter_outlook(raw, period)
            if view.empty:
                continue
            rows.append(
                {
                    tr("earnings.col_period"): tr(key),
                    tr("earnings.col_eps_avg"): _eps(view.eps_avg),
                    tr("earnings.col_eps_range"): (
                        f"{_eps(view.eps_low)} – {_eps(view.eps_high)}"
                    ),
                    tr("earnings.col_rev_avg"): _money(view.rev_avg, view.currency),
                    tr("earnings.col_rev_range"): (
                        f"{_money(view.rev_low, view.currency)} – "
                        f"{_money(view.rev_high, view.currency)}"
                    ),
                    tr("earnings.col_yoy"): _signed_pct(view.rev_growth),
                }
            )
        if rows:
            data_table(
                pd.DataFrame(rows),
                title=tr("earnings.col_period"),
                hide_index=True,
            )
        st.caption(tr("earnings.outlook_caption"))


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _estimates(ticker: str):
    """Raw estimate frames, for the outlook expander's per-period grid."""
    return fetch_estimates(ticker)


def _breakdown(result, results: list) -> None:
    """The five sections under the headline tiles, or nothing when unavailable.

    The whole block shimmers as one: it is three network payloads deep, and the
    dialog opens on a click, so the sections have to reserve their height
    instead of pushing the tiles around as each lands.
    """
    with skeletons.slot("table", rows=6, cols=4) as box:
        try:
            quarters, currency, outlook = quarter_detail(result.ticker)
        except (YFRateLimitError, URLError) as exc:
            notices.data_toast(exc)
            return
        except Exception:
            return  # no breakdown this run; the headline tiles still stand
        q = match_quarter(quarters, result.date) if quarters else None
        with box.container():
            if q is not None:
                _revenue_section(quarters, q, currency)
                _margin_section(quarters, q)
                _gaap_section(quarters, q, currency)
            _eps_section(result, q, results)
            if not outlook.empty:
                _outlook_section(result.ticker, outlook)
            if q is None:
                st.caption(
                    tr("earnings.quarter_pending")
                    if quarters
                    else tr("earnings.no_quarter_data")
                )


def render_result_body(
    ticker: str,
    iso: str,
    results: list,
    names: dict[str, str],
    logos: dict[str, str | None],
) -> None:
    """The earnings-result dialog body: what the street expected vs what printed.

    Kept decorator-free (no `@st.dialog`) so each page wraps it with its own
    per-run `@st.dialog(tr("earnings.dialog_title"))` — the title has to be
    evaluated per run to honor a mid-session language switch.
    """
    d = date.fromisoformat(iso)
    r = next((x for x in results if x.ticker == ticker and x.date == d), None)

    head_logo, head_txt = st.columns([1, 6], vertical_alignment="center")
    if src := logos.get(ticker):
        # An <img> tag, not st.image: `logo()` hands back a BROWSER URL — a
        # relative "app/static/logos/…" for a mirrored file, or an external
        # CDN URL. st.image treats the relative form as a filesystem path,
        # fails to open it and raises MediaFileStorageError. Every other logo
        # site renders markup, so this one does too.
        head_logo.html(
            f'<img src="{html.escape(src, quote=True)}" alt="" loading="lazy" '
            f'style="width:40px;height:40px;border-radius:{RADIUS_SM};'
            # Opaque plate behind transparent brand marks — same as the
            # ticker-page header.
            f"background:{TEXT_PRIMARY};border:1px solid {BORDER};"
            f'box-sizing:border-box;padding:5px;object-fit:contain">'
        )
    head_txt.markdown(
        f"**{ticker}** — {names.get(ticker, ticker)}  \n"
        + tr("earnings.reported_on", date=_long_date(d))
    )

    if r is None:
        st.info(tr("earnings.no_figures"))
        return

    # The price reaction is a fresh fetch on first open — the three tiles
    # shimmer so the dialog opens at its final height instead of growing under
    # the pointer.
    with skeletons.slot("metrics", n=3) as tiles:
        move = reaction(ticker, iso)
        with tiles.container():
            m1, m2, m3 = st.columns(3)
            m1.metric(
                tr("earnings.reported_eps"),
                f"{r.reported_eps:.2f}" if r.reported_eps is not None else "—",
                delta=tr("earnings.surprise_vs_est", pct=f"{r.surprise_pct:+.2f}")
                if r.surprise_pct is not None
                else None,
            )
            m2.metric(
                tr("earnings.eps_estimate"),
                f"{r.eps_estimate:.2f}" if r.eps_estimate is not None else "—",
            )
            m3.metric(
                tr("earnings.price_reaction"),
                f"{move:+.2f}%" if move is not None else "—",
                delta=f"{move:+.2f}%" if move is not None else None,
            )
            st.caption(tr("earnings.price_reaction_caption"))

    _breakdown(r, results)
