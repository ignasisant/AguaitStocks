"""Watchlist screener page — rank and filter the whole watchlist by KPIs.

Cross-sectional view of the same fundamentals the per-ticker page computes:
cheap P/E, high ROIC, high FCF yield, low leverage, across every name.
"""

from __future__ import annotations

import streamlit as st

from stocks.web.kpi_text import kpi_desc, kpi_label
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
from stocks.web.i18n import t as tr
from stocks.web.widgets import is_mobile, ticker_table_html

_MOBILE = is_mobile()

st.title(tr("screener.title"))

from stocks.data.crypto import is_crypto

all_tickers = watchlist_tickers(auth.watchlist_path())
# Crypto has no fundamentals — pairs would only add all-NaN rows here.
tickers = [t for t in all_tickers if not is_crypto(t)]
if not all_tickers:
    st.warning(tr("screener.watchlist_empty"))
    st.stop()
if not tickers:
    st.info(tr("screener.only_crypto"))
    st.stop()
if len(tickers) < len(all_tickers):
    st.caption(tr("screener.crypto_excluded", n=len(all_tickers) - len(tickers)))


@st.cache_data(ttl=3600, show_spinner=tr("screener.loading_fundamentals"))
def _frame(sig: tuple):
    # `sig` (the ticker tuple) is part of the cache key on purpose: editing
    # the watchlist must invalidate the frame, not wait out the TTL.
    return metrics_frame(fetch_metrics_many(list(sig)))


df = _frame(tuple(tickers))

label = {k: kpi_label(k) for k in df.columns}
options = list(df.columns)

# Phones: the sidebar starts collapsed — put the controls in the main area,
# folded into an expander above the results they filter.
if _MOBILE:
    _controls = st.expander(tr("screener.screen_filters"), icon=":material/tune:")
else:
    _controls = st.sidebar

# Fewer default columns on phones: the full set forces horizontal panning
# inside the table; more are one multiselect tap away.
default_cols = [c for c in DEFAULT_COLUMNS if c in options]
if _MOBILE:
    default_cols = default_cols[:4]

with _controls:
    if not _MOBILE:
        st.header(tr("screener.screen"))
    sort_by = st.selectbox(
        tr("screener.rank_by"),
        options,
        index=options.index("roic") if "roic" in options else 0,
        format_func=lambda k: label.get(k, k),
    )
    if kpi_desc(sort_by):
        st.caption(kpi_desc(sort_by))
    ascending = st.checkbox(tr("screener.ascending"), value=sort_by in LOWER_IS_BETTER)
    columns = st.multiselect(
        tr("screener.columns"),
        options,
        default=default_cols,
        format_func=lambda k: label.get(k, k),
    )
    st.divider()
    st.caption(tr("screener.filters_caption"))
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
            help=kpi_desc(key) or None,
        ):
            val = st.number_input(label[key], value=default, key=f"val_{key}")
            filters.append(Filter(key, kind, val))

view = apply_filters(df, filters)
view = rank(view, sort_by, ascending=ascending)
if columns:
    view = view[[c for c in columns if c in view.columns]]

st.caption(tr("screener.pass_caption", n=len(view), total=len(df)))
# Shared Positions-style table (logo + company-name ticker cells). Values are
# pre-formatted strings; sorting stays with the Rank-by control above.
disp = format_frame(view).rename(columns=label)
disp.insert(0, "ticker", disp.index)
st.html(ticker_table_html(disp))
st.download_button(
    tr("screener.download_csv"), df.to_csv().encode(), "screen.csv", "text/csv",
    icon=":material/download:",
)

with st.expander(tr("screener.metrics_help"), icon=":material/help:"):
    for k in options:
        if kpi_desc(k):
            st.markdown(f"**{kpi_label(k)}** — {kpi_desc(k)}")
