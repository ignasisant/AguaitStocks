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
from stocks.data.earnings import (
    add_months,
    calendar_events,
    group_by_date,
    month_weeks,
    price_reaction,
)
from stocks.web import auth
from stocks.web.widgets import db_mtime, held_tickers, is_mobile, ticker_table_html
from stocks.web.widgets import logo as _logo

st.title("Earnings calendar")

from stocks.data.crypto import is_crypto

# Crypto never reports earnings — drop pairs before the calendar fetch.
holdings = [
    h for h in load_watchlist(auth.watchlist_path()) if not is_crypto(h.ticker)
]
if not holdings:
    st.warning(
        "No stocks on the watchlist (crypto has no earnings). Add tickers on "
        "the **Profile** page."
    )
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
    _groups["Portfolio"] = _portfolio
if favs := {h.ticker for h in holdings if h.favorite}:
    _groups["Favorites"] = favs
_tag_groups: dict[str, set[str]] = {}
for h in holdings:
    for t in h.tags:
        _tag_groups.setdefault(t, set()).add(h.ticker)
_groups.update(sorted(_tag_groups.items()))


@st.cache_data(ttl=6 * 3600, show_spinner="Fetching earnings dates…")
def _calendar_data(sig: tuple):
    # One parallel yfinance pass -> (upcoming events, past reported results).
    # The calendar places both on their month; the list splits them into two
    # tables. `sig` (the ticker tuple) keys the cache so watchlist edits
    # invalidate it. Pass this page's holdings — the session user's watchlist
    # minus crypto — not calendar_events' default (the root watchlist).
    return calendar_events(holdings)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _reaction(ticker: str, iso: str) -> float | None:
    return price_reaction(ticker, date.fromisoformat(iso))


events, results = _calendar_data(tuple(tickers))
if not events and not results:
    st.info("No earnings dates found for the watchlist.")
    st.stop()

logos = {t: _logo(t) for t in {e.ticker for e in events} | {r.ticker for r in results}}
today = date.today()
imminent = sum(1 for e in events if e.days_until is not None and e.days_until <= 7)
if imminent:
    st.warning(f"{imminent} report(s) within 7 days.")

# ────────────────────────────────────────────────────────────── calendar view
CAL_CSS = """
  .earn-cal {width:100%; border-collapse:collapse; table-layout:fixed;}
  .earn-cal th {font-size:0.72rem; text-transform:uppercase; letter-spacing:.04em;
                color:#9aa0aa; font-weight:600; padding:0.3rem 0.4rem; text-align:left;}
  .earn-cal td {border:1px solid rgba(140,140,140,.18); vertical-align:top;
                height:96px; padding:0.3rem 0.35rem; width:14.28%;}
  .earn-cal td.dim {background:rgba(140,140,140,.05);}
  .earn-cal td.today {background:rgba(56,132,255,.12); border-color:rgba(56,132,255,.55);}
  .earn-daynum {font-size:0.78rem; color:#8b9099; font-weight:600; margin-bottom:0.2rem;}
  .earn-cal td.today .earn-daynum {color:#3884ff;}
  .earn-chip {display:flex; align-items:center; gap:0.3rem; margin:0.12rem 0;
              padding:0.1rem 0.28rem; border-radius:6px; background:rgba(140,140,140,.12);
              font-size:0.72rem; font-weight:600; line-height:1.3;
              color:var(--st-text-color, inherit);
              font-family:var(--st-font, inherit);}
  .earn-chip.soon {background:rgba(255,86,86,.16); color:#ff7a7a;}
  .earn-chip img {width:16px; height:16px; border-radius:3px; object-fit:contain;}
  .earn-chip span {white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .earn-chip.past {cursor:pointer;}
  .earn-chip.past:hover {filter:brightness(1.3);}
  .earn-chip.beat {background:rgba(61,213,109,.16); color:#41c96b;}
  .earn-chip.miss {background:rgba(255,86,86,.16); color:#ff7a7a;}
"""

# Clickable grid: Python hands the finished table HTML over as data, JS wires
# each past chip to a trigger carrying {ticker, date}. Re-runs on every data
# change, so handlers re-attach when the month pages.
_CAL_JS = """
export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const root = parentElement.querySelector("#root")
  if (!root) return
  root.innerHTML = (data && data.html) || ""
  for (const el of root.querySelectorAll(".earn-chip.past")) {
    el.onclick = () => {
      setTriggerValue("pick", { ticker: el.dataset.ticker, date: el.dataset.date })
    }
  }
}
"""

