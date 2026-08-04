"""Streamlit entry point — st.navigation over the app_pages/ modules.

Run: uv run stocks dashboard   (or: uv run streamlit run src/stocks/web/app.py)

Page config, the dense-layout CSS and the nav are defined once here; the page
modules under app_pages/ carry only their own content. Colors and fonts live
in .streamlit/config.toml — the CSS below is only spacing the theme can't
express.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit Community Cloud runs this file straight from the repo checkout;
# make src/ importable there (and pin imports to the source tree locally).
_SRC = str(Path(__file__).resolve().parents[2])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import streamlit as st  # noqa: E402

from stocks.web import auth  # noqa: E402
from stocks.web.widgets import ticker_picker  # noqa: E402

st.set_page_config(page_title="Stocks", layout="wide")

# Dense layout: kill Streamlit's default top padding and wide element gaps so
# charts and metrics sit high and tight instead of floating in whitespace.
st.html(
    """
    <style>
      .block-container {padding-top: 1.2rem; padding-bottom: 1rem;
                        padding-left: 2.5rem; padding-right: 2.5rem; max-width: 100%;}
      /* Desktop only: reclaim the header strip. On phones the header must
         survive — it carries the sidebar/nav toggle, and the sidebar starts
         collapsed there. */
      @media (min-width: 641px) {
        header[data-testid="stHeader"] {height: 0; background: transparent;}
      }
      @media (max-width: 640px) {
        .block-container {padding-left: 0.75rem; padding-right: 0.75rem;}
        [data-testid="stMetricLabel"] p {font-size: 0.8rem;}
        [data-testid="stCaptionContainer"] p {font-size: 0.8rem;}
      }
      [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] {gap: 0.4rem;}
      [data-testid="stMetric"] {padding: 0;}
      [data-testid="stMetricValue"] {font-size: 1.15rem; line-height: 1.1;}
      [data-testid="stMetricLabel"] p {font-size: 0.72rem;}
      [data-testid="stMetricDelta"] {font-size: 0.8rem;}
      [data-testid="stCaptionContainer"] p {font-size: 0.75rem; margin-bottom: 0;}
      h1 {font-size: 1.7rem; padding: 0.2rem 0;}
      h2, h3 {padding: 0.2rem 0; margin-top: 0.3rem;}
      hr {margin: 0.4rem 0;}
      [data-testid="stElementToolbar"] {display: none;}
    </style>
    """
)

# Browsing is public: anonymous visitors get the shared read-only guest
# watchlist. resolve_user() puts the session's data paths (watchlist, ledger,
# prefs) in session state; the Portfolio/Import/Profile pages and mutating
# widgets gate themselves with require_login()/is_logged_in().
auth.resolve_user()

ticker_page = st.Page(
    "app_pages/ticker.py",
    title="Ticker",
    icon=":material/query_stats:",
    url_path="ticker",
)

page = st.navigation(
    [
        st.Page(
            "app_pages/home.py",
            title="Home",
            icon=":material/home:",
            default=True,
        ),
        ticker_page,
        st.Page("app_pages/portfolio.py", title="Portfolio", icon=":material/pie_chart:"),
        st.Page("app_pages/screener.py", title="Screener", icon=":material/filter_alt:"),
        st.Page("app_pages/earnings.py", title="Earnings", icon=":material/calendar_month:"),
        st.Page("app_pages/valuation.py", title="Valuation", icon=":material/calculate:"),
        st.Page(
            "app_pages/import_transactions.py",
            title="Import",
            icon=":material/upload_file:",
        ),
        st.Page(
            "app_pages/profile.py",
            title="Profile",
            icon=":material/account_circle:",
        ),
    ]
)

# Anonymous visitors get a sign-in entry point on every page; the gated
# pages (Portfolio, Import, Profile) render a full login screen themselves.
if "auth" in st.secrets and not auth.is_logged_in():
    st.sidebar.button(
        "Sign in with Google",
        icon=":material/login:",
        on_click=st.login,
        width="stretch",
    )

# Deep link: ?ticker=SYM selects that symbol (applied once per new URL value,
# so it doesn't fight the picker). Away from the Ticker page it also jumps
# there — keeps pre-refactor /?ticker= bookmarks and table links working.
_qp = (st.query_params.get("ticker") or "").strip().upper()
if _qp and st.session_state.get("_url_ticker") != _qp:
    st.session_state["picker_selected"] = _qp
    st.session_state["_url_ticker"] = _qp
    if page.url_path != ticker_page.url_path:
        st.switch_page(ticker_page)

# The ticker picker lives here, above page.run(), so every page carries it:
# sidebar searchbar + watchlist on desktop, popover on phones. Clicking any
# ticker row navigates to the Ticker page (the picker's on_click sets the
# flag); on the Ticker page itself the rerun just redraws the selection.
ticker_picker(key="nav")
_clicked = st.session_state.pop("picker_clicked", False)
if _clicked and page.url_path != ticker_page.url_path:
    st.switch_page(ticker_page)

page.run()
