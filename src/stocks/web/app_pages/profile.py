"""Profile page — account identity, preferences and the personal watchlist.

Everything here is per-account (see stocks.web.auth): the watchlist editor
writes this user's watchlist.yaml, preferences go to their prefs.json. Alert
rules and broker aliases stay YAML-only; saving the editor preserves them.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from stocks.config import load_watchlist
from stocks.web import auth, i18n, widgets
from stocks.web.i18n import t as tr

# Account identity, prefs and the watchlist editor are all per-account.
auth.require_login()

st.title(tr("nav.profile"))

paths = auth.user_paths()

# ------------------------------------------------------------------ account
with st.container(horizontal=True, vertical_alignment="center"):
    picture = getattr(st.user, "picture", None)
    if picture:
        st.image(picture, width=56)
    name = getattr(st.user, "name", None) or st.user.email
    st.markdown(f"**{name}**  \n{st.user.email}")
    st.button(tr("common.log_out"), icon=":material/logout:", on_click=st.logout)

st.caption(tr("profile.account_caption", folder=paths.root))

st.divider()

# -------------------------------------------------------------- preferences
st.subheader(tr("profile.preferences"))

prefs = auth.load_prefs()

# Language: "auto" follows the browser locale (st.context.locale); an explicit
# pick is stored and wins over the browser on every page (see i18n).
_AUTO = "auto"
lang_opts = [_AUTO, *i18n.LANGUAGES]
current_lang = prefs.get("language") or _AUTO


def _lang_label(code: str) -> str:
    return tr("profile.lang_auto") if code == _AUTO else i18n.LANGUAGES[code]


lang = st.selectbox(
    tr("profile.language"),
    lang_opts,
    index=lang_opts.index(current_lang if current_lang in lang_opts else _AUTO),
    format_func=_lang_label,
    key="pref_language",
)
_lang_val = None if lang == _AUTO else lang
if _lang_val != prefs.get("language"):
    prefs["language"] = _lang_val
    auth.save_prefs(prefs)
    st.rerun()  # re-run so app.py re-resolves the language for the whole app
st.caption(tr("profile.language_caption"))

ccy = st.segmented_control(
    tr("profile.display_currency"),
    auth.CURRENCIES,
    default=prefs.get("currency", "EUR"),
    key="pref_currency",
)
if ccy and ccy != prefs.get("currency"):
    prefs["currency"] = ccy
    auth.save_prefs(prefs)
    st.toast(tr("profile.currency_set", ccy=ccy), icon=":material/check:")
st.caption(tr("profile.currency_caption"))

st.divider()

# ---------------------------------------------------------------- watchlist
st.subheader(tr("profile.watchlist"))
st.caption(tr("profile.watchlist_caption"))

holdings = load_watchlist(paths.watchlist)
if not holdings:
    st.info(tr("profile.watchlist_empty"))
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
        "ticker": st.column_config.TextColumn(
            tr("profile.col_ticker"), required=True, max_chars=12
        ),
        "name": st.column_config.TextColumn(tr("profile.col_name")),
        "favorite": st.column_config.CheckboxColumn(
            tr("profile.col_favorite"), default=False
        ),
        "shares": st.column_config.NumberColumn(tr("profile.col_shares"), min_value=0.0),
        "cost": st.column_config.NumberColumn(
            tr("profile.col_avg_cost"),
            min_value=0.0,
            help=tr("profile.col_avg_cost_help"),
        ),
        "tags": st.column_config.TextColumn(
            tr("profile.col_tags"),
            help=tr("profile.col_tags_help"),
        ),
    },
)

if st.button(tr("profile.save_watchlist"), type="primary", icon=":material/save:"):
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
    st.success(tr("profile.saved", n=len(saved)))
