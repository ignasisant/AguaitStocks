"""Profile page — account identity, preferences and the personal watchlist.

Everything here is per-account (see stocks.web.auth): the watchlist editor
writes this user's watchlist.yaml, preferences go to their prefs.json. Alert
rules and broker aliases stay YAML-only; saving the editor preserves them.

The Preferences tab follows the "Aguait Perfil Refactor" canvas: every setting
is a row with its label and explanation on the left and its control on the
right, the rows are grouped into cards (Interface / Tax residence / Data and
account) instead of floating on the page, and a sticky right rail carries the
tutorial and a read-only summary of what is set.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from stocks import storage
from stocks.config import CURRENCY_SYMBOL, load_watchlist
from stocks.portfolio import last_import, tax
from stocks.portfolio.tax import de as tax_de
from stocks.web import (
    auth,
    css,
    empty,
    exports,
    i18n,
    onboarding,
    tax_ui,
    widgets,
)
from stocks.web.i18n import t as tr
from stocks.web.markup import esc

# Account identity, prefs and the watchlist editor are all per-account.
auth.require_login()

# The five currencies this app is actually used in lead the chip row; the
# other six sit behind a popover. Eleven chips in a row was the single widest
# control on the page and the reason the settings column had no room for a
# rail (canvas 1a).
_TOP_CURRENCIES = ("EUR", "USD", "GBP", "CHF", "SEK")

# Page stylesheet. Cards, setting rows and the rail are plain markup over
# Streamlit's own blocks: `st.container(key=…)` puts an `st-key-<key>` class on
# the block, which is the only stable hook a container gives us. Card padding
# is zeroed here because each row carries its own — the card is a frame, the
# rows are the grid. Never write a left angle bracket in this block: DOMPurify
# drops the whole stylesheet when the text holds one.
_CSS = """
/* ------------------------------------------------------ setting cards */
[data-testid="stMainBlockContainer"]
  [data-testid="stVerticalBlock"][class*="st-key-pcard_"] {
  padding: 0; gap: 0;
}
.ag-cardhead {
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
  padding: 16px 24px 14px;
}
.ag-cardtitle {
  font-size: var(--ag-fs-lg); font-weight: 600; line-height: 1.3;
  color: var(--ag-text-primary);
}
.ag-cardsub { font-size: var(--ag-fs-sm); color: var(--ag-text-muted); }
.ag-cardnote {
  display: inline-flex; align-items: center; gap: 5px;
  border-radius: var(--ag-radius-pill); padding: 2px 9px;
  font-size: var(--ag-fs-xs); font-weight: 600;
  background: var(--ag-warn-band); color: var(--ag-warn);
}
/* -------------------------------------------------------- setting rows */
/* The divider is a pseudo-element, NEVER a border: app.py's card tagger
   stamps .topstocks-card on any main-area block whose computed border-top is
   thicker than 0, which turned every row into a 16px-radius card of its own.
   Inset 24px each side, like the canvas's own rule. */
[class*="st-key-prow_"] { padding: 20px 24px; position: relative; }
[class*="st-key-prow_"]::before {
  content: ""; position: absolute; left: 24px; right: 24px; top: 0;
  height: 1px; background: var(--ag-border);
}
/* The canvas's spacing: 20px between cards, 4px between a label and its
   explanation. Streamlit's own block gap is one value for the whole page. */
[class*="st-key-prefs_body"] [data-testid="stColumn"]
  > [data-testid="stVerticalBlock"] { gap: 20px; }
[class*="st-key-prow_"] [data-testid="stColumn"]
  > [data-testid="stVerticalBlock"] { gap: 4px; }
/* Label gutter is fixed, as in the canvas: the help text must not reflow with
   the viewport, and the control field takes whatever is left. */
[class*="st-key-prow_"] [data-testid="stHorizontalBlock"]
  > [data-testid="stColumn"]:first-child {
  flex: 0 0 260px;
}
[class*="st-key-prow_"] [data-testid="stCaptionContainer"] p {
  font-size: var(--ag-fs-sm); line-height: 1.55; color: var(--ag-text-muted);
}
[class*="st-key-prow_"] .stSelectbox,
[class*="st-key-prow_"] .stNumberInput { max-width: 340px; }
[class*="st-key-prow_"] [data-testid="stCaptionContainer"] { margin-top: 2px; }
.ag-morehint {
  display: block; margin-top: 8px;
  font-size: var(--ag-fs-sm); color: var(--ag-text-faint);
}
/* The chips and the "N more" trigger are one row, and the trigger is the
   canvas's dashed chip rather than a second solid button. */
