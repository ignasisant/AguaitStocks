"""Profile page — account identity, preferences and the personal watchlist.

Everything here is per-account (see stocks.web.auth): the watchlist editor
writes this user's watchlist.yaml, preferences go to their prefs.json. Alert
rules and broker aliases stay YAML-only; saving the editor preserves them.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from stocks.config import load_watchlist
from stocks.web import auth, widgets

# Account identity, prefs and the watchlist editor are all per-account.
auth.require_login()

st.title("Profile")

paths = auth.user_paths()

# ------------------------------------------------------------------ account
with st.container(horizontal=True, vertical_alignment="center"):
    picture = getattr(st.user, "picture", None)
    if picture:
        st.image(picture, width=56)
    name = getattr(st.user, "name", None) or st.user.email
    st.markdown(f"**{name}**  \n{st.user.email}")
    st.button("Log out", icon=":material/logout:", on_click=st.logout)

st.caption(
    "Your watchlist, portfolio ledger and preferences are attached to this "
    "account — other users of this app never see them. "
    f"Data folder: `{paths.root}`."
)

st.divider()

# -------------------------------------------------------------- preferences
st.subheader("Preferences")

prefs = auth.load_prefs()
ccy = st.segmented_control(
    "Display currency",
    auth.CURRENCIES,
    default=prefs.get("currency", "EUR"),
    key="pref_currency",
)
if ccy and ccy != prefs.get("currency"):
    prefs["currency"] = ccy
    auth.save_prefs(prefs)
    st.toast(f"Display currency set to {ccy}", icon=":material/check:")
st.caption(
    "Converts the Portfolio headline figures at the latest FX rate. Tax "
    "figures stay in EUR — Spanish fiscal rules are euro-denominated."
)

st.divider()

# ---------------------------------------------------------------- watchlist
st.subheader("Watchlist")
st.caption(
    "Drives Overview, Screener, Earnings and the ticker picker. Add or remove "
    "rows and hit **Save**. `shares` + `cost` make an entry a position for the "
    "equal-weight fallback analytics; the real book still comes from imported "
    "transactions. Alert rules on kept tickers are preserved. Crypto goes in "
    "as a Yahoo pair symbol — `BTC-USD`, `ETH-EUR` — never a bare coin code "
    "(bare codes collide with stock tickers)."
)

holdings = load_watchlist(paths.watchlist)
if not holdings:
    st.info(
        "Watchlist is empty — add a row below (**+** at the bottom of the table), "
        "type a ticker like `AAPL`, and hit **Save**. Overview, Screener and "
        "Earnings come alive as soon as one ticker is in."
    )
frame = pd.DataFrame(
    [
        {
            "logo": widgets.logo(h.ticker),
            "ticker": h.ticker,
            "name": h.name,
            "favorite": h.favorite,
            "shares": h.shares or None,
            "cost": h.cost,
            "tags": ", ".join(h.tags),
        }
        for h in holdings
    ],
    columns=["logo", "ticker", "name", "favorite", "shares", "cost", "tags"],
)
edited = st.data_editor(
    frame,
    num_rows="dynamic",
    hide_index=True,
    key="watchlist_editor",
    disabled=("logo",),
    column_config={
        "logo": st.column_config.ImageColumn("", width=40),
        "ticker": st.column_config.TextColumn("Ticker", required=True, max_chars=12),
        "name": st.column_config.TextColumn("Name"),
        "favorite": st.column_config.CheckboxColumn("Favorite", default=False),
        "shares": st.column_config.NumberColumn("Shares", min_value=0.0),
        "cost": st.column_config.NumberColumn(
            "Avg cost",
            min_value=0.0,
            help="Average buy price per share, in the stock's native currency",
        ),
        "tags": st.column_config.TextColumn(
            "Tags",
            help="Comma-separated groups, e.g. `semis, EM` — the ticker "
            "picker search matches them",
        ),
    },
)

if st.button("Save watchlist", type="primary", icon=":material/save:"):
    entries = [
        {
            "ticker": row.get("ticker"),
            "name": None if pd.isna(row.get("name")) else row.get("name"),
            "favorite": bool(row.get("favorite"))
            and not pd.isna(row.get("favorite")),
            "shares": None if pd.isna(row.get("shares")) else row.get("shares"),
            "cost": None if pd.isna(row.get("cost")) else row.get("cost"),
            "tags": (
                []
                if pd.isna(row.get("tags"))
                else [s for s in str(row.get("tags")).split(",")]
            ),
        }
        for row in edited.to_dict("records")
    ]
    auth.save_watchlist_entries(entries, paths.watchlist)
    saved = load_watchlist(paths.watchlist)
    st.success(f"Saved — watchlist now has {len(saved)} entries.")
