"""Earnings calendar page — upcoming reports and past results for the watchlist.

Aggressive growth names gap hard on prints; this is the heads-up before them
and the scorecard after. Two views over the same data: a month **calendar**
grid (default) and a flat **list**. Future prints render as neutral chips
(red when imminent); past prints are green/red beat/miss chips — click one to
get a result overview dialog (EPS est vs reported, surprise, price reaction).

The calendar grid is a tiny CCv2 component so chip clicks flow back to Python;
the view switcher, filter pills (Portfolio / Favorites / watchlist tags, union
across picks) and month navigation live in a fragment so paging months or
toggling filters reruns only the calendar block, not the whole page.
"""

from __future__ import annotations

import html
from datetime import date

import pandas as pd
import streamlit as st

from stocks.config import load_watchlist
from stocks.data.crypto import is_crypto
from stocks.data.earnings import (
    add_months,
    calendar_events,
    group_by_date,
    month_weeks,
)
from stocks.web import auth, skeletons
from stocks.web.earnings_ui import calendar_component, render_result_body
from stocks.web.i18n import t as tr
from stocks.web.widgets import (
    calendar_css,
    db_mtime,
    held_tickers,
    is_mobile,
    ticker_table_html,
)
from stocks.web.widgets import logo as _logo

st.title(tr("earnings.title"))

# Crypto never reports earnings — drop pairs before the calendar fetch.
holdings = [
    h for h in load_watchlist(auth.watchlist_path()) if not is_crypto(h.ticker)
]
if not holdings:
    st.warning(tr("earnings.no_stocks"))
    st.stop()

tickers = [h.ticker for h in holdings]
names = {h.ticker: (h.name or h.ticker) for h in holdings}

# Filter groups for the pills row: Portfolio = open ledger positions (plus any
# watchlist entry with shares), Favorites = the starred set, then one group per
# watchlist tag. Empty groups are dropped so the row only offers live filters.
_db = str(auth.db_path())
_groups: dict[str, set[str]] = {}
_portfolio = set(held_tickers(_db, db_mtime(_db))) | {
    h.ticker for h in holdings if h.is_position
}
if _portfolio:
    _groups[tr("earnings.filter_portfolio")] = _portfolio
if favs := {h.ticker for h in holdings if h.favorite}:
    _groups[tr("earnings.filter_favorites")] = favs
_tag_groups: dict[str, set[str]] = {}
for h in holdings:
    for t in h.tags:
        _tag_groups.setdefault(t, set()).add(h.ticker)
_groups.update(sorted(_tag_groups.items()))


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _calendar_data(sig: tuple):
    # One parallel yfinance pass -> (upcoming events, past reported results).
    # The calendar places both on their month; the list splits them into two
    # tables. `sig` (the ticker tuple) keys the cache so watchlist edits
    # invalidate it. Pass this page's holdings — the session user's watchlist
    # minus crypto — not calendar_events' default (the root watchlist).
    return calendar_events(holdings)


# One parallel pass over the whole watchlist, then a logo lookup per reporting
# name: the page has nothing to show until both are done. Reserve the body in
# the shape of the view that is actually coming — phones default to the list,
# desktop to the month grid — and fill it below.
_body_slot = (
    skeletons.reserve("table", rows=8, cols=3)
    if is_mobile()
    else skeletons.reserve("calendar", weeks=5, cols=7, cell=96)
)
events, results = _calendar_data(tuple(tickers))
if not events and not results:
    _body_slot.container().info(tr("earnings.no_dates"))
    st.stop()

logos = {t: _logo(t) for t in {e.ticker for e in events} | {r.ticker for r in results}}
today = date.today()
imminent = sum(1 for e in events if e.days_until is not None and e.days_until <= 7)
# Claimed once and reused: the warning and the view below both belong in the
# body the skeleton was holding open.
_body = _body_slot.container()
if imminent:
    _body.warning(tr("earnings.imminent_warning", n=imminent))

# ────────────────────────────────────────────────────────────── calendar view
# Seven columns at page width — the shared grid, regular density.
CAL_CSS = calendar_css(
    "earn", density="regular", cell_height="96px", cell_width="14.28%"
)

# Clickable grid: Python hands the finished table HTML over as data, the shared
# component wires each past chip (data-ticker/data-date) to a {ticker, date}
# trigger. Re-runs on every data change, so handlers re-attach when the month
# pages.
_calendar_grid = calendar_component("earnings_calendar", CAL_CSS)


def _chip(ev) -> str:
    src = logos.get(ev.ticker)
    img = f'<img src="{html.escape(src)}">' if src else ""
    soon = " soon" if ev.days_until is not None and ev.days_until <= 7 else ""
    title = html.escape(f"{ev.ticker} — {names.get(ev.ticker, ev.ticker)}")
    return (
        f'<div class="earn-chip{soon}" title="{title}">'
        f'{img}<span>{html.escape(ev.ticker)}</span></div>'
    )


def _result_chip(r) -> str:
    src = logos.get(r.ticker)
    img = f'<img src="{html.escape(src)}">' if src else ""
    verdict = "" if r.beat is None else (" beat" if r.beat else " miss")
    arrow = "" if r.beat is None else (" ▲" if r.beat else " ▼")
    bits = [f"{r.ticker} — {names.get(r.ticker, r.ticker)}"]
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
        f'<div class="earn-chip past{verdict}" title="{title}"'
        f' data-ticker="{html.escape(r.ticker)}" data-date="{r.date.isoformat()}">'
        f'{img}<span>{html.escape(r.ticker)}{arrow}</span></div>'
    )


@st.dialog(tr("earnings.dialog_title"), width="large")
def _result_dialog(ticker: str, iso: str) -> None:
    render_result_body(ticker, iso, results, names, logos)


