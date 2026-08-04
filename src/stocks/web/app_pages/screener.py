"""Watchlist screener page — rank and filter the whole watchlist by KPIs.

Cross-sectional view of the same fundamentals the per-ticker page computes:
cheap P/E, high ROIC, high FCF yield, low leverage, across every name.
"""

from __future__ import annotations

import streamlit as st

from stocks.analysis.fundamentals import KPI_SOURCES
from stocks.analysis.screener import (
    DEFAULT_COLUMNS,
    LOWER_IS_BETTER,
    Filter,
    apply_filters,
    fetch_metrics_many,
    format_frame,
    metrics_frame,
    rank,
)
from stocks.config import tickers as watchlist_tickers
from stocks.web import auth
from stocks.web.widgets import is_mobile, ticker_table_html

_MOBILE = is_mobile()

st.title("Watchlist screener")

from stocks.data.crypto import is_crypto

all_tickers = watchlist_tickers(auth.watchlist_path())
# Crypto has no fundamentals — pairs would only add all-NaN rows here.
tickers = [t for t in all_tickers if not is_crypto(t)]
if not all_tickers:
    st.warning("Watchlist empty. Add tickers on the **Profile** page.")
    st.stop()
if not tickers:
    st.info("Only crypto on the watchlist — the KPI screener needs stocks.")
    st.stop()
if len(tickers) < len(all_tickers):
    st.caption(
        f"{len(all_tickers) - len(tickers)} crypto asset(s) excluded — "
        "no fundamentals to screen."
    )


@st.cache_data(ttl=3600, show_spinner="Loading fundamentals for the whole watchlist…")
def _frame(sig: tuple):
    # `sig` (the ticker tuple) is part of the cache key on purpose: editing
    # the watchlist must invalidate the frame, not wait out the TTL.
    return metrics_frame(fetch_metrics_many(list(sig)))


df = _frame(tuple(tickers))

label = {k: KPI_SOURCES[k].label for k in df.columns}
options = list(df.columns)

# Phones: the sidebar starts collapsed — put the controls in the main area,
# folded into an expander above the results they filter.
if _MOBILE:
    _controls = st.expander("Screen & filters", icon=":material/tune:")
else:
    _controls = st.sidebar

# Fewer default columns on phones: the full set forces horizontal panning
# inside the table; more are one multiselect tap away.
default_cols = [c for c in DEFAULT_COLUMNS if c in options]
if _MOBILE:
    default_cols = default_cols[:4]

with _controls:
    if not _MOBILE:
        st.header("Screen")
    sort_by = st.selectbox(
        "Rank by",
        options,
        index=options.index("roic") if "roic" in options else 0,
        format_func=lambda k: label.get(k, k),
    )
    if KPI_SOURCES[sort_by].desc:
        st.caption(KPI_SOURCES[sort_by].desc)
    ascending = st.checkbox("Ascending", value=sort_by in LOWER_IS_BETTER)
    columns = st.multiselect(
        "Columns",
        options,
        default=default_cols,
        format_func=lambda k: label.get(k, k),
    )
    st.divider()
    st.caption("Filters (percent metrics are fractions: 0.15 = 15%)")
    filters: list[Filter] = []
    for key, kind, default in (
        ("pe_ttm", "max", 40.0),
        ("roic", "min", 0.15),
        ("fcf_yield", "min", 0.03),
    ):
        if key not in options:
            continue
        if st.checkbox(
            f"{label[key]} {'≤' if kind == 'max' else '≥'}",
            key=f"chk_{key}",
            help=KPI_SOURCES[key].desc or None,
        ):
            val = st.number_input(label[key], value=default, key=f"val_{key}")
            filters.append(Filter(key, kind, val))

view = apply_filters(df, filters)
view = rank(view, sort_by, ascending=ascending)
if columns:
    view = view[[c for c in columns if c in view.columns]]

st.caption(f"{len(view)} of {len(df)} tickers pass — re-sort with **Rank by**.")
# Shared Positions-style table (logo + company-name ticker cells). Values are
# pre-formatted strings; sorting stays with the Rank-by control above.
disp = format_frame(view).rename(columns=label)
disp.insert(0, "ticker", disp.index)
st.html(ticker_table_html(disp))
st.download_button(
    "Download raw numbers (CSV)", df.to_csv().encode(), "screen.csv", "text/csv",
    icon=":material/download:",
)

with st.expander("What do these metrics mean?", icon=":material/help:"):
    for k in options:
        src = KPI_SOURCES[k]
        if src.desc:
            st.markdown(f"**{src.label}** — {src.desc}")
