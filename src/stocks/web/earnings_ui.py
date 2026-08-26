"""Shared earnings-calendar UI: the past-result dialog and the clickable
calendar component.

Both the full Earnings page and the home dashboard's mini-calendar render past
prints as green/red beat/miss chips that open a result overview on click. That
overview (EPS est vs reported, surprise, price reaction, recent-quarters table)
and the CCv2 plumbing that flows chip clicks back to Python live here so the two
pages stay identical instead of drifting.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from stocks.data.earnings import price_reaction
from stocks.web import skeletons
from stocks.web.i18n import t as tr


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def reaction(ticker: str, iso: str) -> float | None:
    """% price move across a past print, cached (shared by both calendars)."""
    return price_reaction(ticker, date.fromisoformat(iso))


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
    return st.components.v2.component(name, html='<div id="root"></div>', js=_PICK_JS, css=css)


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
        head_logo.image(src, width=40)
    reported = f"{tr(f'earnings.mon_{d.month}')} {d.day:02d}, {d.year}"
    head_txt.markdown(
        f"**{ticker}** — {names.get(ticker, ticker)}  \n"
        + tr("earnings.reported_on", date=reported)
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

    history = [x for x in results if x.ticker == ticker]
    if len(history) > 1:
        st.markdown(tr("earnings.recent_quarters"))
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
