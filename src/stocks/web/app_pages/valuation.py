"""Valuation page — TIKR-style DCF + reverse-DCF, driven by live sliders.

Move growth / discount / terminal / horizon and watch the implied fair value,
bull-base-bear upside, and (the useful one) the FCF growth the current price
already assumes. Fundamentals + consensus are fetched once per ticker and
cached; the sliders only re-run the pure math, so it stays instant.

Everything here is *derived* — as good as the assumptions. The "terminal %"
readout is how much of the value leans on the terminal value; treat a high
number as low confidence.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from stocks.analysis.valuation import DcfInputs, summarize
from stocks.analysis.valuation import gather as gather_valuation
from stocks.formatting import pct
from stocks.web.widgets import is_mobile, metric_cells, show_chart

_MOBILE = is_mobile()

st.title("DCF valuation")

with st.expander("How to read this page", icon=":material/school:"):
    st.markdown(
        """
- **DCF (discounted cash flow)** projects free cash flow over the forecast
  horizon, discounts every year back to today at your **discount rate** (your
  required annual return), adds a **terminal value** for everything after the
  horizon, and divides by the share count — a fair value per share.
- **terminal N% of value** — how much of that fair value comes from the
  terminal guess rather than the explicit forecast. High (say >75%) means the
  answer leans on the far future: treat it as low confidence.
- **Bull / base / bear** are the same model at base growth ± the spread. The
  **prob-weighted fair value** blends them 25% / 50% / 25%.
- **Margin of safety** — how far the base fair value sits above (+) or below
  (−) today's price; your cushion for being wrong.
- **Reverse-DCF** inverts the model: the constant FCF growth that would
  *justify today's price*. If the market's implied growth is above what you
  believe the business can do, the price is demanding; below it, beatable.