def _render_calendar(offset: int, events, results) -> None:
    year, month = add_months(today.year, today.month, offset)
    by_date = group_by_date(events)
    res_by_date = group_by_date(results)
    weekdays = [
        tr("earnings.wd_mon"),
        tr("earnings.wd_tue"),
        tr("earnings.wd_wed"),
        tr("earnings.wd_thu"),
        tr("earnings.wd_fri"),
        tr("earnings.wd_sat"),
        tr("earnings.wd_sun"),
    ]

    rows = ["<tr>" + "".join(f"<th>{d}</th>" for d in weekdays) + "</tr>"]
    for week in month_weeks(year, month):
        cells = []
        for day in week:
            cls = []
            if day.month != month:
                cls.append("dim")
            if day == today:
                cls.append("today")
            chips = "".join(_result_chip(r) for r in res_by_date.get(day, []))
            chips += "".join(_chip(e) for e in by_date.get(day, []))
            cls_attr = f' class="{" ".join(cls)}"' if cls else ""
            cells.append(
                f'<td{cls_attr}><div class="earn-daynum">{day.day}</div>{chips}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    grid = _calendar_grid(
        data={"html": f'<table class="earn-cal">{"".join(rows)}</table>'},
        key="earn_cal",
        on_pick_change=lambda: None,
    )
    if grid.pick:
        _result_dialog(grid.pick["ticker"], grid.pick["date"])


def _shift_month(delta: int) -> None:
    st.session_state.cal_offset += delta


def _reset_month() -> None:
    st.session_state.cal_offset = 0


@st.fragment
def _views() -> None:
    """Calendar/list switcher, filters and month navigation — reruns independently."""
    # Phones default to the list: the 7-column month grid squeezes cells to
    # ~55px at 390px viewport and the chips ellipsize to nothing. The grid
    # stays one tap away.
    v_calendar = tr("earnings.view_calendar")
    v_list = tr("earnings.view_list")
    with st.container(horizontal=True, vertical_alignment="center"):
        view = st.segmented_control(
            tr("earnings.view_label"),
            [v_calendar, v_list],
            default=v_list if is_mobile() else v_calendar,
            label_visibility="collapsed",
        )
        picked = (
            st.pills(
                tr("earnings.filter_label"),
                list(_groups),
                selection_mode="multi",
                label_visibility="collapsed",
                key="earn_filter",
            )
            if _groups
            else []
        )

    # Union across selected groups; no selection = everything. Fetch above is
    # cached on the full watchlist, so toggling pills is frame-cheap.
    if picked:
        allowed = set().union(*(_groups[p] for p in picked))
        f_events = [e for e in events if e.ticker in allowed]
        f_results = [r for r in results if r.ticker in allowed]
        if not f_events and not f_results:
            st.info(tr("earnings.no_match_filters"))
            return
    else:
        f_events, f_results = events, results

    if view == v_list:
        # Positions-style shared table: logo + name live in the ticker cell,
        # surprise gets the semantic green/red beat/miss color.
        if f_events:
            st.markdown(tr("earnings.upcoming"))
            frame = pd.DataFrame(
                {
                    "ticker": [e.ticker for e in f_events],
                    "date": [e.date for e in f_events],
                    "days out": [e.days_until for e in f_events],
                }
            )
            st.html(
                ticker_table_html(
                    frame,
                    fmt={"days out": "{:.0f} d"},
                    left_cols=("date",),
                    labels={
                        "ticker": tr("earnings.list_col_ticker"),
                        "date": tr("earnings.list_col_date"),
                        "days out": tr("earnings.list_col_days_out"),
                    },
                    mobile={"value": "days out", "delta": "date"},
                )
            )
        if f_results:
            st.markdown(tr("earnings.past_results"))
            past = pd.DataFrame(
                {
                    "ticker": [r.ticker for r in f_results],
                    "date": [r.date for r in f_results],
                    "eps est": [r.eps_estimate for r in f_results],
                    "reported": [r.reported_eps for r in f_results],
                    "surprise": [r.surprise_pct for r in f_results],
                }
            )
            st.html(
                ticker_table_html(
                    past,
                    fmt={
                        "eps est": "{:.2f}",
                        "reported": "{:.2f}",
                        "surprise": "{:+.1f}%",
                    },
                    signed=("surprise",),
                    left_cols=("date",),
                    labels={
                        "ticker": tr("earnings.list_col_ticker"),
                        "date": tr("earnings.list_col_date"),
                        "eps est": tr("earnings.list_col_eps_est"),
                        "reported": tr("earnings.list_col_reported"),
                        "surprise": tr("earnings.list_col_surprise"),
                    },
                    mobile={
                        "value": "reported",
                        "delta": "surprise",
                        "sub": ("date", "eps est"),
                        "sub_labels": {"eps est": tr("earnings.list_col_eps_est")},
                    },
                )
            )
    else:
        st.session_state.setdefault("cal_offset", 0)
        offset = st.session_state.cal_offset
        y, m = add_months(today.year, today.month, offset)

        nav_prev, nav_label, nav_next, nav_today = st.columns([1, 4, 1, 1])
        nav_prev.button(
            ":material/chevron_left:", on_click=_shift_month, args=(-1,), width="stretch"
        )
        nav_next.button(
            ":material/chevron_right:", on_click=_shift_month, args=(1,), width="stretch"
        )
        nav_today.button(tr("earnings.today_btn"), on_click=_reset_month, width="stretch")
        nav_label.subheader(f"{tr(f'earnings.month_{m}')} {y}")

        _render_calendar(offset, f_events, f_results)
        st.caption(tr("earnings.calendar_legend"))


with _body:
    _views()