[class*="st-key-pccy_row"] { flex-wrap: wrap; gap: 8px; align-items: center; }
[class*="st-key-pccy_row"] [data-testid="stPopover"] button,
[class*="st-key-pccy_row"] .stPopover button {
  border-style: dashed; border-color: var(--ag-border-focus);
  background: transparent;
}
[class*="st-key-pccy_row"] [data-testid="stPopover"] button p,
[class*="st-key-pccy_row"] .stPopover button p {
  color: var(--ag-text-secondary); font-weight: 500;
}
/* The jurisdiction's own rules, as facts rather than prose. */
.ag-rules {
  display: flex; flex-wrap: wrap; gap: 22px; margin-top: 12px;
  background: var(--ag-surface-page); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-md); padding: 12px 16px;
}
.ag-rule { display: flex; flex-direction: column; gap: 3px; }
.ag-rule-k {
  font-size: var(--ag-fs-xs); font-weight: 500; color: var(--ag-text-muted);
}
.ag-rule-v {
  font-size: var(--ag-fs-md); font-weight: 600; color: var(--ag-text-primary);
}
/* ------------------------------------------------------- identity card */
/* Its own key, not a pcard_ one: it has no row grid, so it keeps the card
   padding the pcard_ rule above zeroes. */
[class*="st-key-pident_row"] { align-items: center; flex-wrap: nowrap; }
[class*="st-key-pident_row"] > [data-testid="stElementContainer"]:first-child {
  flex: 1 1 auto; min-width: 0;
}
.ag-ident { display: flex; align-items: center; gap: 16px; }
.ag-avatar {
  width: 52px; height: 52px; flex: 0 0 auto; overflow: hidden;
  border-radius: var(--ag-radius-pill);
  background: var(--ag-purple-900); border: 1px solid var(--ag-purple-800);
  display: flex; align-items: center; justify-content: center;
  font-family: Epilogue, sans-serif; font-weight: 800; font-size: 20px;
  color: var(--ag-purple-400);
}
.ag-avatar img { width: 100%; height: 100%; object-fit: cover; }
.ag-ident-t { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ag-ident-n {
  font-size: var(--ag-fs-2xl); font-weight: 600; line-height: 1.25;
  color: var(--ag-text-primary);
}
.ag-ident-e { font-size: var(--ag-fs-md); color: var(--ag-text-secondary); }
.ag-ident-r {
  margin-left: auto; display: flex; flex-direction: column; gap: 6px;
  align-items: flex-end; min-width: 0;
}
.ag-folder {
  display: inline-flex; align-items: center; gap: 8px;
  max-width: min(420px, 100%);
  background: var(--ag-surface-page); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-sm); padding: 6px 10px;
  font-family: "Martian Mono", monospace; font-size: var(--ag-fs-xs);
  color: var(--ag-text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ag-ident-note { font-size: var(--ag-fs-sm); color: var(--ag-text-muted); }
/* -------------------------------------------------------------- the rail */
[class*="st-key-prefs_body"] > [data-testid="stHorizontalBlock"]
  > [data-testid="stColumn"]:last-child {
  flex: 0 0 320px; align-self: flex-start; position: sticky; top: 4.5rem;
}
[data-testid="stMainBlockContainer"]
  [data-testid="stVerticalBlock"][class*="st-key-prail_tour"] {
  background: var(--ag-purple-900); border-color: var(--ag-purple-800);
}
[class*="st-key-prail_tour"] .stButton,
[class*="st-key-prail_tour"] .stButton button { width: 100%; }
.ag-railtitle {
  display: flex; align-items: center; gap: 10px;
  font-size: var(--ag-fs-lg); font-weight: 600; color: var(--ag-text-primary);
}
/* Material Symbols ligature, not inline SVG — st.html's sanitizer strips svg. */
.ag-railicon {
  font-family: "Material Symbols Rounded"; font-size: 18px; line-height: 1;
  font-variation-settings: "FILL" 0, "wght" 300; color: var(--ag-purple-400);
}
.ag-railbody {
  display: block; margin-top: 6px; font-size: var(--ag-fs-md);
  line-height: 1.6; color: var(--ag-purple-300);
}
.ag-prog { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
.ag-prog-track {
  flex: 1; height: 4px; border-radius: var(--ag-radius-pill);
  background: var(--ag-purple-800); overflow: hidden;
}
.ag-prog-fill { height: 100%; background: var(--ag-purple-400); }
.ag-prog-n {
  font-family: "Martian Mono", monospace; font-size: var(--ag-fs-2xs);
  font-weight: 500; color: var(--ag-purple-400);
}
.ag-sum { display: flex; flex-direction: column; gap: 12px; }
.ag-sum-t { font-size: var(--ag-fs-lg); font-weight: 600; }
.ag-sum-row {
  display: flex; justify-content: space-between; gap: 12px;
  font-size: var(--ag-fs-md);
}
.ag-sum-row span { color: var(--ag-text-secondary); }
.ag-sum-row b { color: var(--ag-text-primary); font-weight: 600; }
.ag-sum-rule { height: 1px; background: var(--ag-border); }
.ag-sum-note {
  font-size: var(--ag-fs-sm); line-height: 1.6; color: var(--ag-text-muted);
}
/* The tab strip's right-hand reassurance. An absolutely placed element
   rather than a ::after on the tab list: the list is a scroll container on
   phones and clipped generated content there. */
[class*="st-key-profile_tabbar"] { position: relative; }
[class*="st-key-psavehint"] {
  position: absolute; right: 0; top: 6px; z-index: 1;
}
.ag-savehint {
  display: flex; align-items: center; gap: 6px; white-space: nowrap;
  font-size: var(--ag-fs-sm); color: var(--ag-text-faint);
}
.ag-savehint .ag-railicon { font-size: 15px; color: var(--ag-success-fill); }
.ag-folder .ag-railicon {
  font-size: 15px; color: var(--ag-text-muted); flex: 0 0 auto;
}
/* text-overflow needs a block, not the flex chip itself. */
.ag-folder-p { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* Phones: one column, no sticky rail, cards edge to edge. */
@media (max-width: 640px) {
  [class*="st-key-prow_"] { padding: 14px 16px; }
  [class*="st-key-prow_"]::before { left: 16px; right: 16px; }
  [class*="st-key-prow_"] [data-testid="stHorizontalBlock"]
    > [data-testid="stColumn"]:first-child { flex: 1 1 auto; }
  [class*="st-key-psavehint"] { display: none; }
  .ag-cardhead { padding: 14px 16px 12px; }
  .ag-ident { flex-wrap: wrap; gap: 12px; }
  .ag-ident-r { margin-left: 0; align-items: flex-start; width: 100%; }
  [class*="st-key-prefs_body"] > [data-testid="stHorizontalBlock"]
    > [data-testid="stColumn"]:last-child { position: static; flex: 1 1 auto; }
}
"""


def _card_head(title: str, sub: str = "", note: str = "") -> str:
    """A card's header strip: title, optional muted subtitle, optional pill."""
    parts = [f'<span class="ag-cardtitle">{esc(title)}</span>']
    if sub:
        parts.append(f'<span class="ag-cardsub">{esc(sub)}</span>')
    if note:
        parts.append(f'<span class="ag-cardnote">{esc(note)}</span>')
    return f'<div class="ag-cardhead">{"".join(parts)}</div>'


def _row(card, key: str, label: str, help_text: str = "", *, align: str = "top"):
    """One setting: label plus explanation left, control right.

    Returns the right-hand column so the caller can draw whatever widget the
    setting needs into it. The split is the canvas's 260px label gutter (fixed
    in CSS) against the control field; Streamlit stacks the two on phones.
    `align` is "center" for the rows whose control is a lone button, which the
    canvas centers against the label block.
    """
    row = card.container(key=f"prow_{key}")
    left, right = row.columns([26, 71], gap="medium", vertical_alignment=align)
    left.markdown(f"**{label}**")
    if help_text:
        left.caption(help_text)
    return right


# CURRENCY_SYMBOL formats amounts, so it spells the Swiss franc and the krona
# out ("CHF 12"). On a chip the canvas wants the mark, not the code twice.
_CCY_MARKS = {"CHF": "\u20a3", "SEK": "kr", "NOK": "kr", "DKK": "kr"}


def _short_path(path) -> str:
    """The tail of a data folder: ".../users/<account>".

    The full path is a per-account slug under the repo, long enough to push
    the Log out button onto a second line — and its identifying part is the
    last segment, not the first. The whole thing stays in the chip's tooltip.
    """
    parts = str(path).split("/")
    return "/".join(parts[-2:]) if len(parts) > 3 else str(path)


def _ccy_label(code: str) -> str:
    """"€ EUR" — the currency's own mark ahead of its code."""
    mark = _CCY_MARKS.get(code) or CURRENCY_SYMBOL.get(code, "").strip()
    return f"{mark} {code}" if mark and mark != code else code


@st.cache_data(show_spinner=False)
def _ledger_csv(db: str, mtime: float, base: str) -> bytes:
    """`exports.ledger_csv`, memoized on the ledger's own state.

    `mtime` is in the signature only to key the cache: an import invalidates
    the export without a TTL, and nothing else can change it.
    """
    return exports.ledger_csv(db, base)


@st.dialog(tr("profile.delete_title"))
def _delete_dialog(paths) -> None:
    """The deletion path the privacy policy promises, behind a real dialog.

    Was an expander on the page; the canvas puts it back where the rest of the
    destructive controls in this app live — a button that has to open
    something before it can do anything.
    """
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
        except Exception:  # noqa: BLE001 — surface, never half-delete silently
            st.error(tr("profile.delete_failed"), icon=":material/error:")
        else:
            st.logout()


st.title(tr("nav.profile"))

paths = auth.user_paths()
prefs = auth.load_prefs()
holdings = load_watchlist(paths.watchlist)

css.inject(_CSS)

# ------------------------------------------------------------------ account
# Identity, where this account's files live and the way out, on one card.
with st.container(border=True, key="pident_card"):
    _ident = st.container(
        horizontal=True, vertical_alignment="center", key="pident_row"
    )
    _name = getattr(st.user, "name", None) or st.user.email
    _picture = getattr(st.user, "picture", None)
    _initials = "".join(p[0] for p in str(_name).split()[:2]).upper() or "?"
    _avatar = (
        f'<img src="{esc(_picture)}" alt="">' if _picture else esc(_initials)
    )
    _ident.html(
        '<div class="ag-ident">'
        f'<div class="ag-avatar">{_avatar}</div>'
        '<div class="ag-ident-t">'
        f'<span class="ag-ident-n">{esc(_name)}</span>'
        f'<span class="ag-ident-e">{esc(st.user.email)}</span>'
        "</div>"
        '<div class="ag-ident-r">'
        f'<span class="ag-folder" title="{esc(str(paths.root))}">'
        '<span class="ag-railicon">folder_open</span>'
        f'<span class="ag-folder-p">{esc(_short_path(paths.root))}</span>'
        "</span>"
        f'<span class="ag-ident-note">{esc(tr("profile.account_scope"))}</span>'
        "</div></div>"
    )
    _ident.button(tr("common.log_out"), icon=":material/logout:", on_click=st.logout)

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
    "watch": (
        f":material/format_list_bulleted: {tr('profile.watchlist')}"
        + (f" :gray-badge[{len(holdings)}]" if holdings else "")
    ),
    "notify": f":material/notifications: {tr('profile.notifications')}",
}
_want_tab = st.session_state.pop("profile_tab", None)
if _want_tab in _TAB_LABELS:
    st.session_state["profile_tabs"] = _TAB_LABELS[_want_tab]
# on_change="rerun" makes the tabs a real keyed widget (session-settable);
# every tab still renders every run — no .open gating — so the watchlist
# editor and the polling fragment keep their existing behavior, at the cost
# of one rerun per manual tab switch.
_tabbar = st.container(key="profile_tabbar")
# Nothing on this page has a Save button, so the page has to say so.
_tabbar.container(key="psavehint").html(
    '<span class="ag-savehint">'
    '<span class="ag-railicon">check</span>'
    f'{esc(tr("profile.saves_instantly"))}</span>'
)
tab_prefs, tab_iv, tab_watch, tab_notify = _tabbar.tabs(
    list(_TAB_LABELS.values()), key="profile_tabs", on_change="rerun"
)

# -------------------------------------------------------------- preferences
with tab_prefs:
    _body = st.container(key="prefs_body")
    _main, _rail = _body.columns([32, 10], gap="medium", vertical_alignment="top")

    # ------------------------------------------------------------ interface
    _ui = _main.container(border=True, key="pcard_ui")
    _ui.html(_card_head(tr("profile.ui_section"), tr("profile.ui_section_sub")))

    # Language: "auto" follows the browser locale (st.context.locale); an
    # explicit pick is stored and wins over the browser on every page (i18n).
    _AUTO = "auto"
    lang_opts = [_AUTO, *i18n.LANGUAGES]
    current_lang = prefs.get("language") or _AUTO

    def _lang_label(code: str) -> str:
        return tr("profile.lang_auto") if code == _AUTO else i18n.LANGUAGES[code]

    lang = _row(
        _ui, "lang", tr("profile.language"), tr("profile.language_caption")
    ).selectbox(
        tr("profile.language"),
        lang_opts,
        index=lang_opts.index(current_lang if current_lang in lang_opts else _AUTO),
        format_func=_lang_label,
        key="pref_language",
        label_visibility="collapsed",
    )
    _lang_val = None if lang == _AUTO else lang
    if _lang_val != prefs.get("language"):
        prefs["language"] = _lang_val
        auth.save_prefs(prefs)
        st.rerun()  # re-run so app.py re-resolves the language for the whole app

    # Reference currency. The five in daily use are chips; the rest live in a
    # popover so the row stays one line. A currency picked from the popover is
    # pushed into the chip row by dropping both widget states, which lets the
    # `default=` below take effect again on the next run.
    _ccy_cell = _row(
        _ui,
        "ccy",
        tr("profile.display_currency"),
        tr("profile.currency_caption"),
    )
    _ccy_now = prefs.get("currency", "EUR")
    _chips = list(_TOP_CURRENCIES) + (
        [] if _ccy_now in _TOP_CURRENCIES else [_ccy_now]
    )
    _rest = [c for c in auth.CURRENCIES if c not in _chips]
    _chiprow = _ccy_cell.container(
        horizontal=True, vertical_alignment="center", key="pccy_row"
    )
    ccy = _chiprow.segmented_control(
        tr("profile.display_currency"),
        _chips,
        default=_ccy_now if _ccy_now in _chips else None,
        format_func=_ccy_label,
        key="pref_currency",
        label_visibility="collapsed",
    )
    if ccy and ccy != _ccy_now:
        prefs["currency"] = ccy
        auth.save_prefs(prefs)
        st.toast(tr("profile.currency_set", ccy=ccy), icon=":material/check:")
    if _rest:
        # No icon: the popover trigger draws its own chevron.
        with _chiprow.popover(tr("profile.currency_more", n=len(_rest))):
            _pick = st.segmented_control(
                tr("profile.display_currency"),
                _rest,
                default=None,
                format_func=_ccy_label,
                key="pref_currency_more",
                label_visibility="collapsed",
            )
            if _pick and _pick != _ccy_now:
                prefs["currency"] = _pick
                auth.save_prefs(prefs)
                st.session_state.pop("pref_currency", None)
                st.session_state.pop("pref_currency_more", None)
                st.rerun()
        _ccy_cell.html(
            f'<span class="ag-morehint">{esc(" · ".join(_rest))}</span>'
        )

    # ---------------------------------------------------------- tax residence
    # Which country's rules the Realized & tax tab applies. "auto" reads the
    # region off the browser locale (en-US -> US) and lands on Spain when it
    # recognizes nothing — the ledger has to be taxed under some set of rules.
    # The bracket inputs below only render for jurisdictions that read them;
    # Spain's savings base doesn't care about filing status or other income.
    _taxcard = _main.container(border=True, key="pcard_tax")
    _taxcard.html(
        _card_head(tr("profile.tax_section"), note=tr("profile.tax_legal_note"))
    )
    _res_opts = [tax_ui.AUTO, *tax.codes()]
    _res_current = prefs.get(tax_ui.PREF_RESIDENCE) or tax_ui.AUTO

    def _res_label(code: str) -> str:
        return (
            tr("profile.tax_residence_auto")
            if code == tax_ui.AUTO
            else tax_ui.label(code)
        )

    _res_cell = _row(
        _taxcard,
        "res",
        tr("profile.tax_residence"),
        tr("profile.tax_residence_caption"),
    )
    residence = _res_cell.selectbox(
        tr("profile.tax_residence"),
        _res_opts,
        index=_res_opts.index(
            _res_current if _res_current in _res_opts else tax_ui.AUTO),
        format_func=_res_label,
        key="pref_tax_residence",
        label_visibility="collapsed",
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

    # What the pick actually decides, as facts rather than a paragraph: the
    # currency the basis is replayed in, the share-identification rule and
    # where the tax year starts. Reading them off the jurisdiction keeps this
    # honest when a country module changes.
    _active = tax.get(tax_ui.resolve_code(prefs))
    _year_rule = (
        tr("profile.tax_year_calendar")
        if _active.year_start == (1, 1)
        else tr(
            "profile.tax_year_from",
            day=_active.year_start[1],
            month=_active.year_start[0],
        )
    )
    _res_cell.html(
        '<div class="ag-rules">'
        f'<div class="ag-rule"><span class="ag-rule-k">'
        f'{esc(tr("profile.tax_rule_cost"))}</span>'
        f'<span class="ag-rule-v">{esc(_active.currency)} · '
        f'{esc(tr("profile.tax_rule_fx"))}</span></div>'
        f'<div class="ag-rule"><span class="ag-rule-k">'
        f'{esc(tr("profile.tax_rule_matching"))}</span>'
        f'<span class="ag-rule-v">'
        f'{esc(tr(f"profile.tax_match_{_active.matching}"))}</span></div>'
        f'<div class="ag-rule"><span class="ag-rule-k">'
        f'{esc(tr("profile.tax_rule_year"))}</span>'
        f'<span class="ag-rule-v">{esc(_year_rule)}</span></div>'
        "</div>"
    )

    # Only the knobs the active jurisdiction actually reads: Spain's savings
    # base has no filing status and no bracket to stack income on, so an ES
    # account sees nothing below. The order is the jurisdiction's.
    _fields = _active.settings_fields
    if "filing_status" in _fields:
        _statuses = list(_active.filing_statuses)
        _status_current = prefs.get(tax_ui.PREF_FILING_STATUS) or _statuses[0]
        status = _row(
            _taxcard,
            "filing",
            tr("profile.tax_filing_status"),
            tr(f"profile.tax_filing_status_caption_{_active.code.lower()}"),
        ).selectbox(
            tr("profile.tax_filing_status"),
            _statuses,
            index=_statuses.index(
                _status_current if _status_current in _statuses else _statuses[0]),
            format_func=lambda c: tr(f"profile.tax_status_{c}"),
            key="pref_tax_filing_status",
            label_visibility="collapsed",
        )
        if status != prefs.get(tax_ui.PREF_FILING_STATUS):
            prefs[tax_ui.PREF_FILING_STATUS] = status
            auth.save_prefs(prefs)

    if "church_tax_rate" in _fields:
        # Kirchensteuer: 8% of the tax in Bavaria and Baden-Württemberg, 9%
        # in the other states, nothing if the filer is not church-registered.
        _rates = list(tax_de.CHURCH_TAX_RATES)
        try:
            _rate_now = float(prefs.get(tax_ui.PREF_CHURCH_TAX) or 0.0)
        except (TypeError, ValueError):
            _rate_now = 0.0
        church = _row(
            _taxcard,
            "church",
            tr("profile.tax_church"),
            tr("profile.tax_church_caption"),
        ).selectbox(
            tr("profile.tax_church"),
            _rates,
            index=_rates.index(_rate_now if _rate_now in _rates else 0.0),
            format_func=lambda r: tr(f"profile.tax_church_{int(r * 100)}"),
            key="pref_tax_church",
            label_visibility="collapsed",
        )
        if float(church) != _rate_now:
            prefs[tax_ui.PREF_CHURCH_TAX] = float(church)
            auth.save_prefs(prefs)

    if "other_income" in _fields:
        income = _row(
            _taxcard,
            "income",
            tr("profile.tax_other_income"),
            tr(f"profile.tax_other_income_caption_{_active.code.lower()}"),
        ).number_input(
            tr("profile.tax_other_income"),
            min_value=0.0,
            step=1_000.0,
            value=float(prefs.get(tax_ui.PREF_OTHER_INCOME) or 0.0),
            key="pref_tax_other_income",
            label_visibility="collapsed",
        )
        if float(income) != float(prefs.get(tax_ui.PREF_OTHER_INCOME) or 0.0):
            prefs[tax_ui.PREF_OTHER_INCOME] = float(income)
            auth.save_prefs(prefs)

    if "subnational_rate" in _fields:
        # Canada: the provincial half of the bill. Entered as a percentage
        # because that is how every rate table prints it, stored as a fraction.
        try:
            _sub_now = float(prefs.get(tax_ui.PREF_SUBNATIONAL) or 0.0)
        except (TypeError, ValueError):
            _sub_now = 0.0
        sub = _row(
            _taxcard,
            "sub",
            tr("profile.tax_subnational"),
            tr("profile.tax_subnational_caption"),
        ).number_input(
            tr("profile.tax_subnational"),
            min_value=0.0,
            max_value=30.0,
            step=0.5,
            value=round(_sub_now * 100, 2),
            key="pref_tax_subnational",
            label_visibility="collapsed",
        )
        if abs(float(sub) / 100 - _sub_now) > 1e-9:
            prefs[tax_ui.PREF_SUBNATIONAL] = float(sub) / 100
            auth.save_prefs(prefs)

    if "include_niit" in _fields:
        niit = _row(
            _taxcard,
            "niit",
            tr("profile.tax_niit"),
            tr("profile.tax_niit_caption"),
        ).toggle(
            tr("profile.tax_niit"),
            value=bool(prefs.get(tax_ui.PREF_NIIT)),
            key="pref_tax_niit",
            label_visibility="collapsed",
        )
        if niit != bool(prefs.get(tax_ui.PREF_NIIT)):
            prefs[tax_ui.PREF_NIIT] = niit
            auth.save_prefs(prefs)

    # ------------------------------------------------------- data and account
    _data = _main.container(border=True, key="pcard_data")
    _data.html(_card_head(tr("profile.data_section")))

    _export_cell = _row(
        _data,
        "export",
        tr("profile.export_title"),
        tr("profile.export_help"),
        align="center",
    )
    # Read back off `prefs` rather than `_ccy_now`: a currency picked from the
    # chips this very run is already in the dict, and the export should be in
    # the currency the user just chose.
    _ccy_base = str(prefs.get("currency", "EUR")).upper()
    try:
        _db_mtime = paths.db.stat().st_mtime
    except OSError:
        _db_mtime = 0.0
    _csv = _ledger_csv(str(paths.db), _db_mtime, _ccy_base) if _db_mtime else b""
    if _csv:
        _export_cell.download_button(
            tr("profile.export_button"),
            _csv,
            f"aguait-ledger-{datetime.now():%Y-%m-%d}.csv",
            "text/csv",
            icon=":material/download:",
            key="export_ledger",
        )
    else:
        _export_cell.caption(tr("profile.export_none"))

    # The deletion path the privacy policy promises. Owner account never gets
    # the control: its "data dir" is the repo root (auth.delete_account would
    # refuse it anyway).
    if paths.root != auth.PROJECT_ROOT:
        if _row(
            _data,
            "delete",
            tr("profile.delete_row_title"),
            tr("profile.delete_row_help"),
            align="center",
        ).button(
            tr("profile.delete_open"),
            icon=":material/delete_forever:",
            key="delete_open",
        ):
            _delete_dialog(paths)

    # ----------------------------------------------------------------- rail
    # The empty half of the old page. The tour is where a returning user looks
    # for the walkthrough, and the only entry point left once an account has
    # finished it: after that it only auto-opens for a release the account has
    # not seen (stocks.web.onboarding.maybe_open).
    _tour = _rail.container(border=True, key="prail_tour")
    _tour.html(
        '<span class="ag-railtitle">'
        '<span class="ag-railicon">menu_book</span>'
        f'{esc(tr("tour.launch"))}</span>'
        f'<span class="ag-railbody">{esc(tr("tour.launch_caption"))}</span>'
    )
    # A caller-supplied label suppresses render_launcher's own icon, so the
    # glyph rides inside the label; the card title already says "tour", which
    # is why the button says what pressing it does instead.
    onboarding.render_launcher(
        "profile_tour",
        _tour,
        label=f":material/menu_book: {tr('tour.launch_start')}",
        button_type="primary",
    )
    _states = onboarding.setup_state(prefs)
    _done = sum(1 for on in _states.values() if on)
    _tour.html(
        '<div class="ag-prog">'
        '<div class="ag-prog-track">'
        f'<div class="ag-prog-fill" style="width:{_done / len(_states):.0%}"></div>'
        "</div>"
        f'<span class="ag-prog-n">{_done}/{len(_states)}</span></div>'
    )

    # What is set, without opening three controls to check.
    _imported = last_import.load(paths.last_import)
    _when = tr("profile.summary_never")
    if _imported:
        _when = str(_imported.imported_at)[:10]
    # Short forms on purpose: a 320px rail is a summary, not a second copy of
    # the controls — "Auto", "EUR", "Spain · FIFO".
    _lang_short = _lang_label(current_lang).split("(")[0].strip()
    _tax_short = (
        f"{tax_ui.label(_active.code).split(chr(8212))[0].strip()} · "
        f'{tr(f"profile.tax_match_{_active.matching}")}'
    )
    _rail.container(border=True, key="prail_sum").html(
        '<div class="ag-sum">'
        f'<span class="ag-sum-t">{esc(tr("profile.summary_title"))}</span>'
        f'<div class="ag-sum-row"><span>{esc(tr("profile.language"))}</span>'
        f"<b>{esc(_lang_short)}</b></div>"
        f'<div class="ag-sum-row">'
        f'<span>{esc(tr("profile.display_currency"))}</span>'
        f'<b>{esc(str(prefs.get("currency", "EUR")))}</b></div>'
        f'<div class="ag-sum-row"><span>{esc(tr("profile.tax_section"))}</span>'
        f"<b>{esc(_tax_short)}</b></div>"
        f'<div class="ag-sum-row">'
        f'<span>{esc(tr("profile.summary_last_import"))}</span>'
        f"<b>{esc(_when)}</b></div>"
        '<div class="ag-sum-rule"></div>'
        f'<span class="ag-sum-note">{esc(tr("profile.summary_note"))}</span>'
        "</div>"
    )

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

    # Loaded once at the top of the script — the tab label carries its count.
    if not holdings:
        # Already on the page that fixes it, so no CTA — the body says which
        # control to reach for instead.
        empty.state(
            tr("profile.empty_watchlist_title"),
            tr("profile.empty_watchlist_body"),
            event="profile.watchlist",
            icon="list_alt",
        )
    # Shortcut for the areas the account said it follows: append the examples
    # it does not have yet. Additive and never destructive — an edited
    # watchlist keeps every row it already has, which is why this can sit here
    # rather than only in front of an untouched seed.
    # The *saved* profile, not the form's live widget values on the tab next
    # door: an unsaved radio flick should not rewrite this offer. An account
    # that never saved a profile has no focus, so this is empty and the whole
    # block stays off the page.
    _suggested = auth.focus_suggestions(path=paths.watchlist)
    if _suggested:
        with st.container(border=True):
            names = ", ".join(f"**{e['ticker']}**" for e in _suggested)
            st.markdown(
                f"{tr('profile.focus_suggest_title')}\n\n{names}"
            )
            st.caption(tr("profile.focus_suggest_help"))
            if st.button(
                tr("profile.focus_suggest_add", n=len(_suggested)),
                icon=":material/playlist_add:",
                key="focus_suggest_add",
            ):
                auth.save_watchlist_entries(
                    [
                        {"ticker": h.ticker, "name": h.name, "favorite": h.favorite,
                         "shares": h.shares or None, "cost": h.cost, "tags": h.tags}
                        for h in holdings
                    ]
                    + _suggested,
                    paths.watchlist,
                )
                st.rerun()

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
