"""The assistant surface built on the multi-provider registry (web/llm.py).

The whole assistant lives in one *side panel* — a launcher icon pinned top-right
that opens a slide-in overlay, reachable from every page. It is fully
self-contained: provider choice, model choice, BYOK key entry (with the same
encrypted-for-15-days storage the app uses elsewhere) and the conversation all
happen inside the panel, so there is no separate Chat page in the nav.

Storage is account-scoped: session slot "llm_key::<pid>", prefs "<pid>_key_enc" /
"<pid>_key_saved_at", the "llm_provider" / "<pid>_model" choices, and one
"chat_history::<watchlist_path>" thread per account.
"""

from __future__ import annotations

import time

import streamlit as st
from cryptography.fernet import Fernet, InvalidToken

from stocks.config import load_watchlist
from stocks.config import positions as load_positions
from stocks.web import auth, llm
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import enriched_positions
from stocks.web.widgets import db_mtime

_TTL = 15 * 24 * 3600  # 15 days, seconds — must match the Chat page's window


# ------------------------------------------------------------- key + provider
# Account-scoped storage: session slot "llm_key::<pid>", prefs "<pid>_key_enc" /
# "<pid>_key_saved_at", and the "llm_provider" / "<pid>_model" choices. Both the
# read side (active_*) and the setup side (pick/gate/save/forget) live here now
# that the panel is the only assistant surface.


def _sk(pid: str) -> str:
    return f"llm_key::{pid}"


def _fernet() -> Fernet | None:
    k = st.secrets.get("chat", {}).get("enc_key")
    return Fernet(k) if k else None


def _load_saved_key(pid: str) -> str:
    """Decrypt a remembered provider key if present and within the 15-day window."""
    f = _fernet()
    if not f:
        return ""
    prefs = auth.load_prefs()
    token = prefs.get(f"{pid}_key_enc")
    saved_at = prefs.get(f"{pid}_key_saved_at", 0)
    if not token or time.time() - saved_at > _TTL:
        return ""
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""  # enc_key rotated or ciphertext corrupt -> treat as unset


def _save_key(pid: str, api_key: str) -> None:
    """Encrypt and persist a provider key (save_prefs mirrors prefs.json)."""
    f = _fernet()
    if not f:
        st.warning(tr("chat.no_enc"))
        return
    prefs = auth.load_prefs()
    prefs[f"{pid}_key_enc"] = f.encrypt(api_key.encode()).decode()
    prefs[f"{pid}_key_saved_at"] = int(time.time())
    auth.save_prefs(prefs)


def _forget_key(pid: str) -> None:
    st.session_state.pop(_sk(pid), None)
    prefs = auth.load_prefs()
    if prefs.pop(f"{pid}_key_enc", None) is not None:
        prefs.pop(f"{pid}_key_saved_at", None)
        auth.save_prefs(prefs)


def active_provider() -> llm.Provider | None:
    """The provider the user last chose (session > prefs > default), or None
    when no provider SDK is installed."""
    provs = llm.available_providers()
    if not provs:
        return None
    pid = (
        st.session_state.get("llm_provider")
        or auth.load_prefs().get("llm_provider")
        or llm.DEFAULT_PROVIDER
    )
    p = llm.PROVIDERS.get(pid)
    return p if (p and p.available()) else provs[0]


def active_key(provider: llm.Provider) -> str:
    return st.session_state.get(_sk(provider.id)) or _load_saved_key(provider.id)


def _active_model(provider: llm.Provider) -> str:
    """The model chosen for this provider (session > prefs > default)."""
    return (
        st.session_state.get(f"llm_model::{provider.id}")
        or auth.load_prefs().get(f"{provider.id}_model")
        or provider.default_model
    )


