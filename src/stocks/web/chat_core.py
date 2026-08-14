"""The assistant surface built on the multi-provider registry (web/llm.py).

The whole assistant lives in one *side panel* — a launcher icon pinned top-right
that opens a slide-in overlay, reachable from every page. It is fully
self-contained: provider choice, model choice, BYOK key entry (with the same
encrypted-for-15-days storage the app uses elsewhere) and the conversation all
happen inside the panel, so there is no separate Chat page in the nav. The
keyless "Aguait AI" provider (llm.py free chain) skips the key gate entirely
and is throttled per account by _spend_free_quota.

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
from stocks.secrets_env import secret
from stocks.web import auth, chat_skills, llm
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import enriched_positions
from stocks.web.widgets import asset_logo, brand_logo, db_mtime

_TTL = 15 * 24 * 3600  # 15 days, seconds — must match the Chat page's window


# ------------------------------------------------------------- key + provider
# Account-scoped storage: session slot "llm_key::<pid>", prefs "<pid>_key_enc" /
# "<pid>_key_saved_at", and the "llm_provider" / "<pid>_model" choices. Both the
# read side (active_*) and the setup side (pick/gate/save/forget) live here now
# that the panel is the only assistant surface.


def _sk(pid: str) -> str:
    return f"llm_key::{pid}"


def _fernet() -> Fernet | None:
    k = secret("CHAT_ENC_KEY", "chat", "enc_key")
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
        or llm.default_provider_id()
    )
    p = llm.PROVIDERS.get(pid)
    return p if (p and p.available()) else provs[0]


def active_key(provider: llm.Provider) -> str:
    return st.session_state.get(_sk(provider.id)) or _load_saved_key(provider.id)


# The free chain runs on the operator's shared keys, so each account gets a
# modest daily allowance (override with [free_llm] daily_cap) — one runaway
# user must not drain the quota every other account depends on.
_FREE_DAILY_CAP = 30


def _free_daily_cap() -> int:
    try:
        return int(st.secrets.get("free_llm", {}).get("daily_cap", _FREE_DAILY_CAP))
    except Exception:
        return _FREE_DAILY_CAP


def _spend_free_quota() -> bool:
    """Consume one unit of today's free allowance; False when it's spent.

    The counter lives in the account's prefs under "free_msgs::<date>"; keys
    from previous days are dropped on write so prefs.json never accumulates."""
    day = time.strftime("%Y-%m-%d")
    key = f"free_msgs::{day}"
    prefs = auth.load_prefs()
    used = int(prefs.get(key, 0))
    if used >= _free_daily_cap():
        return False
    for stale in [k for k in prefs if k.startswith("free_msgs::") and k != key]:
        prefs.pop(stale)
    prefs[key] = used + 1
    auth.save_prefs(prefs)
    return True


def _active_model(provider: llm.Provider) -> str:
    """The model chosen for this provider (session > prefs > default)."""
    return (
        st.session_state.get(f"llm_model::{provider.id}")
        or auth.load_prefs().get(f"{provider.id}_model")
        or provider.default_model
    )


_DEFAULT_WIDTH = 380  # px; must match the .st-key-chatpanel fallback in the CSS
_MIN_WIDTH, _MAX_WIDTH = 320, 1500  # clamp range for the drag-to-resize handle


def _provider_option_md(pid: str) -> str:
    """Segmented-control label: provider logo (markdown image) + brand name."""
    p = llm.PROVIDERS[pid]
    src = brand_logo(p.id, p.domain) if p.domain else asset_logo("aguait-icon.svg")
    img = f"![{p.label}]({src}) " if src else ""
    return f"{img}{p.label}"


def _pick_provider(key: str) -> llm.Provider:
    """Provider selector; remembers the choice in session + prefs."""
    provs = llm.available_providers()
    ids = [p.id for p in provs]
    prefs = auth.load_prefs()
    default = (
        st.session_state.get("llm_provider")
        or prefs.get("llm_provider")
        or llm.default_provider_id()
    )
    pid = st.segmented_control(
        tr("chat.provider"),
        ids,
        format_func=_provider_option_md,
        default=default if default in ids else ids[0],
        required=True,  # clicking the active segment must not deselect it
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


# ------------------------------------------------------------- skills
# The skill library (web/chat_skills.py + web/skills/*.md) — analysis
# frameworks appended to the system prompt. Mode lives in prefs
# ("chat_skills_mode": auto|manual|off) plus the manual pick ("chat_skills").

_SKILL_MODES = ("auto", "manual", "off")


def _skill_label(sid: str) -> str:
    return tr(f"chat.skill.{sid}")


def _lens_label(ids: list[str]) -> str:
    return tr("chat.lens", skills=" · ".join(_skill_label(i) for i in ids))


def _pick_skills() -> None:
    """Skill mode + manual selection; remembers both in prefs."""
    prefs = auth.load_prefs()
    saved_mode = prefs.get("chat_skills_mode", "auto")
    if saved_mode not in _SKILL_MODES:
        saved_mode = "auto"
    mode = st.segmented_control(
        tr("chat.skills_mode"),
        _SKILL_MODES,
        default=saved_mode,
        format_func=lambda m: tr(f"chat.skills_{m}"),
        key="panel_skills_mode",
    ) or saved_mode  # deselecting the control keeps the saved mode
    if mode != prefs.get("chat_skills_mode"):
        prefs["chat_skills_mode"] = mode
        auth.save_prefs(prefs)
    if mode == "auto":
        st.caption(tr("chat.skills_auto_hint"))
    if mode != "manual":
        return
    ids = [s.id for s in chat_skills.catalog()]
    saved = [i for i in prefs.get("chat_skills", []) if i in ids]
    picked = st.multiselect(
        tr("chat.skills_label"), ids, default=saved,
        format_func=_skill_label,
        max_selections=chat_skills.MAX_MANUAL,
        key="panel_skills",
    )
    if picked != saved:
        prefs["chat_skills"] = picked
        auth.save_prefs(prefs)


def _resolve_skills(provider: llm.Provider, api_key: str,
                    history: list[dict]) -> list[str]:
    """Skill ids to apply to the pending answer, per the saved mode.

    Auto routes the message through the provider's cheapest model (for the
    keyless free chain that is one extra backend call per message). When the
    router call itself fails it falls back to the previous answer's skills —
    the answer always proceeds, and an unchanged skill set keeps the system
    prompt byte-identical, which keeps provider prompt caches warm."""
    prefs = auth.load_prefs()
    mode = prefs.get("chat_skills_mode", "auto")
    if mode == "off":
        return []
    if mode == "manual":
        valid = chat_skills.valid_ids()
        return [i for i in prefs.get("chat_skills", [])
                if i in valid][:chat_skills.MAX_MANUAL]
    # Auto. Prior user turns ride along so follow-ups ("why?", "and the
    # dividend?") keep routing to the thread's topic, not to nothing.
    prior = [m["content"][:200] for m in history[:-1] if m["role"] == "user"][-2:]
    context = _view_context().strip()
    if prior:
        context += "\nEarlier user messages (topic continuity): " + " | ".join(prior)
    ids = chat_skills.classify(provider, api_key, history[-1]["content"], context)
    if ids is None:  # router failed — reuse the previous turn's lens
        prev = [m for m in history if m["role"] == "assistant" and m.get("skills")]
        return prev[-1]["skills"] if prev else []
    return ids


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


# English phrasings for the stored profile enum keys (auth.PROFILE_*). The
# system prompt is English regardless of UI language, so the persona is built
# from these, not from the localized form labels.
_RISK_EN = {
    "aggressive": "an aggressive",
    "very_aggressive": "a very aggressive",
    "balanced": "a balanced",
    "conservative": "a conservative",
}
_HORIZON_EN = {
    "5y_plus": "5+ year",
    "3_5y": "3–5 year",
    "1_3y": "1–3 year",
    "under_1y": "under-1-year",
}
_FOCUS_EN = {
    "tech": "technology and growth stocks",
    "em": "emerging markets",
    "crypto": "crypto assets",
    "dividends_value": "dividends, value and broad-index holdings",
}
_CONSTRAINT_EN = {
    "spain_tax": "factor in Spanish tax residency (IRPF; no US wash-sale rule)",
    "eur": "reason and report in EUR",
    "no_leverage": "avoid recommending leverage, margin or derivatives",
    "esg": "apply ESG screening",
}


def _join_en(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _persona() -> str:
    """The 'who am I advising' sentence, built from the user's saved profile.

    Falls back to the historical default when the user hasn't filled the
    investor-profile form yet (auth.load_profile()['set'] is False)."""
    prof = auth.load_profile()
    if not prof.get("set"):
        return "The signed-in user is an aggressive long-term (5y+) investor. "
    risk = _RISK_EN.get(prof.get("risk"), "an aggressive")
    horizon = _HORIZON_EN.get(prof.get("horizon"), "5+ year")
    out = (
        f"The signed-in user describes themselves as {risk} investor with a "
        f"{horizon} time horizon. "
    )
    focus = [_FOCUS_EN[f] for f in prof.get("focus", []) if f in _FOCUS_EN]
    if focus:
        out += "They focus on " + _join_en(focus) + ". "
    cons = [_CONSTRAINT_EN[c] for c in prof.get("constraints", []) if c in _CONSTRAINT_EN]
    if cons:
        out += "Always respect these constraints: " + _join_en(cons) + ". "
    notes = (prof.get("notes") or "").strip()
    if notes:
        out += f"Additional context from the user: {notes} "
    return out


def _system_prompt(skill_ids: list[str] | None = None) -> str:
    """Persona (from the user's profile) + the current view + a live snapshot
    of the account's book + the analysis frameworks chosen for this turn."""
    return (
        "You are a concise investing assistant embedded in a personal stock "
        "tracker. " + _persona() + "You are not a licensed financial advisor: "
        "give analysis and trade-offs, not directives, and flag when something "
        "needs the user's own judgement. The context below is current as of "
        "this message; treat the figures as the user's real position, and let "
        "the current view guide what they are most likely asking about.\n\n"
        f"{_view_context()}"
        f"{_portfolio_context()}"
        + chat_skills.skills_block(skill_ids or [])
    )


# ------------------------------------------------------------- conversation

# How many past messages to actually send the model per request. The full thread
# stays on screen and on disk; only this tail is re-sent, so cost stops growing
# quadratically with conversation length. ~10 exchanges of memory.
_MAX_CONTEXT_MSGS = 20


def _recent(history: list[dict]) -> list[dict]:
    """The tail of the conversation sent to the model. Trims any leading
    assistant turn so the slice still opens with a user message (Anthropic
    requires it; the others don't care). Rebuilt as bare role/content dicts —
    stored turns carry extra keys (e.g. "skills") the provider APIs reject."""
    msgs = history[-_MAX_CONTEXT_MSGS:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def render_conversation(ns: str, provider: llm.Provider, model: str, api_key: str) -> None:
    """Draw the history, take input, and stream the next answer.

    ns namespaces the widget keys so two surfaces can coexist. The
    account-scoped history is hydrated from disk on first touch this session
    (auth.load_chat) and written back after every turn (auth.save_chat), so it
    survives a reload, a new session, or an ephemeral redeploy.
    """
    hist_key = f"chat_history::{auth.watchlist_path()}"
    if hist_key not in st.session_state:
        st.session_state[hist_key] = auth.load_chat()
    history: list[dict] = st.session_state[hist_key]

    # The message list is its own fixed-height scroll region so older turns stay
    # reachable (a plain container would just grow past the panel and clip).
    # autoscroll keeps the newest turn in view; the input renders below it. The
    # concrete height is a fallback — CSS (.st-key-<ns>_scroll) stretches it to
    # fill the panel. New turns are written back into box so they render in place.
    box = st.container(height=460, border=False, autoscroll=True, key=f"{ns}_scroll")
    with box:
        for msg in history:
            with st.chat_message(msg["role"]):
                if msg.get("skills"):  # which lens produced this answer
                    st.caption(_lens_label(msg["skills"]))
                st.markdown(msg["content"])

    if prompt := st.chat_input(tr("chat.placeholder"), key=f"{ns}_input"):
        history.append({"role": "user", "content": prompt})
        with box, st.chat_message("user"):
            st.markdown(prompt)

    # Generate whenever the last turn is a user turn still awaiting a reply
    # (covers both a fresh message and a Regenerate through one code path).
    if history and history[-1]["role"] == "user":
        if provider.id == "free" and not _spend_free_quota():
            history.pop()  # drop the turn we won't answer
            auth.save_chat(history)
            st.error(tr("chat.free_cap", cap=_free_daily_cap()))
            st.stop()
        with box, st.chat_message("assistant"):
            try:
                # Spinner covers the skill routing plus the wait for the first
                # token (write_stream shows nothing until the model responds);
                # it clears as text streams in.
                with st.spinner(tr("chat.thinking")):
                    skills = _resolve_skills(provider, api_key, history)
                    if skills:
                        st.caption(_lens_label(skills))
                    answer = st.write_stream(
                        provider.stream(api_key, model, _system_prompt(skills),
                                        _recent(history))
                    )
            except Exception as exc:  # classified per provider; unknown -> re-raise
                err = provider.error_key(exc)
                if err is None:
                    raise
                history.pop()  # drop the unanswered user turn
                auth.save_chat(history)
                st.error(tr(err, provider=provider.label))
                st.stop()
        turn: dict = {"role": "assistant", "content": answer}
        if skills:
            turn["skills"] = skills
        history.append(turn)
        auth.save_chat(history)  # persist the completed user+assistant turn

    if history and history[-1]["role"] == "assistant":
        with box, st.container(horizontal=True):
            if st.button(tr("chat.regenerate"), icon=":material/refresh:",
                         key=f"{ns}_regen"):
                history.pop()
                auth.save_chat(history)
                st.rerun()
            if st.button(tr("chat.clear"), icon=":material/delete_sweep:",
                         key=f"{ns}_clear"):
                history.clear()  # same list object as session_state — stays empty
                auth.save_chat(history)
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
  position: fixed; top: 0.6rem; right: 1rem; z-index: 1000000;
  width: max-content !important; min-width: 0 !important;
}
/* Desktop: centered in the 4rem breadcrumb bar, same 36px height as the
   search field beside it. */
@media (min-width: 641px) {
  .st-key-chatfab { top: 14px; }
}
/* Design's topbar AI button: square-ish 8px radius, brand purple 600 fill,
   purple glow shadow, purple 700 on hover. */
.st-key-chatfab button {
  border-radius: 8px; width: 36px; height: 36px; padding: 0;
  box-shadow: 0px 4px 12px rgba(127, 63, 232, 0.25);
  background: #7F3FE8 !important;
  border-color: #7F3FE8 !important; color: #FEFEFF !important;
}
.st-key-chatfab button:hover {
  background: #6A2EBF !important;
  border-color: #6A2EBF !important; color: #FEFEFF !important;
}
.st-key-chatfab button * { color: #FEFEFF !important; }
.st-key-chatpanel {
  position: fixed; top: 0; right: 0; bottom: 0;   /* full height */
  /* width driven by the --chat-w var (set live by the width slider), never
     wider than the viewport. */
  width: min(var(--chat-w, 380px), 100vw); z-index: 1000000;
  background: #18161C; border-left: 1px solid #3B3942;
  padding: 0.75rem 1rem 1rem; overflow: hidden;
  box-shadow: -10px 0 30px rgba(0, 0, 0, 0.35);
  display: flex; flex-direction: column;
}
/* Drag-to-resize: a grab strip on the panel's left edge. The JS in
   render_side_panel updates --chat-w live while dragging and persists the last
   width to localStorage. Hidden on phones, where the panel is full-width. */
.st-key-chatpanel .chat-resize-handle {
  position: absolute; left: 0; top: 0; bottom: 0; width: 7px;
  cursor: ew-resize; z-index: 1000001;
}
.st-key-chatpanel .chat-resize-handle::before {
  content: ""; position: absolute; left: 2px; top: 50%;
  transform: translateY(-50%); width: 3px; height: 44px; border-radius: 3px;
  background: #3B3942; transition: background 0.15s;
}
.st-key-chatpanel .chat-resize-handle:hover::before { background: #A98EF7; }
@media (max-width: 640px) { .st-key-chatpanel .chat-resize-handle { display: none; } }
/* Panel open = full-height drawer, so it would cover the fixed top-bar search
   (widgets.py .st-key-topbar_search) pinned top-right. Pull the search left of
   the panel — one gap past its live width — so the whole top strip (breadcrumb
   left, search, then the panel with its own Close) reads as one row. */
body:has(.st-key-chatpanel) .st-key-topbar_search {
  right: calc(min(var(--chat-w, 380px), 100vw) + 1rem) !important;
}
/* Stretch the whole chain from the panel down to the message region so the
   conversation fills every pixel between the title row and the input — no
   guessed height, no dead space. Only ancestors of the scroll region grow;
   the title, settings, input and regenerate button keep their natural size,
   so the input pins to the panel's foot. */
.st-key-chatpanel *:has(.st-key-panel_scroll) {
  display: flex !important; flex-direction: column;
  flex: 1 1 auto; min-height: 0;
}
.st-key-panel_scroll { flex: 1 1 auto; min-height: 0; height: auto !important; }
.st-key-chatpanel [data-testid="stChatInput"] {
  background: #18161C; padding-top: 0.4rem;
}
/* Conversation palette. Streamlit's default avatars borrow the market-semantic
   redColor (user) and orangeColor (assistant) tokens — a pink face and an
   orange robot that read as "loss"/"alert" and clash with the brand. Recolour
   to the purple family (assistant = branded gradient like the launcher FAB,
   user = quiet navy) and give each turn a rounded, tinted bubble. */
.st-key-chatpanel [data-testid="stChatMessage"] {
  background: transparent; gap: 0.6rem; padding: 0.15rem 0;
}
.st-key-chatpanel [data-testid="stChatMessageAvatarAssistant"] {
  background: linear-gradient(135deg, #A98EF7, #6A2EBF) !important;
  box-shadow: 0 2px 8px rgba(127, 63, 232, 0.4);
}
.st-key-chatpanel [data-testid="stChatMessageAvatarUser"] {
  background: #28262D !important; border: 1px solid #3B3942;
}
.st-key-chatpanel [data-testid="stChatMessageAvatarAssistant"] * { color: #FEFEFF !important; }
.st-key-chatpanel [data-testid="stChatMessageAvatarUser"] * { color: #C6B7FB !important; }
.st-key-chatpanel [data-testid="stChatMessageContent"] {
  border-radius: 14px; padding: 0.55rem 0.85rem;
}
.st-key-chatpanel [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"])
  [data-testid="stChatMessageContent"] {
  background: #28262D; border: 1px solid #3B3942;
}
.st-key-chatpanel [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
  [data-testid="stChatMessageContent"] {
  background: rgba(127, 63, 232, 0.16); border: 1px solid rgba(127, 63, 232, 0.35);
}
/* Wide screens: reserve room so page content never hides under the panel, but
   never surrender more than 60% of the width to it (tracks --chat-w). */
@media (min-width: 1100px) {
  body:has(.st-key-chatpanel) .block-container {
    padding-right: calc(min(var(--chat-w, 380px), 60vw) + 20px);
  }
}
/* Phones: the panel fills the viewport (over the header too) and the page keeps
   its width underneath — no room reserved. Between 640px and 1100px the panel
   floats at its chosen width as an overlay. */
@media (max-width: 640px) {
  .st-key-chatpanel { top: 0; width: 100vw; border-left: none; }
}
</style>
"""


# Drag-to-resize. st.html is NOT iframed, so this runs in the top document and
# can reach the fixed panel and listen for mouse moves anywhere on the page (an
# iframe component could not — the pointer leaves the frame mid-drag). The panel
# is glued to the right edge, so width = innerWidth - cursorX. Clamped, applied
# live to --chat-w (which drives the panel, the search offset and the page
# padding), and the release value is remembered per-browser in localStorage.
_RESIZE_JS = f"""
<script>
(function() {{
  const MIN = {_MIN_WIDTH}, MAX = {_MAX_WIDTH};
  const root = document.documentElement;
  const saved = parseInt(localStorage.getItem('chatPanelWidth'), 10);
  if (saved) root.style.setProperty('--chat-w', saved + 'px');

  // Bind the document-level listeners exactly once — st.html re-runs on every
  // full rerun, and the panel is created/destroyed on open/close.
  const S = window.__chatResize || (window.__chatResize = {{ dragging: false }});
  if (!S.bound) {{
    S.bound = true;
    document.addEventListener('mousemove', function(e) {{
      if (!S.dragging) return;
      let w = window.innerWidth - e.clientX;
      w = Math.max(MIN, Math.min(MAX, Math.min(w, window.innerWidth)));
      root.style.setProperty('--chat-w', w + 'px');
    }});
    document.addEventListener('mouseup', function() {{
      if (!S.dragging) return;
      S.dragging = false;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      const px = parseInt(getComputedStyle(root).getPropertyValue('--chat-w'), 10);
      if (px) localStorage.setItem('chatPanelWidth', px);
    }});
  }}

  function ensureHandle() {{
    const panel = document.querySelector('.st-key-chatpanel');
    if (!panel) return false;
    if (panel.querySelector('.chat-resize-handle')) return true;
    const h = document.createElement('div');
    h.className = 'chat-resize-handle';
    h.addEventListener('mousedown', function(e) {{
      S.dragging = true;
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'ew-resize';
      e.preventDefault();
    }});
    panel.appendChild(h);
    return true;
  }}
  // The panel may not be in the DOM yet on this pass; retry briefly, then stop.
  if (!ensureHandle()) {{
    const iv = setInterval(function() {{ if (ensureHandle()) clearInterval(iv); }}, 120);
    setTimeout(function() {{ clearInterval(iv); }}, 3000);
  }}
}})();
</script>
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

    if provider.needs_key and not active_key(provider):
        # Setup mode: choose a provider and enter its key, right here.
        provider = _pick_provider("panel_provider_setup")
        if provider.needs_key:
            _key_gate(provider)  # renders the form; reruns the fragment on submit
            return
        st.rerun()  # keyless provider picked — drop the setup form

    # Configured: settings out of the way, conversation front and centre.
    with st.expander(tr("chat.settings"), expanded=False):
        provider = _pick_provider("panel_provider")
        if len(provider.models) > 1:
            _pick_model(provider, f"panel_model_{provider.id}")  # persists the choice
        _pick_skills()
        if provider.needs_key and st.button(
            tr("chat.forget"), icon=":material/logout:", key="panel_forget"
        ):
            _forget_key(provider.id)
            st.rerun()

    key = active_key(provider)  # provider may have just switched in the expander
    if provider.needs_key and not key:
        _key_gate(provider)  # newly-selected provider has no key yet -> prompt
        return
    if not provider.needs_key:
        st.caption(tr("chat.free_note"))
    render_conversation("panel", provider, _active_model(provider), key)


def render_side_panel(view_label: str) -> None:
    """Overlay assistant: launcher icon + slide-in panel. Call from app.py
    BEFORE page.run() — the launcher is position: fixed, so DOM order doesn't
    matter, and rendering first keeps it alive when a page raises or calls
    st.stop(). Every page, signed-in users only."""
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
    # Emitted after the panel exists so the handle can attach. Outside the
    # fragment, so sending a chat message does not re-run this script.
    st.html(_RESIZE_JS, unsafe_allow_javascript=True)
