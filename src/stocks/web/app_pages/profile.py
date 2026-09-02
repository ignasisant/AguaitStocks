"""Profile page — account identity, preferences and the personal watchlist.

Everything here is per-account (see stocks.web.auth): the watchlist editor
writes this user's watchlist.yaml, preferences go to their prefs.json. Alert
rules and broker aliases stay YAML-only; saving the editor preserves them.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from stocks import storage
from stocks.config import load_watchlist
from stocks.portfolio import tax
from stocks.portfolio.tax import de as tax_de
from stocks.web import auth, i18n, tax_ui, widgets
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
# Deep-linking: another page (e.g. the Home setup card) can request a tab by
# setting session "profile_tab" to a _TAB_LABELS id before st.switch_page.
# One-shot — popped into the tabs widget's own keyed state, which then owns
# the selection (so reruns and manual tab clicks behave normally).
_TAB_LABELS = {
    "prefs": f":material/tune: {tr('profile.preferences')}",
    "iv": f":material/person: {tr('profile.iv_section')}",
    "watch": f":material/format_list_bulleted: {tr('profile.watchlist')}",
    "notify": f":material/notifications: {tr('profile.notifications')}",
}
_want_tab = st.session_state.pop("profile_tab", None)
if _want_tab in _TAB_LABELS:
    st.session_state["profile_tabs"] = _TAB_LABELS[_want_tab]
# on_change="rerun" makes the tabs a real keyed widget (session-settable);
# every tab still renders every run — no .open gating — so the watchlist
# editor and the polling fragment keep their existing behavior, at the cost
# of one rerun per manual tab switch.
tab_prefs, tab_iv, tab_watch, tab_notify = st.tabs(
    list(_TAB_LABELS.values()), key="profile_tabs", on_change="rerun"
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

    # ---------------------------------------------------------- tax residence
    # Which country's rules the Realized & tax tab applies. "auto" reads the
    # region off the browser locale (en-US -> US) and lands on Spain when it
    # recognizes nothing — the ledger has to be taxed under some set of rules.
    # The bracket inputs below only render for jurisdictions that read them;
    # Spain's savings base doesn't care about filing status or other income.
    st.subheader(tr("profile.tax_section"))
    _res_opts = [tax_ui.AUTO, *tax.codes()]
    _res_current = prefs.get(tax_ui.PREF_RESIDENCE) or tax_ui.AUTO

    def _res_label(code: str) -> str:
        return (
            tr("profile.tax_residence_auto")
            if code == tax_ui.AUTO
            else tax_ui.label(code)
        )

    residence = st.selectbox(
        tr("profile.tax_residence"),
        _res_opts,
        index=_res_opts.index(
            _res_current if _res_current in _res_opts else tax_ui.AUTO),
        format_func=_res_label,
        key="pref_tax_residence",
    )
    _res_val = None if residence == tax_ui.AUTO else residence
    if _res_val != prefs.get(tax_ui.PREF_RESIDENCE):
        prefs[tax_ui.PREF_RESIDENCE] = _res_val
        auth.save_prefs(prefs)
        st.toast(
            tr("profile.tax_set", label=_res_label(residence)),
            icon=":material/check:",
        )
        st.rerun()  # the tab's rules, currency and wording all change with it
    st.caption(tr("profile.tax_residence_caption"))

    # Only the knobs the active jurisdiction actually reads: Spain's savings
    # base has no filing status and no bracket to stack income on, so an ES
    # account sees nothing below. The order is the jurisdiction's.
    _active = tax.get(tax_ui.resolve_code(prefs))
    _fields = _active.settings_fields
    if "filing_status" in _fields:
        _statuses = list(_active.filing_statuses)
        _status_current = prefs.get(tax_ui.PREF_FILING_STATUS) or _statuses[0]
        status = st.selectbox(
            tr("profile.tax_filing_status"),
            _statuses,
            index=_statuses.index(
                _status_current if _status_current in _statuses else _statuses[0]),
            format_func=lambda c: tr(f"profile.tax_status_{c}"),
            key="pref_tax_filing_status",
        )
        if status != prefs.get(tax_ui.PREF_FILING_STATUS):
            prefs[tax_ui.PREF_FILING_STATUS] = status
            auth.save_prefs(prefs)
        st.caption(
            tr(f"profile.tax_filing_status_caption_{_active.code.lower()}")
        )

    if "church_tax_rate" in _fields:
        # Kirchensteuer: 8% of the tax in Bavaria and Baden-Württemberg, 9%
        # in the other states, nothing if the filer is not church-registered.
        _rates = list(tax_de.CHURCH_TAX_RATES)
        try:
            _rate_now = float(prefs.get(tax_ui.PREF_CHURCH_TAX) or 0.0)
        except (TypeError, ValueError):
            _rate_now = 0.0
        church = st.selectbox(
            tr("profile.tax_church"),
            _rates,
            index=_rates.index(_rate_now if _rate_now in _rates else 0.0),
            format_func=lambda r: tr(f"profile.tax_church_{int(r * 100)}"),
            key="pref_tax_church",
        )
        if float(church) != _rate_now:
            prefs[tax_ui.PREF_CHURCH_TAX] = float(church)
            auth.save_prefs(prefs)
        st.caption(tr("profile.tax_church_caption"))

    if "other_income" in _fields:
        income = st.number_input(
            tr("profile.tax_other_income"),
            min_value=0.0,
            step=1_000.0,
            value=float(prefs.get(tax_ui.PREF_OTHER_INCOME) or 0.0),
            key="pref_tax_other_income",
        )
        if float(income) != float(prefs.get(tax_ui.PREF_OTHER_INCOME) or 0.0):
            prefs[tax_ui.PREF_OTHER_INCOME] = float(income)
            auth.save_prefs(prefs)
        st.caption(
            tr(f"profile.tax_other_income_caption_{_active.code.lower()}")
        )

    if "include_niit" in _fields:
        niit = st.toggle(
            tr("profile.tax_niit"),
            value=bool(prefs.get(tax_ui.PREF_NIIT)),
            key="pref_tax_niit",
        )
        if niit != bool(prefs.get(tax_ui.PREF_NIIT)):
            prefs[tax_ui.PREF_NIIT] = niit
            auth.save_prefs(prefs)
        st.caption(tr("profile.tax_niit_caption"))

    # ------------------------------------------------------------ danger zone
    # The deletion path the privacy policy promises. Owner account never gets
    # the control: its "data dir" is the repo root (auth.delete_account would
    # refuse it anyway).
    if paths.root != auth.PROJECT_ROOT:
        st.space("large")
        with st.expander(f":material/delete_forever: {tr('profile.delete_title')}"):
            st.markdown(tr("profile.delete_body"))
            sure = st.checkbox(tr("profile.delete_confirm"), key="delete_sure")
            if st.button(
                tr("profile.delete_button"),
                type="primary",
                icon=":material/delete_forever:",
                disabled=not sure,
                key="delete_account",
            ):
                try:
                    auth.delete_account(paths)
                except Exception:
                    st.error(tr("profile.delete_failed"), icon=":material/error:")
                else:
                    st.logout()

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
    # The grid stays editable on phones — a read-only card list can't add or
    # retag a holding — but seven columns pan, so the two that carry no edit
    # (the logo) or repeat the symbol (the name) drop out of the view there.
    # Hidden columns still come back in `edited`, so saving is unaffected.
    edited = st.data_editor(
        frame,
        num_rows="dynamic",
        hide_index=True,
        key="watchlist_editor",
        disabled=("logo",),
        column_order=(
            ("ticker", "favorite", "shares", "cost", "tags")
            if widgets.is_mobile()
            else None
        ),
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
# scoped to this session, written into prefs (mirrored to the bucket) and
# carried by a t.me deep link; the user's "/start <code>" arrives through the
# webhook queue, the Actions chat job (stocks/chat/bot.py) matches it and
# writes telegram_chat_id back into prefs, and the polling fragment below
# watches its own prefs until the id appears. The crons (notify/fanout.py)
# read the resulting prefs headless.
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

        st.markdown(tr("profile.tg_how_body"))

        link = st.session_state.get("tg_link")
        if link and _time.time() - link["ts"] > _LINK_TTL:
            link = None
            st.session_state.pop("tg_link", None)
            # Retire the code server-side too — a stale code must not stay
            # matchable in prefs after the page stopped waiting for it.
            fresh = auth.load_prefs()
            if fresh.pop("tg_link_code", None) is not None:
                fresh.pop("tg_link_ts", None)
                auth.save_prefs(fresh)
            st.warning(tr("profile.tg_expired"))

        if link is None:
            st.space("small")
            if st.button(
                tr("profile.tg_connect"), type="primary", icon=":material/send:"
            ):
                # Random, session-scoped one-time code: an attacker sending
                # "/start <guess>" from their own Telegram can only ever match
                # their own session. Never derive this from the email.
                code = _secrets.token_urlsafe(12)
                st.session_state["tg_link"] = {"code": code, "ts": _time.time()}
                # The code goes into prefs (mirrored to the bucket) so the
                # Actions chat job can match the incoming "/start <code>".
                fresh = auth.load_prefs()
                fresh["tg_link_code"] = code
                fresh["tg_link_ts"] = int(_time.time())
                auth.save_prefs(fresh)
                st.rerun()
        else:
            st.space("small")
            st.link_button(
                tr("profile.tg_open"),
                _tg.deep_link(link["code"]),
                type="primary",
                icon=":material/open_in_new:",
            )
            st.caption(
                tr(
                    "profile.tg_manual",
                    bot=f"@{_tg.bot_username()}",
                    code=link["code"],
                )
            )
            st.space("small")

            @st.fragment(run_every="3s")
            def _tg_poll() -> None:
                # The "/start <code>" arrives via the webhook queue; the
                # Actions chat job matches it and writes telegram_chat_id
                # into this account's prefs.json in the bucket. Poll our own
                # prefs (bucket first) until the id appears — the job also
                # sends the in-chat confirmation.
                pending = st.session_state.get("tg_link")
                if not pending or _time.time() - pending["ts"] > _LINK_TTL:
                    st.rerun(scope="app")  # expired mid-poll: let the page re-gate
                    return
                try:
                    if storage.enabled():
                        storage.restore(paths.prefs)
                except Exception:  # noqa: BLE001 — transient; next 3s tick retries
                    st.caption(f":material/error: {tr('profile.tg_poll_error')}")
                    return
                fresh = auth.load_prefs()
                if not fresh.get("telegram_chat_id"):
                    st.caption(f":material/hourglass_top: {tr('profile.tg_waiting')}")
                    return
                st.session_state.pop("tg_link", None)
                st.rerun(scope="app")

            _tg_poll()