_calendar_grid = st.components.v2.component(
    "earnings_calendar",
    html='<div id="root"></div>',
    js=_CAL_JS,
    css=CAL_CSS,
)


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
        est = f" vs est {r.eps_estimate:.2f}" if r.eps_estimate is not None else ""
        bits.append(f"EPS {r.reported_eps:.2f}{est}")
    if r.surprise_pct is not None:
        bits.append(f"{r.surprise_pct:+.1f}%")
    title = html.escape(" · ".join(bits) + " — click for details")
    return (
        f'<div class="earn-chip past{verdict}" title="{title}"'
        f' data-ticker="{html.escape(r.ticker)}" data-date="{r.date.isoformat()}">'
        f'{img}<span>{html.escape(r.ticker)}{arrow}</span></div>'
    )


@st.dialog("Earnings result")
def _result_dialog(ticker: str, iso: str) -> None:
    d = date.fromisoformat(iso)
    r = next((x for x in results if x.ticker == ticker and x.date == d), None)

    head_logo, head_txt = st.columns([1, 6], vertical_alignment="center")
    if src := logos.get(ticker):
        head_logo.image(src, width=40)
    head_txt.markdown(
        f"**{ticker}** — {names.get(ticker, ticker)}  \n"
        f":gray[Reported {d.strftime('%b %d, %Y')}]"
    )

    if r is None:
        st.info("No reported figures for this date.")
        return

    move = _reaction(ticker, iso)
    m1, m2, m3 = st.columns(3)
    m1.metric(
        "Reported EPS",
        f"{r.reported_eps:.2f}" if r.reported_eps is not None else "—",
        delta=f"{r.surprise_pct:+.2f}% vs est" if r.surprise_pct is not None else None,
    )
    m2.metric(
        "EPS estimate",
        f"{r.eps_estimate:.2f}" if r.eps_estimate is not None else "—",
    )
    m3.metric(
        "Price reaction",
        f"{move:+.2f}%" if move is not None else "—",
        delta=f"{move:+.2f}%" if move is not None else None,
    )
    st.caption(
        "Price reaction spans the print: last close before the date vs first "
        "close after, so it always contains the gap regardless of pre/post "
        "market timing."
    )

    history = [x for x in results if x.ticker == ticker]
    if len(history) > 1:
        st.markdown("**Recent quarters**")
        frame = pd.DataFrame(
            {
                "Date": [x.date for x in history],
                "EPS est": [x.eps_estimate for x in history],
                "Reported": [x.reported_eps for x in history],
                "Surprise": [x.surprise_pct for x in history],
            }
        )
        st.dataframe(
            frame,
            hide_index=True,
            column_config={
                "EPS est": st.column_config.NumberColumn(format="%.2f"),
                "Reported": st.column_config.NumberColumn(format="%.2f"),
                "Surprise": st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )


def _render_calendar(offset: int, events, results) -> None:
    year, month = add_months(today.year, today.month, offset)
    by_date = group_by_date(events)
    res_by_date = group_by_date(results)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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
            cells.append(f'<td{cls_attr}><div class="earn-daynum">{day.day}</div>{chips}</td>')
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
    with st.container(horizontal=True, vertical_alignment="center"):
        view = st.segmented_control(
            "View",
            ["Calendar", "List"],
            default="List" if is_mobile() else "Calendar",
            label_visibility="collapsed",
        )
        picked = (
            st.pills(
                "Filter",
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
            st.info("No earnings dates match the selected filters.")
            return
    else:
        f_events, f_results = events, results

    if view == "List":
        # Positions-style shared table: logo + name live in the ticker cell,
        # surprise gets the semantic green/red beat/miss color.
        if f_events:
            st.markdown("**Upcoming**")
            frame = pd.DataFrame(
                {
                    "ticker": [e.ticker for e in f_events],
                    "date": [e.date for e in f_events],
                    "days out": [e.days_until for e in f_events],
                }
            )
            st.html(
                ticker_table_html(
                    frame, fmt={"days out": "{:.0f} d"}, left_cols=("date",)
                )
            )
        if f_results:
            st.markdown("**Past results**")
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
        nav_today.button("Today", on_click=_reset_month, width="stretch")
        nav_label.subheader(date(y, m, 1).strftime("%B %Y"))

        _render_calendar(offset, f_events, f_results)
        st.caption(
            "Red chips = report within 7 days. Green ▲ / red ▼ = past beat/miss "
            "— click for the result overview. Blue cell = today."
        )


_views()
