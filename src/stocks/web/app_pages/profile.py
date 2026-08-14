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

prefs = auth.load_prefs()

# ------------------------------------------------------------- scope tabs
# One tab per profile scope. Default st.tabs semantics (every tab renders on
# every run) are intentional: the watchlist editor keeps unsaved edits across
# tab switches and the Telegram polling fragment keeps polling while another
# tab is in front.
tab_prefs, tab_iv, tab_watch, tab_notify = st.tabs(
    [
        f":material/tune: {tr('profile.preferences')}",
        f":material/person: {tr('profile.iv_section')}",
        f":material/format_list_bulleted: {tr('profile.watchlist')}",
        f":material/notifications: {tr('profile.notifications')}",
    ]
)

# -------------------------------------------------------------- preferences
with tab_prefs:
    # Language: "auto" follows the browser locale (st.context.locale); an
    # explicit pick is stored and wins over the browser on every page (i18n).
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

# --------------------------------------------------------- investor profile
# What the AI assistant is told about the user. Same form as the first-login
# dialog (auth.render_profile_form); chat_core reads it to build the persona.
with tab_iv:
    st.caption(tr("profile.iv_caption"))
    _profile = auth.render_profile_form("iv_page")
    if st.button(
        tr("profile.iv_save"), type="primary", icon=":material/save:", key="iv_page_save"
    ):
        auth.save_profile(_profile)
        st.toast(tr("profile.iv_saved"), icon=":material/check:")

# ---------------------------------------------------------------- watchlist
with tab_watch:
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
            "shares": st.column_config.NumberColumn(
                tr("profile.col_shares"), min_value=0.0
            ),
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

# ------------------------------------------------------------ notifications
# Telegram linking + digest/alert toggles. The linking flow: a one-time code
# scoped to this session, a t.me deep link, and a polling fragment that
# watches getUpdates for "/start <code>" — see notify/telegram.py. The cron
# (notify/fanout.py, GitHub Actions) reads the resulting prefs headless.
with tab_notify:
    st.caption(tr("profile.notify_caption"))

    from stocks.notify import telegram as _tg  # noqa: E402

    if not _tg.configured():
        st.info(tr("profile.tg_not_configured"), icon=":material/notifications_off:")
    elif prefs.get("telegram_chat_id"):
        handle = prefs.get("telegram_username") or ""
        st.markdown(
            f":green-badge[:material/check: Telegram] "
            f"{tr('profile.tg_linked_as', handle=f'@{handle}' if handle else '')}"
        )
        for pref_key, label_key in (
            ("notify_digest", "profile.notify_digest"),
            ("notify_alerts", "profile.notify_alerts"),
        ):
            val = st.toggle(
                tr(label_key),
                value=bool(prefs.get(pref_key, True)),
                key=f"pref_{pref_key}",
            )
            if val != bool(prefs.get(pref_key, True)):
                prefs[pref_key] = val
                auth.save_prefs(prefs)
                st.toast(tr(label_key), icon=":material/check:")

        with st.container(horizontal=True):
            if st.button(tr("profile.tg_test"), icon=":material/send:"):
                try:
                    _tg.send_message(
                        i18n.translate("notify.test_message", i18n.active_language()),
                        prefs["telegram_chat_id"],
                        parse_mode=None,
                    )
                    st.toast(tr("profile.tg_test_sent"), icon=":material/check:")
                except Exception as exc:  # noqa: BLE001 — surface, don't crash the page
                    st.toast(
                        tr("profile.tg_test_failed", error=exc), icon=":material/error:"
                    )
            if st.button(tr("profile.tg_unlink"), icon=":material/link_off:"):
                for k in ("telegram_chat_id", "telegram_username", "telegram_linked_at"):
                    prefs.pop(k, None)
                prefs["telegram_chat_id"] = None
                auth.save_prefs(prefs)
                st.toast(tr("profile.tg_unlinked"), icon=":material/link_off:")
                st.rerun()
    else:
        import secrets as _secrets
        import time as _time

        _LINK_TTL = 600  # seconds a pending link code stays valid

        link = st.session_state.get("tg_link")
        if link and _time.time() - link["ts"] > _LINK_TTL:
            link = None
            st.session_state.pop("tg_link", None)
            st.warning(tr("profile.tg_expired"))

        if link is None:
            if st.button(
                tr("profile.tg_connect"), type="primary", icon=":material/send:"
            ):
                # Random, session-scoped one-time code: an attacker sending
                # "/start <guess>" from their own Telegram can only ever match
                # their own session. Never derive this from the email.
                st.session_state["tg_link"] = {
                    "code": _secrets.token_urlsafe(12),
                    "ts": _time.time(),
                }
                st.rerun()
        else:
            st.link_button(
                tr("profile.tg_open"),
                _tg.deep_link(link["code"]),
                type="primary",
                icon=":material/open_in_new:",
            )

            @st.fragment(run_every="3s")
            def _tg_poll() -> None:
                pending = st.session_state.get("tg_link")
                if not pending or _time.time() - pending["ts"] > _LINK_TTL:
                    st.rerun(scope="app")  # expired mid-poll: let the page re-gate
                    return
                chat = _tg.match_start(_tg.get_updates(), pending["code"])
                if chat is None:
                    st.caption(f":material/hourglass_top: {tr('profile.tg_waiting')}")
                    return
                fresh = auth.load_prefs()
                fresh["telegram_chat_id"] = chat["id"]
                fresh["telegram_username"] = chat.get("username") or ""
                fresh["telegram_linked_at"] = int(_time.time())
                auth.save_prefs(fresh)
                st.session_state.pop("tg_link", None)
                try:
                    _tg.send_message(
                        i18n.translate(
                            "notify.linked_ok", i18n.active_language(),
                            email=st.user.email,
                        ),
                        chat["id"],
                        parse_mode=None,
                    )
                except Exception:  # noqa: BLE001 — linking succeeded regardless
                    pass
                st.rerun(scope="app")

            _tg_poll()