def _pick_provider(key: str) -> llm.Provider:
    """Provider selector; remembers the choice in session + prefs."""
    provs = llm.available_providers()
    ids = [p.id for p in provs]
    prefs = auth.load_prefs()
    default = (
        st.session_state.get("llm_provider")
        or prefs.get("llm_provider")
        or llm.DEFAULT_PROVIDER
    )
    labels = {p.id: p.label for p in provs}
    pid = st.selectbox(
        tr("chat.provider"),
        ids,
        index=ids.index(default) if default in ids else 0,
        format_func=lambda i: labels[i],
        key=key,
    )
    st.session_state["llm_provider"] = pid
    if pid != prefs.get("llm_provider"):
        prefs["llm_provider"] = pid
        auth.save_prefs(prefs)
    return llm.PROVIDERS[pid]


def _pick_model(provider: llm.Provider, key: str) -> str:
    """Model selector for the provider; remembers the choice in session + prefs."""
    prefs = auth.load_prefs()
    saved = prefs.get(f"{provider.id}_model")
    default = saved if saved in provider.models else provider.default_model
    model = st.selectbox(
        tr("chat.model"),
        provider.models,
        index=provider.models.index(default),
        key=key,
    )
    st.session_state[f"llm_model::{provider.id}"] = model
    if model != saved:
        prefs[f"{provider.id}_model"] = model
        auth.save_prefs(prefs)
    return model


def _key_gate(provider: llm.Provider) -> str | None:
    """Return the active key for the provider, or render the BYOK form and None."""
    key = st.session_state.get(_sk(provider.id)) or _load_saved_key(provider.id)
    if key:
        st.session_state[_sk(provider.id)] = key
        return key

    st.info(tr("chat.byok_help", provider=provider.label))
    entered = st.text_input(
        tr("chat.key_label", provider=provider.label),
        type="password",
        placeholder=provider.key_placeholder,
        help=tr("chat.key_help"),
        key=f"panel_key_{provider.id}",
    )
    remember = st.checkbox(tr("chat.remember"), key=f"panel_remember_{provider.id}")
    st.markdown(f"[{tr('chat.get_key')}]({provider.console_url})")
    if entered:
        entered = entered.strip()
        st.session_state[_sk(provider.id)] = entered
        if remember:
            _save_key(provider.id, entered)
        st.rerun()  # fragment-scoped: re-resolve the key, drop the form
    return None


# ------------------------------------------------------------- context


def _fmt_eur(x) -> str:
    return f"€{x:,.0f}" if x is not None and x == x else "n/a"  # x==x screens NaN


def _view_context() -> str:
    """What the user is looking at right now — page + focused ticker.

    Read from session state (set by app.py on each full run, before
    render_side_panel), so a fragment-only rerun of the panel still sees the
    current view without re-running app.py.
    """
    view = st.session_state.get("_chat_view")
    ticker = st.session_state.get("picker_selected")
    bits = []
    if view:
        bits.append(f"The user is currently on the {view} page.")
    if ticker:
        bits.append(f"The ticker in focus is {ticker}.")
    return ("Current view: " + " ".join(bits) + "\n\n") if bits else ""