Every number here is **derived** — only as good as the assumptions you set in
the sliders. Growth is prefilled from analyst consensus when available.
"""
    )


def _pct(x: float | None, signed: bool = False) -> str | None:
    return pct(x, signed=signed, na=None)  # None hides st.metric deltas


# Selection comes from the global sidebar picker (web/app.py) — the same
# ticker you were analyzing. Type any off-watchlist symbol there to value it.
ticker = (st.session_state.get("picker_selected") or "").strip().upper()
if not ticker:
    st.info("Search or pick a ticker to value.")
    st.stop()

from stocks.data.crypto import is_crypto

if is_crypto(ticker):
    st.info(
        f"{ticker} is a crypto asset — a DCF discounts free cash flow, and "
        "coins don't produce any. Pick a stock to value."
    )
    st.stop()


@st.cache_data(ttl=3600, show_spinner="Fetching fundamentals + consensus…")
def _data(tkr: str):
    return gather_valuation(tkr)


data = _data(ticker)
inp0 = data["inputs"]
price = data["price"]
cons = data["consensus"]

if cons is not None and cons.target_mean is not None:
    c1, c2, c3, c4 = metric_cells(4)
    c1.metric("Price", f"{price:.2f}" if price else "n/a")
    c2.metric("Analyst target (mean)", f"{cons.target_mean:.2f}",
              _pct(cons.target_upside, signed=True))
    c3.metric("Rating", cons.rating or "n/a")
    c4.metric("Next-FY EPS growth", _pct(cons.eps_growth_next_fy) or "n/a")
    st.caption("Consensus is an analyst aggregate — cross-check before acting.")

if inp0 is None:
    st.error(
        f"No DCF for {ticker}: free cash flow or share count unavailable "
        "(negative/missing FCF is common for early-growth names)."
    )
    st.stop()

base_growth_default = data["base_growth"] if data["base_growth"] is not None else 0.10

# Phones: the sidebar starts collapsed, and sliders there give no live
# feedback (chart hidden while the drawer is open) — render the assumptions
# in the main area instead, right above the results they drive.
if _MOBILE:
    _assumptions = st.expander("Assumptions", icon=":material/tune:", expanded=False)
else:
    _assumptions = st.sidebar
with _assumptions:
    if not _MOBILE:
        st.header("Assumptions")
    growth = st.slider(
        "Base FCF growth", -0.10, 0.50, float(round(base_growth_default, 3)), 0.005,
        help="Yearly free-cash-flow growth you expect over the forecast horizon. "
        "Prefilled from analyst consensus when available.",
    )
    spread = st.slider(
        "Bull/bear spread", 0.0, 0.20, 0.05, 0.005,
        help="Bull = base + spread, bear = base − spread. Widens the scenario range.",
    )
    discount = st.slider(
        "Discount rate", 0.05, 0.20, float(inp0.discount_rate), 0.005,
        help="Your required annual return — future cash is discounted at this rate, "
        "so higher = more conservative fair value. ~8–10% for stable large caps, "
        "12%+ for risky growth.",
    )
    terminal = st.slider(
        "Terminal growth", 0.0, 0.04, float(inp0.terminal_growth), 0.0025,
        help="Growth assumed *forever* after the horizon (Gordon model). Keep at or "
        "below long-run nominal GDP, ~2–3% — nothing outgrows the economy forever.",
    )
    years = st.slider(
        "Forecast horizon (yrs)", 3, 10, int(inp0.years),
        help="Years of explicit FCF projection before the terminal value takes over.",
    )
    use_exit = st.checkbox(
        "Terminal = FCF exit multiple",
        help="Instead of growing FCF forever, value the business at the horizon as "
        "FCF × a multiple — i.e. assume it trades at that multiple when you'd sell.",
    )
    exit_multiple = None
    if use_exit:
        exit_multiple = st.number_input(
            "Exit multiple", 5.0, 60.0, 20.0,
            help="Price/FCF multiple applied to the final-year FCF. 20× ≈ a 5% FCF "
            "yield at the exit.",
        )

if discount <= terminal:
    st.error("Discount rate must exceed terminal growth for a Gordon terminal.")
    st.stop()

inp = DcfInputs(
    fcf0=inp0.fcf0,
    shares=inp0.shares,
    net_cash=inp0.net_cash,
    discount_rate=discount,
    terminal_growth=terminal,
    years=years,
)
summary = summarize(inp, price, growth, spread=spread, exit_multiple=exit_multiple)
results, cases = summary["results"], summary["cases"]

st.subheader("Fair value scenarios")
cols = metric_cells(3)
for col, name in zip(cols, ("bear", "base", "bull"), strict=True):
    r = results[name]
    upside = (r.fair_value / price - 1) if price else None
    col.metric(
        f"{name.title()}  (g={cases[name] * 100:.1f}%)",
        f"{r.fair_value:.2f}",
        f"{upside * 100:+.1f}%" if upside is not None else None,
    )
    col.caption(f"terminal {r.terminal_weight * 100:.0f}% of value")

# Fair value vs price — the at-a-glance over/undervalued read.
scenarios = ("bear", "base", "bull")
detail = []
for n in scenarios:
    r = results[n]
    parts = [f"g = {cases[n] * 100:.1f}%"]
    if price:
        parts.append(f"<b>{(r.fair_value / price - 1) * 100:+.1f}%</b> vs price {price:,.2f}")
    parts.append(f"terminal {r.terminal_weight * 100:.0f}% of value")
    detail.append("<br>".join(parts))
fig = go.Figure()
fig.add_bar(
    x=list(scenarios),
    y=[results[n].fair_value for n in scenarios],
    marker_color=["#d97706", "#2563eb", "#16a34a"],
    name="Fair value",
    customdata=detail,
    hovertemplate=(
        "<b>%{x}</b> · fair value  <b>%{y:,.2f}</b><br>%{customdata}<extra></extra>"
    ),
)
if price:
    fig.add_hline(y=price, line_dash="dash", annotation_text=f"price {price:.2f}")
# t=30: the price hline pins the range top when the name trades above every
# scenario, pushing its annotation into the top margin — keep room for it.
fig.update_layout(height=320, margin=dict(t=30, b=10), yaxis_title="Per-share value")
show_chart(fig)

if price:
    weighted = summary["weighted"]
    mos = summary["mos"]
    implied = summary["implied"]

    st.subheader("Read-through")
    r1, r2, r3 = metric_cells(3, width=150)
    r1.metric("Prob-weighted fair value (25/50/25)", f"{weighted['fair_value']:.2f}",
              f"{weighted['total'] * 100:+.1f}%",
              help="Bear/base/bull fair values blended 25% / 50% / 25%.")
    r2.metric("Margin of safety (base)", f"{mos * 100:+.1f}%",
              help="Base fair value vs today's price — your cushion for being wrong. "
              "Positive = priced below your base case.")
    r3.metric("Price implies FCF growth (reverse-DCF)",
              f"{implied * 100:.1f}%" if implied is not None else "out of range",
              help="The model run backwards: the constant FCF growth needed to justify "
              "today's price with these discount/terminal settings.")
    if implied is not None:
        verdict = "demanding" if implied > growth else "beatable at your base"
        st.caption(f"Market prices in {implied * 100:.1f}% constant FCF growth vs your "
                   f"{growth * 100:.1f}% base — {verdict}.")

st.caption(
    f"FCF0 {inp.fcf0 / 1e9:,.1f}B · shares {inp.shares / 1e9:,.2f}B · "
    f"net cash {inp.net_cash / 1e9:,.1f}B. Derived scaffold, not advice."
)