def _portfolio_context() -> str:
    """A snapshot of the user's real book for the system prompt.

    Prefers the imported ledger valued at live prices (the same frame the
    Portfolio page shows, cached per account); falls back to the watchlist's
    positions (shares/cost only) when no ledger exists yet. Only the signed-in
    account's own data is read (auth.db_path / auth.watchlist_path).
    """
    db = auth.db_path()
    tbl = enriched_positions(str(db), db_mtime(str(db))) if db.exists() else None

    if tbl is not None and not tbl.empty:
        lines = []
        for tk, r in tbl.iterrows():
            pnl_pct, day_pct, wt = r.get("pnl_pct"), r.get("day_pct"), r.get("weight")
            lines.append(
                f"- {tk}: {r['shares']:g} sh | value {_fmt_eur(r['value_eur'])}"
                f" | cost {_fmt_eur(r['cost_eur'])}"
                + (f" | P/L {pnl_pct:+.1%} ({_fmt_eur(r['pnl_eur'])})"
                   if pnl_pct == pnl_pct else "")
                + (f" | weight {wt:.0%}" if wt == wt else "")
                + (f" | today {day_pct:+.1%}" if day_pct == day_pct else "")
            )
        total = tbl["value_eur"].dropna().sum()
        total_pnl = tbl["pnl_eur"].dropna().sum()
        book = (
            f"Holdings (live market data, EUR). Total book {_fmt_eur(total)}, "
            f"unrealised P/L {_fmt_eur(total_pnl)}:\n" + "\n".join(lines)
        )
        held = set(tbl.index)
    else:
        holds = load_positions(auth.watchlist_path())
        book = (
            "Positions (from watchlist; no live valuation):\n"
            + "\n".join(
                f"- {h.ticker}: {h.shares:g} shares"
                + (f" @ {h.cost:g} avg cost" if h.cost else "")
                for h in holds
            )
            if holds
            else "(no open positions)"
        )
        held = {h.ticker for h in holds}

    watching = [
        h.ticker for h in load_watchlist(auth.watchlist_path()) if h.ticker not in held
    ]
    if watching:
        book += "\n\nAlso on the watchlist (not held): " + ", ".join(watching)
    return book


def _system_prompt() -> str:
    """Frozen persona + the current view + a live snapshot of the account's book."""
    return (
        "You are a concise investing assistant embedded in a personal stock "
        "tracker. The signed-in user is an aggressive long-term (5y+) investor. "
        "You are not a licensed financial advisor: give analysis and trade-offs, "
        "not directives, and flag when something needs the user's own judgement. "
        "The context below is current as of this message; treat the figures as "
        "the user's real position, and let the current view guide what they are "
        "most likely asking about.\n\n"
        f"{_view_context()}"
        f"{_portfolio_context()}"
    )


# ------------------------------------------------------------- conversation


def render_conversation(ns: str, provider: llm.Provider, model: str, api_key: str) -> None:
    """Draw the history, take input, and stream the next answer.

    ns namespaces the widget keys so two surfaces can coexist. The
    account-scoped history is the same session-state list the Chat page uses,
    so a thread started in the panel continues on the full page and back.
    """
    hist_key = f"chat_history::{auth.watchlist_path()}"
    history: list[dict] = st.session_state.setdefault(hist_key, [])

    # box is created before the input so, in the panel, messages sit above the
    # (inline) chat input; on the page the input auto-docks to the bottom. New
    # turns are written back into box out of order so they render in place.
    box = st.container()
    with box:
        for msg in history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if prompt := st.chat_input(tr("chat.placeholder"), key=f"{ns}_input"):
        history.append({"role": "user", "content": prompt})
        with box, st.chat_message("user"):
            st.markdown(prompt)

    # Generate whenever the last turn is a user turn still awaiting a reply
    # (covers both a fresh message and a Regenerate through one code path).
    if history and history[-1]["role"] == "user":
        with box, st.chat_message("assistant"):
            try:
                answer = st.write_stream(
                    provider.stream(api_key, model, _system_prompt(), history)
                )
            except Exception as exc:  # classified per provider; unknown -> re-raise
                err = provider.error_key(exc)
                if err is None:
                    raise
                history.pop()  # drop the unanswered user turn
                st.error(tr(err, provider=provider.label))
                st.stop()
        history.append({"role": "assistant", "content": answer})

    if history and history[-1]["role"] == "assistant":
        with box:
            if st.button(tr("chat.regenerate"), icon=":material/refresh:",
                         key=f"{ns}_regen"):
                history.pop()
                st.rerun()


# ------------------------------------------------------------- side panel


# A launcher icon pinned top-right (every page, every width) that opens a
# slide-in overlay. On wide screens the panel is a 380px right rail and the page
# reserves room beside it; on narrow screens it fills the viewport and floats
# over the page. Colours match the fixed dark theme (.streamlit/config.toml).
_PANEL_CSS = """
<style>
/* Launcher: a round AI icon pinned top-right. width:max-content beats
   Streamlit's default width:100% (otherwise the button clips off-screen). */
.st-key-chatfab {
  position: fixed; top: 0.6rem; right: 3.5rem; z-index: 1000000;
  width: max-content !important; min-width: 0 !important;
}
.st-key-chatfab button {
  border-radius: 999px; width: 2.6rem; height: 2.6rem; padding: 0;
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.45);
}
.st-key-chatpanel {
  position: fixed; top: 2.9rem; right: 0; bottom: 0;
  width: 380px; z-index: 1000000;
  background: #1E293B; border-left: 1px solid #334155;
  padding: 0.75rem 1rem 1rem; overflow-y: auto;
  box-shadow: -10px 0 30px rgba(0, 0, 0, 0.35);
}
/* Pin the panel's chat input to the bottom while messages scroll above it. */
.st-key-chatpanel [data-testid="stChatInput"] {
  position: sticky; bottom: 0; background: #1E293B; padding-top: 0.4rem;
}
/* Wide screens: reserve room so page content never hides under the panel. */
@media (min-width: 1100px) {
  body:has(.st-key-chatpanel) .block-container { padding-right: 400px; }
}
/* Narrow screens: the panel fills the viewport (over the header too) and the
   page keeps its width underneath — no room reserved. */
@media (max-width: 1099px) {
  .st-key-chatpanel { top: 0; width: 100vw; border-left: none; }
}
</style>
"""


@st.fragment
def _panel_body() -> None:
    """The panel's interactive core, in a fragment so sending a message (or
    changing provider/model/key) reruns only the panel, not the underlying page.

    Fully self-contained: when no provider key is configured it shows the BYOK
    setup inline; once configured it tucks provider/model/forget into a
    collapsed expander and renders the conversation below."""
    provider = active_provider()
    if provider is None:  # no SDK installed — should not happen once deps sync
        st.error("No LLM provider is installed.")
        return

    if not active_key(provider):
        # Setup mode: choose a provider and enter its key, right here.
        provider = _pick_provider("panel_provider_setup")
        _key_gate(provider)  # renders the form; reruns the fragment on submit
        return

    # Configured: settings out of the way, conversation front and centre.
    with st.expander(tr("chat.settings"), expanded=False):
        provider = _pick_provider("panel_provider")
        _pick_model(provider, f"panel_model_{provider.id}")  # persists the choice
        if st.button(tr("chat.forget"), icon=":material/logout:", key="panel_forget"):
            _forget_key(provider.id)
            st.rerun()

    key = active_key(provider)  # provider may have just switched in the expander
    if not key:
        _key_gate(provider)  # newly-selected provider has no key yet -> prompt
        return
    render_conversation("panel", provider, _active_model(provider), key)


def render_side_panel(view_label: str) -> None:
    """Overlay assistant: launcher icon + slide-in panel. Call from app.py after
    page.run(), on every page, for signed-in users only."""
    st.session_state["_chat_view"] = view_label  # read by _view_context()
    st.html(_PANEL_CSS)

    if not st.session_state.get("chat_panel_open", False):
        with st.container(key="chatfab"):
            if st.button("", icon=":material/auto_awesome:", key="chat_fab_open",
                         type="primary", help=tr("chat.title")):
                st.session_state["chat_panel_open"] = True
                st.rerun()
        return

    with st.container(key="chatpanel"):
        title_col, close_col = st.columns([0.72, 0.28], vertical_alignment="center")
        title_col.markdown(f"**{tr('chat.title')}**")
        if close_col.button(tr("chat.close"), icon=":material/close:",
                            key="chat_panel_close", width="stretch"):
            st.session_state["chat_panel_open"] = False
            st.rerun()
        _panel_body()
