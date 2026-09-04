"""The assistant surface built on the multi-provider registry (web/llm.py).

The whole assistant lives in one *side panel* — a launcher icon pinned top-right
that opens a slide-in overlay, reachable from every page. It is fully
self-contained: provider choice, model choice, BYOK key entry (with the same
encrypted, sliding-90-day storage the app uses elsewhere) and the conversation
all happen inside the panel, so there is no separate Chat page in the nav. The
keyless "TopStocks AI" provider (llm.py free chain) skips the key gate entirely
and is throttled per account by _spend_free_quota.

Storage is account-scoped: session slot "llm_key::<pid>", prefs "<pid>_key_enc" /
"<pid>_key_saved_at" / "<pid>_key_first_at", the "llm_provider" / "<pid>_model"
choices, and one "chat_history::<watchlist_path>" thread per account.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from cryptography.fernet import Fernet

from stocks import obs
from stocks.chat import agent, engine, market, tokens, toolbox, tools
from stocks.config import load_watchlist
from stocks.portfolio import autodetect, demo, last_import, llm_map, platforms
from stocks.portfolio.ledger import add_many, all_transactions
from stocks.portfolio.validate import known_tickers, validate
from stocks.secrets_env import secret
from stocks.web import (
    auth,
    chat_skills,
    chat_web,
    css,
    llm,
    ratelimit,
    skeletons,
)
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import enriched_positions, ledger_state
from stocks.web.widgets import (
    data_table,
    db_mtime,
    viewer_tz,
)

_TTL = engine.BYOK_TTL  # 90 days, seconds — the shared sliding "remembered" window


# ------------------------------------------------------------- key + provider
# Account-scoped storage: session slot "llm_key::<pid>", prefs "<pid>_key_enc" /
# "<pid>_key_saved_at" / "<pid>_key_first_at", and the "llm_provider" /
# "<pid>_model" choices. Both the
# read side (active_*) and the setup side (pick/gate/save/forget) live here now
# that the panel is the only assistant surface.


def _sk(pid: str) -> str:
    return f"llm_key::{pid}"


def _fernet() -> Fernet | None:
    k = secret("CHAT_ENC_KEY", "chat", "enc_key")
    return Fernet(k) if k else None


def _load_saved_key(pid: str) -> str:
    """Decrypt a remembered provider key if present and inside its window.

    Reading one is also the maintenance point: a live key has its 90-day
    window slid forward (at most once a day) and any expired key anywhere in
    prefs is deleted outright, ciphertext included.
    """
    prefs = auth.load_prefs()
    key = engine.decrypt_byok(prefs, pid)
    if engine.maintain_byok(prefs, pid if key else None):
        auth.save_prefs(prefs)
    return key


def _save_key(pid: str, api_key: str) -> None:
    """Encrypt and persist a provider key (save_prefs mirrors prefs.json).

    A fresh entry restarts both clocks: the sliding window and the absolute
    cap (`_key_first_at`) that the sliding one can never outrun.
    """
    f = _fernet()
    if not f:
        st.warning(tr("chat.no_enc"))
        return
    prefs = auth.load_prefs()
    now = int(time.time())
    enc_k, saved_k, first_k = engine.byok_fields(pid)
    prefs[enc_k] = f.encrypt(api_key.encode()).decode()
    prefs[saved_k] = now
    prefs[first_k] = now
    auth.save_prefs(prefs)


def _mask_key(key: str) -> str:
    """A key rendered as a glance-check: head, ellipsis, tail. Enough to tell
    two keys apart without putting the secret on screen."""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def _key_lifetime(pid: str) -> int | None:
    """Whole days left on the stored key (sliding window vs absolute cap,
    whichever runs out first), or None when nothing is stored."""
    prefs = auth.load_prefs()
    enc_k, saved_k, first_k = engine.byok_fields(pid)
    if not prefs.get(enc_k):
        return None
    try:
        saved = float(prefs.get(saved_k, 0) or 0)
        first = float(prefs.get(first_k, saved) or saved)
    except (TypeError, ValueError):
        return None
    now = time.time()
    left = min(engine.BYOK_TTL - (now - saved), engine.BYOK_MAX_AGE - (now - first))
    return max(0, int(left // 86400))


def _show_key(provider: llm.Provider, key: str) -> None:
    """The configured key, masked by default with an opt-in reveal, plus where
    it lives — stored (with days left) or session-only."""
    if st.toggle(tr("chat.key_show"), key=f"panel_key_show_{provider.id}"):
        st.code(key, language=None, wrap_lines=True)
    else:
        st.code(_mask_key(key), language=None)
    days = _key_lifetime(provider.id)
    st.caption(tr("chat.key_stored", days=days) if days is not None
               else tr("chat.key_session_only"))


def _forget_key(pid: str) -> None:
    st.session_state.pop(_sk(pid), None)
    prefs = auth.load_prefs()
    enc_k, saved_k, first_k = engine.byok_fields(pid)
    if prefs.pop(enc_k, None) is not None:
        prefs.pop(saved_k, None)
        prefs.pop(first_k, None)
        auth.save_prefs(prefs)


def _offered_providers() -> list[llm.Provider]:
    """Providers this account may actually pick.

    The keyless chain is the operator's money, so an account the free policy
    does not cover (engine.free_eligible) is not shown it — offering a
    provider whose every turn will be refused is worse than not offering it.
    BYOK providers are always listed: those spend the user's own key.
    """
    provs = llm.available_providers()
    if engine.free_eligible(auth.load_prefs()):
        return provs
    return [p for p in provs if p.id != "free"] or provs


def active_provider() -> llm.Provider | None:
    """The provider the user last chose (session > prefs > default), or None
    when no provider SDK is installed."""
    provs = _offered_providers()
    if not provs:
        return None
    pid = (
        st.session_state.get("llm_provider")
        or auth.load_prefs().get("llm_provider")
        or llm.default_provider_id()
    )
    p = llm.PROVIDERS.get(pid)
    return p if (p and p in provs) else provs[0]


def active_key(provider: llm.Provider) -> str:
    return st.session_state.get(_sk(provider.id)) or _load_saved_key(provider.id)


# Free-chain daily allowance: constants and counter logic live in the shared
# engine (stocks/chat/engine.py) so the web panel and the Telegram bot spend
# from the same per-account pot ("free_msgs::<date>" in prefs).
def _free_daily_cap() -> int:
    """This account's allowance today — the reduced one while it is new."""
    return engine.free_daily_cap(auth.load_prefs())


def _spend_free_quota() -> bool:
    """Consume one unit of today's free allowance; False when it's spent."""
    prefs = auth.load_prefs()
    if not engine.spend_free_quota(prefs):
        return False
    auth.save_prefs(prefs)
    return True


def _refund_free_quota(units: int) -> None:
    """Hand back units a turn spent without ever producing an answer.

    Prefs are re-read rather than reused: the spend that is being undone saved
    them, so the copy on disk is the current one.
    """
    if units <= 0:
        return
    prefs = auth.load_prefs()
    engine.refund_free_quota(prefs, units)
    auth.save_prefs(prefs)


def _cap_message() -> str:
    """The refusal for the wall a spend actually hit.

    Three walls guard the free chain and they fail differently: this account's
    allowance resets tomorrow, the shared pot is everyone's and may be back
    within the hour, and an ineligible account never had an allowance at all.
    Naming the account cap for either of the others tells the reader they spent
    messages they never sent.
    """
    prefs = auth.load_prefs()
    reason = engine.free_cap_reason(prefs)
    return tr(engine.FREE_CAP_ERRORS[reason],
              cap=engine.free_daily_cap(prefs), full=engine.free_daily_cap())


def _active_model(provider: llm.Provider) -> str:
    """The model chosen for this provider (session > prefs > default)."""
    return (
        st.session_state.get(f"llm_model::{provider.id}")
        or auth.load_prefs().get(f"{provider.id}_model")
        or provider.default_model
    )


_DEFAULT_WIDTH = 380  # px; must match the .st-key-chatpanel fallback in the CSS
_MIN_WIDTH, _MAX_WIDTH = 320, 1500  # clamp range for the drag-to-resize handle


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

    st.caption(tr("chat.byok_help", provider=provider.label))
    # A form, so Enter in the field and the button below it are the same
    # action. It used to save on any rerun that found text in the box, which
    # left the screen with no visible way to commit.
    with st.form(f"panel_key_form_{provider.id}", border=False):
        entered = st.text_input(
            tr("chat.key_label", provider=provider.label),
            type="password",
            placeholder=provider.key_placeholder,
            help=tr("chat.key_help"),
            key=f"panel_key_{provider.id}",
        )
        remember = st.checkbox(tr("chat.remember"),
                               key=f"panel_remember_{provider.id}")
        st.markdown(f"[{tr('chat.get_key')}]({provider.console_url})")
        submitted = st.form_submit_button(tr("chat.byok_save"), type="primary",
                                          width="stretch")
    st.caption(tr("chat.byok_reassure"))
    if submitted and entered.strip():
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
_SKILL_MODE_KEY = "panel_skills_mode"


def _skill_mode(prefs: dict) -> str:
    """The mode in force right now — the picker's live value, else prefs.

    The chip that opens the picker renders *before* the picker's body, so a
    chip label read off prefs was one rerun stale: the press that switched the
    control to Auto reran the script, the chip drew from the prefs file the
    picker had not written yet, and the popover said "Auto" over a chip that
    still said "Off". Reading the widget's own state first makes both agree
    within the same rerun; prefs answer only before the picker has ever run.
    """
    mode = st.session_state.get(_SKILL_MODE_KEY)
    if mode not in _SKILL_MODES:
        mode = prefs.get("chat_skills_mode", "auto")
    return mode if mode in _SKILL_MODES else "auto"


def _skill_label(sid: str) -> str:
    return tr(f"chat.skill.{sid}")


def _lens_label(ids: list[str]) -> str:
    return tr("chat.lens", skills=" · ".join(_skill_label(i) for i in ids))


def _pick_skills() -> None:
    """Skill mode + manual selection; remembers both in prefs.

    The manual picker is a chip grid, not a multiselect: chips show every lens
    at once inside the popover that opened them, and a press costs one rerun —
    where the widget it replaces was tall enough to push the composer off
    screen in a 380px drawer.
    """
    prefs = auth.load_prefs()
    # Seed the widget's state instead of passing `default=`, which a keyed
    # control honours on its first render only. A segmented control clears
    # itself when the selected option is pressed again, and that press's own
    # rerun arrives here with None in state — so the saved mode snaps back
    # highlighted rather than leaving the row blank with a chip that still
    # names a mode.
    if st.session_state.get(_SKILL_MODE_KEY) not in _SKILL_MODES:
        st.session_state[_SKILL_MODE_KEY] = _skill_mode(prefs)
    st.segmented_control(
        tr("chat.skills_mode"),
        _SKILL_MODES,
        format_func=lambda m: tr(f"chat.skills_{m}"),
        key=_SKILL_MODE_KEY,
    )
    mode = _skill_mode(prefs)
    if mode != prefs.get("chat_skills_mode"):
        prefs["chat_skills_mode"] = mode
        auth.save_prefs(prefs)
    if mode == "auto":
        st.caption(tr("chat.skills_auto_hint"))
    if mode != "manual":
        return
    ids = [s.id for s in chat_skills.catalog()]
    picked = [i for i in prefs.get("chat_skills", []) if i in ids]
    # At the cap, the lenses that are off go disabled rather than silently
    # refusing the press: the limit is the router's, and it has to be visible.
    full = len(picked) >= chat_skills.MAX_MANUAL
    chips = st.container(horizontal=True, key="panel_skillchips")
    for sid in ids:
        on = sid in picked
        blocked = full and not on
        if chips.button(_skill_label(sid), key=f"panel_skill_{sid}",
                        type="primary" if on else "secondary", disabled=blocked,
                        help=tr("chat.skills_full", n=chat_skills.MAX_MANUAL)
                        if blocked else None):
            prefs["chat_skills"] = ([s for s in picked if s != sid] if on
                                    else picked + [sid])
            auth.save_prefs(prefs)
            st.rerun()


def _resolve_skills(provider: llm.Provider, api_key: str, history: list[dict],
                    prefs: dict, context: str) -> list[str]:
    """Skill ids to apply to the pending answer, per the saved mode.

    Auto routes the message through the provider's cheapest model (for the
    keyless free chain that is one extra backend call per message). When the
    router call itself fails it falls back to the previous answer's skills —
    the answer always proceeds, and an unchanged skill set keeps the system
    prompt byte-identical, which keeps provider prompt caches warm.

    `prefs` and `context` are passed in rather than read here: this runs off
    the script thread (engine.in_parallel), where session state is gone."""
    return engine.resolve_skills(prefs, provider, api_key, history,
                                 context=context)


# ------------------------------------------------------------- web search
# Keyless DuckDuckGo search (web/chat_web.py): a planner call on the provider's
# cheapest model decides per message whether the web is needed and with which
# queries — the same one-extra-cheap-call shape as the skill auto-router.


def _gather_web(provider: llm.Provider, api_key: str, history: list[dict],
                prefs: dict, context: str) -> list[chat_web.Result]:
    """The pages this turn reads: the planner's searches, opened and read,
    plus any link the user pasted.

    [] when the web toggle is off — that means no internet at all, pasted
    links included. Like the skill router, this runs off the script thread,
    so `prefs` and the view context arrive as arguments."""
    return engine.ground_web(prefs, provider, api_key, history, context)


def _gather(provider: llm.Provider, api_key: str, msgs: list[dict], prefs: dict,
            watchlist: Path, db: Path, memory_db: Path, thread: str,
            focus: str) -> agent.Evidence:
    """The model-directed lookup for this turn (chat/agent.py).

    Gated by the same "chat_web" toggle as the fixed pre-flight: the tools can
    reach the internet, so a user who turned the web off must not get it back
    through the side door. Off means Evidence(ok=False), which puts the turn on
    the fixed path — where the toggle is honoured too.

    Runs off the script thread, so the account's paths and the current view
    arrive as arguments rather than being read from session state.
    """
    if not engine.web_enabled(prefs):
        return agent.Evidence(ok=False)
    return agent.gather(provider, api_key, msgs, toolbox.Context(
        watchlist=watchlist, db=db, memory_db=memory_db, thread=thread,
        focus=focus))


def _live_quotes(message: str, watchlist: Path, focus: str) -> list[market.Quote]:
    """Live prices for the tickers this message names (chat/market.py).

    Deterministic and model-free: the book snapshot in the system prompt only
    covers what the user holds, so anything else — a ticker they are merely
    looking at, a name they typed — would otherwise be answered from the
    model's training-data prices."""
    return market.lookup_for(message, watchlist, focus=focus)


def _host(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") or url


# ------------------------------------------------------------- actions
# App operations straight from chat (chat/tools.py): favorite, price
# alerts, groups. Detection only runs when the keyword gate hits; a detected
# action replaces the LLM answer with a deterministic localized confirmation
# (no free-quota spend), and any failure falls through to a normal answer.


def _action_context() -> str:
    """What the action parser needs: current view (resolves "this"), the
    watchlist (resolves company names to symbols) and existing groups."""
    bits = [_view_context().strip()]
    holds = load_watchlist(auth.watchlist_path())
    if holds:
        bits.append("Watchlist: " + ", ".join(
            f"{h.ticker} ({h.name})" if h.name else h.ticker for h in holds))
    tags = auth.all_tags()
    if tags:
        bits.append("Existing groups: " + ", ".join(tags))
    return "\n".join(b for b in bits if b)


def _action_reply(act: tools.Action) -> str:
    """Localized confirmation bubble for an executed action — the per-tool
    wording lives with the tools (chat/tools.py)."""
    return tools.reply(act, tr)


def _try_action(provider: llm.Provider, api_key: str,
                message: str) -> tools.Action | None:
    """Detect and execute an app action for the message, or None.

    None on gate miss, parse failure or execution error — the caller then
    answers normally, so a broken action path never blocks the chat."""
    if not tools.maybe_action(message):
        return None
    act = tools.detect(provider, api_key, message, _action_context())
    if act is None:
        return None
    try:
        tools.execute(act, auth.watchlist_path())
    except Exception:
        return None
    return act


def _retire_on_first_chunk(work, chunks):
    """Yield `chunks`, retiring the working line as the first one lands.

    st.write_stream renders nothing until the provider returns its first
    token, so the working line has to survive *into* the streaming call and be
    retired from inside it — clearing beforehand would leave the bubble blank
    for exactly the wait it exists to cover. The finally covers a stream that
    ends without yielding anything at all.
    """
    try:
        for i, chunk in enumerate(chunks):
            if i == 0:
                work.clear()
            yield chunk
    finally:
        work.clear()


def _stream_with_fallback(work, provider: llm.Provider, api_key: str,
                          model: str, system: str, msgs: list[dict],
                          prefs: dict, *, spent: list[int] | None = None) -> str:
    """The answer stream, retried down the provider chain when the chosen
    provider dies: chosen first, then the other saved keys, then the keyless
    free chain — the same resolution order as the Telegram bot
    (engine.attempts). Before this, one saturated provider (a Gemini 503)
    killed the whole turn even with a healthy chain behind it.

    A later candidate only runs while the bubble is still empty: once a first
    token is on screen, switching providers would splice two answers into one
    bubble, so a mid-answer failure propagates as before. The provider that
    actually raised rides on the exception (``chat_provider``) so the caller
    classifies and names the right one.

    A free unit spent on a fallback candidate is appended to `spent`, so a
    chain that ends in an exception can be refunded whole by the caller
    rather than only for the unit the turn opened with.
    """
    cands = [(provider, api_key, model or provider.default_model)]
    for p, k, m in engine.attempts(prefs):
        if p.id != provider.id:
            cands.append((p, k, m or p.default_model))
    last_exc: Exception | None = None
    for i, (p, k, m) in enumerate(cands):
        # The chosen free provider's quota is spent by the caller before the
        # turn starts; a fallback into the free chain spends here, and a spent
        # cap just skips the candidate — the cap message would bury the real
        # story (the chosen provider failing).
        if i and p.id == "free":
            if not _spend_free_quota():
                continue
            if spent is not None:
                spent.append(1)
        started: list[bool] = []

        def _tap(chunks, seen=started):
            for c in chunks:
                seen.append(True)
                yield c

        try:
            # write_stream hands back a list when a chunk isn't a string;
            # every provider here yields text, so join rather than branch.
            streamed = st.write_stream(_retire_on_first_chunk(
                work, _tap(p.stream(k, m, system, msgs))))
            answer = (
                streamed if isinstance(streamed, str)
                else "".join(str(c) for c in streamed)
            )
        except Exception as exc:
            try:
                exc.chat_provider = p  # type: ignore
            except Exception as exc2:
                obs.warn("chat.core.provider_annotate_failed",
                         error_type=type(exc2).__name__,
                         error=str(exc2)[:300])
            if started or i == len(cands) - 1:
                raise
            last_exc = exc
            obs.warn("chat.provider_fallthrough", provider=p.id, model=m,
                     error_type=type(exc).__name__, error=str(exc)[:300])
            continue
        if i:
            st.caption(tr("chat.fallback_note", provider=p.label,
                          chosen=provider.label))
            obs.event("chat.fallback_answered", provider=p.id,
                      chosen=provider.id)
        return answer
    # Every fallback was skipped (free cap spent) — surface the chosen
    # provider's original failure rather than inventing a new one.
    assert last_exc is not None
    raise last_exc


# ------------------------------------------------------------- context


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
    ccy = auth.reporting_currency()
    tbl = (
        enriched_positions(str(db), db_mtime(str(db)), ccy)
        if db.exists()
        else None
    )
    return engine.book_snapshot(tbl, auth.watchlist_path(), ccy)


def _system_prompt(skill_ids: list[str] | None = None) -> str:
    """Persona (from the user's profile) + the current view + a live snapshot
    of the account's book + the analysis frameworks chosen for this turn.
    Assembled by the shared engine (stocks/chat/engine.py) — the Telegram bot
    builds the same prompt from the same pieces."""
    return engine.system_prompt(
        auth.load_profile(), _view_context() + _portfolio_context(), skill_ids
    )


# ------------------------------------------------------------- threads
# Several conversations per account (auth.load_book / list_conversations): a
# bar above the messages with a picker popover — named after the open thread —
# and a New button. All of it lives inside the panel fragment, so switching
# threads reruns the panel and nothing else.


def _hist_key(conv: dict) -> str:
    """Session slot holding a thread's turns: per account, per conversation.

    Keyed by thread as well as account so switching conversations cannot
    redraw the previous one's cached list.
    """
    return f"chat_history::{auth.watchlist_path()}::{conv['id']}"


def _conv_label(conv: dict, limit: int = 30) -> str:
    """A thread's display name: its title, or a placeholder while unnamed."""
    title = (conv.get("title") or "").strip() or tr("chat.untitled")
    return title if len(title) <= limit else title[: limit - 1] + "\u2026"


# The drawer shows one of three things and never two at once: the thread, the
# thread list, or the account settings. One session key decides which, because
# everything else used to stack on top of the conversation — the settings
# expander pushed it down, the thread popover covered it — and a swap costs a
# single fragment rerun.

_DRAWER_VIEWS = ("thread", "threads", "settings")


def _drawer_view() -> str:
    view = st.session_state.get("chat_drawer_view", "thread")
    return view if view in _DRAWER_VIEWS else "thread"


def _open_view(view: str) -> None:
    """Swap the drawer's body and repaint the panel (only the panel)."""
    st.session_state["chat_drawer_view"] = view
    st.rerun()


def _thread_when(conv: dict) -> str:
    """Row stamp: the time today, the weekday this week, the date before that."""
    try:
        when = datetime.fromisoformat(conv["updated"]).astimezone()
    except (KeyError, TypeError, ValueError):
        return ""
    days = (datetime.now().astimezone().date() - when.date()).days
    if days <= 0:
        return when.strftime("%H:%M")
    return when.strftime("%a" if days < 7 else "%d %b")


def _thread_group(conv: dict) -> str:
    """The heading a thread sits under: today, this week, or its month."""
    try:
        when = datetime.fromisoformat(conv["updated"]).astimezone()
    except (KeyError, TypeError, ValueError):
        return ""
    days = (datetime.now().astimezone().date() - when.date()).days
    if days <= 0:
        return tr("chat.group_today")
    if days < 7:
        return tr("chat.group_week")
    return when.strftime("%B").upper()


def _render_thread_row(ns: str, c: dict) -> None:
    """One row of the list: pick it, rename it, or confirm deleting it.

    Rename and delete take over the row itself rather than opening anything:
    the drawer is 380px, and a dialog over a list of threads hides the very
    titles the reader is deciding between.
    """
    cid = c["id"]
    if st.session_state.get(f"{ns}_renaming") == cid:
        name = st.text_input(tr("chat.rename"), value=c["title"],
                             key=f"{ns}_rename_{cid}", label_visibility="collapsed")
        with st.container(horizontal=True):
            if st.button(tr("chat.rename_save"), type="primary",
                         key=f"{ns}_save_{cid}"):
                auth.rename_conversation(cid, name)
                st.session_state.pop(f"{ns}_renaming", None)
                st.rerun()
            if st.button(tr("chat.cancel"), key=f"{ns}_cancel_{cid}"):
                st.session_state.pop(f"{ns}_renaming", None)
                st.rerun()
        return

    if st.session_state.get(f"{ns}_deleting") == cid:
        # Deleting a thread drops its turns and its memory index with it, so
        # the count is named: "9 messages" is the fact that decides it.
        with st.container(key=f"{ns}_confirm"):
            st.markdown(tr("chat.delete_confirm",
                           title=_conv_label(c, 24), n=c["messages"]))
            with st.container(horizontal=True):
                if st.button(tr("chat.delete_yes"), icon=":material/delete:",
                             key=f"{ns}_delyes_{cid}"):
                    auth.delete_conversation(cid)
                    st.session_state.pop(f"{ns}_deleting", None)
                    st.rerun()
                if st.button(tr("chat.cancel"), key=f"{ns}_delno_{cid}"):
                    st.session_state.pop(f"{ns}_deleting", None)
                    st.rerun()
        return

    # The open thread keeps a fixed container key so the CSS can mark it the
    # way the sidebar marks the current page — it stays pressable (re-picking
    # the open thread is simply a way back to it from here).
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_row_active" if c["active"] else f"{ns}_row_{cid}"):
        meta = tr("chat.thread_meta", when=_thread_when(c), n=c["messages"])
        if st.button(f"{_conv_label(c, 26)}  \n{meta}", key=f"{ns}_pick_{cid}",
                     type="tertiary", width="stretch"):
            auth.set_active_conversation(cid)
            _open_view("thread")
        with st.popover("", icon=":material/more_vert:", key=f"{ns}_menu_{cid}",
                        help=tr("chat.thread_menu")):
            if st.button(tr("chat.rename"), icon=":material/edit:",
                         type="tertiary", key=f"{ns}_ren_{cid}"):
                st.session_state[f"{ns}_renaming"] = cid
                st.rerun()
            if st.button(tr("chat.delete_thread"), icon=":material/delete:",
                         type="tertiary", key=f"{ns}_del_{cid}"):
                st.session_state[f"{ns}_deleting"] = cid
                st.rerun()


def _render_threads_view(ns: str) -> None:
    """The drawer's body, swapped for the thread list.

    A search field and date groups need width, which is why this replaces the
    conversation instead of hovering over it in a popover: at the drawer's
    width a popover could show a title and nothing that helps choose between
    two of them.
    """
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_threadhead"):
        if st.button(tr("chat.back"), icon=":material/chevron_left:",
                     type="tertiary", key=f"{ns}_threads_back"):
            _open_view("thread")
        if st.button(tr("chat.new"), icon=":material/add:", type="primary",
                     key=f"{ns}_threads_new"):
            auth.new_conversation()
            _open_view("thread")

    with st.container(key=f"{ns}_view"):
        convs = auth.list_conversations()
        # Client-side filtering: these are tens of threads, and their titles
        # are already in memory from the listing above — an index would cost
        # more than it saves.
        needle = (st.text_input(
            tr("chat.threads_search"), key=f"{ns}_threads_q",
            placeholder=tr("chat.threads_search"), icon=":material/search:",
            label_visibility="collapsed") or "").strip().lower()
        if needle:
            convs = [c for c in convs
                     if needle in (c.get("title") or "").lower()]

        group = ""
        for c in convs:
            heading = _thread_group(c)
            if heading != group:
                group = heading
                st.html(f'<div class="ts-chat-group">{escape(heading)}</div>')
            _render_thread_row(ns, c)



def _maybe_autotitle(conv: dict, history: list[dict], provider: llm.Provider,
                     api_key: str) -> bool:
    """Name a brand-new thread from its opening question (one cheap call).

    True when the title changed, so the caller can rerun and repaint the bar —
    it was drawn before the answer existed. Never fires twice on a thread, and
    never on one the user renamed."""
    if len(history) != 2 or conv.get("title") or not conv.get("title_auto", True):
        return False
    try:
        auth.autotitle_conversation(
            conv["id"], engine.title_for(provider, api_key, history[0]["content"])
        )
    except Exception:
        return False
    return True


# -------------------------------------------------------------- statements
# A broker export dropped into the chat input. autodetect picks the parser —
# or maps the file's columns with one cheap model call when no parser owns it
# — and the rows are then validated and previewed exactly as the Import page
# does. Nothing reaches the ledger until the user presses the button: the
# parsed batch waits in session state, which is also what carries it across
# the fragment reruns the panel does on every interaction.
#
# The uploaded bytes are deliberately never written to disk. An import is
# finished inside the session that started it, so persisting the statement
# (and mirroring it to the bucket) would leave a second copy of the user's
# whole trade history around for no gain; a lost session means re-uploading.

MAX_UPLOAD_MB = 10


def _uploads_key(ns: str) -> str:
    return f"{ns}_uploads"


def _pending_key(ns: str) -> str:
    return f"{ns}_pending_import"


def _seed_key(ns: str) -> str:
    return f"{ns}_seed"


# The opening screen's suggestions, in two sets of three.
#
# With a ledger behind it the assistant's best trick is the reader's own book,
# so those are the questions the design puts on the opening screen. A fresh
# account has no ledger and would be offered three questions it cannot answer,
# so it gets the watchlist set instead: live quotes, fundamentals and the
# earnings calendar all work with no import at all. Those carry {a}/{b} slots
# for two of the account's own tickers, so the suggestion reads as a question
# about *their* list rather than a demo.
_STARTERS = (
    "chat.starter_summary",
    "chat.starter_concentration",
    "chat.starter_earnings_week",
)
_STARTERS_NEW = (
    "chat.starter_movers",
    "chat.starter_compare",
    "chat.starter_earnings",
)


def _position_count() -> int:
    """Open positions in the account's ledger; 0 when there is no ledger yet.

    The share-matching replay is cached on the database's mtime and reads no
    prices, so this is nothing like the live-priced table the system prompt
    builds — cheap enough to run while drawing an empty thread.
    """
    db = auth.db_path()
    if not db.exists():
        return 0
    try:
        positions = ledger_state(str(db), db_mtime(str(db)),
                                 auth.reporting_currency())[1]
    except Exception:  # a half-written or foreign db must not block the panel
        return 0
    return sum(1 for pos in positions if pos.quantity)


def _starter_tickers() -> tuple[str, str]:
    """Two tickers to name in the suggestions: favorites first, then order.

    Falls back to the two the copy reads least oddly with when the watchlist
    is short or empty — a suggestion is only ever a prefilled question, so a
    generic pair is better than hiding the row.
    """
    holdings = load_watchlist(auth.watchlist_path())
    picked = [h.ticker for h in holdings if h.favorite]
    picked += [h.ticker for h in holdings if h.ticker not in picked]
    picked += ["AAPL", "MSFT"]
    return picked[0], picked[1]


def _render_starters(ns: str) -> None:
    """Suggestion chips for an empty thread; each one submits as if typed.

    Written to the same seed key the input reads below, so a chip goes through
    the identical turn pipeline — rate limit, action probe, routing, quota —
    rather than a shortcut that would drift from it.
    """
    a, b = _starter_tickers()
    # Stacked, not wrapped: these are whole questions, so a horizontal
    # container gave each one its own line anyway — with a row gap between
    # them big enough to push the drop zone off screen. The caps label rides
    # inside the same container, so its distance to the first tile is the
    # container's tight gap rather than the page's element spacing.
    row = st.container(key=f"{ns}_starters", gap="xxsmall")
    row.html('<div class="ts-chat-group ts-chat-group-tight">'
             + escape(tr("chat.start_with")) + "</div>")
    for key in (_STARTERS if _position_count() else _STARTERS_NEW):
        prompt = tr(key, a=a, b=b)
        if row.button(prompt, key=f"{ns}_{key}", type="tertiary",
                      width="stretch"):
            st.session_state[_seed_key(ns)] = prompt
            st.rerun()


# The four things worth knowing before the first question. Rows, not cards:
# at the drawer's width cards would push the suggestions off screen, and the
# reader has to be able to see both at once.
#
# Drawn as raw HTML with the artboard's own glyphs rather than as st.markdown
# with :material/…: directives. Three reasons, all found the hard way: the
# icon span Streamlit emits takes its colour from the theme and ignored every
# rule aimed at it, a nested st.container gets picked up by app.py's card
# tagger and grew a card around the list, and a wrapped second line tucked
# under the icon instead of hanging with the text.
_CAP_ICONS = (
    '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"></path>'
    '<path d="M22 12A10 10 0 0 0 12 2v10z"></path>',
    '<circle cx="12" cy="12" r="8.5"></circle>'
    '<path d="M3.5 12h17M12 3.5c3 3.5 3 13.5 0 17M12 3.5c-3 3.5-3 13.5 0 17">'
    "</path>",
    '<path d="M12 3v12"></path><polyline points="7 10 12 15 17 10"></polyline>'
    '<path d="M3 21h18"></path>',
    '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"></path>'
    '<path d="M10 21h4"></path>',
)
_CAPABILITIES = ("chat.cap_analyze", "chat.cap_web", "chat.cap_import",
                 "chat.cap_alerts")


def _glyph(paths: str, size: int = 15) -> str:
    """One of the artboard's line icons, inheriting the row's colour."""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
        f"{paths}</svg>"
    )


def _rich(text: str) -> str:
    """Catalog copy with its **bold** lead, as escaped HTML."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


# The drop zone's glyph. Inline SVG rather than a Material name: this block
# is raw HTML (st.html), where Streamlit's icon directives mean nothing.
_DROP_GLYPH = (
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
    '<path d="M12 3v12"></path><polyline points="7 10 12 15 17 10"></polyline>'
    '<path d="M3 21h18"></path></svg>'
)


def _render_empty_state(ns: str) -> None:
    """The opening screen: what the assistant can do, and three ways in.

    An empty scroll region and a placeholder said nothing — not that the
    assistant can see the reader's own book, not that it reads a pasted link,
    not that a statement dropped on the input becomes an import. The ticker
    count is the line that proves the first claim, so it is counted from the
    account rather than written into the copy.
    """
    # One flex column: it carries the artboard's 18px rhythm between the four
    # blocks, and lets the drop zone sit on the composer instead of floating
    # under the suggestions with the panel's dead space beneath it.
    with st.container(key=f"{ns}_empty"):
        # The count is the line that proves the assistant can see the reader's
        # own data, so it is read from the account: positions when a ledger
        # exists, watchlist tickers when it does not.
        held = _position_count()
        intro = (tr("chat.empty_body", n=held) if held
                 else tr("chat.empty_body_new",
                         n=len(load_watchlist(auth.watchlist_path()))))
        rows = "".join(
            f'<div class="ts-cap">{_glyph(paths)}'
            f"<span>{_rich(tr(key))}</span></div>"
            for paths, key in zip(_CAP_ICONS, _CAPABILITIES, strict=True)
        )
        st.html(
            '<div class="ts-empty-head">'
            f'<span class="ts-empty-title">{escape(tr("chat.empty_title"))}</span>'
            f'<span class="ts-empty-body">{escape(intro)}</span></div>'
            f'<div class="ts-caps">{rows}</div>'
        )
        _render_starters(ns)
        # Drawn, because the drop target has always existed and never looked
        # like one: the input accepts a statement, and nothing said so.
        st.html('<div class="ts-chat-drop">' + _DROP_GLYPH
                + escape(tr("chat.drop_zone", mb=MAX_UPLOAD_MB)) + "</div>")


def _import_hint(ns: str, message: str) -> str | None:
    """The deterministic answer to "import my trades", or None to answer normally.

    An import needs the statement itself, and a model asked to perform one
    narrates the import it did not do instead — invented tickers, invented
    prices, a book reported as changed (which is exactly what shipped). So the
    ask never reaches a model: it is answered here with the step that does
    work, either attaching the file or pressing the button on the batch
    already staged. The same ban is stated in the system prompt for every
    phrasing this gate does not catch.
    """
    if not tools.wants_import(message):
        return None
    staged = st.session_state.get(_pending_key(ns))
    if staged:
        return tr("chat.import_pending_hint", filename=staged["filename"])
    return tr("chat.import_needs_file")


def _failure(history: list[dict]) -> tuple[str, str] | None:
    """The error pinned to a trailing unanswered question, if any.

    A failed answer is recorded on the user turn itself ("error": [key,
    provider]) rather than in session state: it then belongs to its own
    thread, survives a reload, and blocks the generate branch below without a
    second source of truth. engine.recent rebuilds bare role/content dicts,
    so the key never reaches a provider.
    """
    if not history or history[-1]["role"] != "user":
        return None
    err = history[-1].get("error")
    return (err[0], err[1]) if err else None


def _submitted(value) -> tuple[str, list]:
    """(text, files) out of st.chat_input, which returns a bare string only
    when uploads are switched off."""
    if value is None:
        return "", []
    if isinstance(value, str):
        return value, []
    return (value.text or ""), list(value.files or [])


# Number formats for the phone cards of the import previews (desktop keeps
# st.dataframe's own rendering).
_PREVIEW_FMT = {"quantity": "{:,.4f}", "price": "{:,.2f}", "fee": "{:,.2f}"}


def _tx_rows(txs: list) -> list[dict]:
    return [
        {"date": t.date, "ticker": t.ticker, "action": t.action,
         "quantity": t.quantity, "price": t.price, "fee": t.fee,
         "currency": t.currency}
        for t in txs
    ]


def _issue_rows(checked: list) -> list[dict]:
    return [
        {"date": c.tx.date, "ticker": c.tx.ticker, "action": c.tx.action,
         "quantity": c.tx.quantity, "price": c.tx.price,
         "why": "; ".join(i.message for i in (c.errors or c.warnings))}
        for c in checked
    ]


def _prepare_import(name: str, data: bytes, provider: llm.Provider,
                    api_key: str) -> dict:
    """Detect, validate and package one uploaded statement for preview.

    Read-only. Unlike the Import page this passes no live symbol lookup: that
    costs a network round-trip per unknown symbol against an API that already
    rate-limits us, and it can only ever downgrade a warning — never keep a
    bad row out. Unknown symbols are simply shown as warnings.

    The batch staged here is `fresh`, not `importable`: rows the ledger
    already holds are held back rather than imported with a warning the way
    the Import page does. The page shows its warning tier as a full table
    next to a wipe checkbox; a chat bubble shows it folded into an expander,
    which is not a place to put "this doubles your position" and expect it to
    be read. The held-back rows travel along and the preview can opt them in.
    """
    paths = auth.user_paths()
    found = autodetect.detect(name, data, provider, api_key)
    checked = validate(
        found.result,
        # Demo rows go on commit, so checking against them would raise
        # duplicates that will not exist (stocks.portfolio.demo).
        demo.without(all_transactions(paths.db)),
        known=known_tickers(paths.watchlist, paths.db),
    )
    dupes = [c.tx for c in checked.duplicates]
    return {
        "filename": name,
        "label": found.label or tr("chat.import_source_llm"),
        "platform": found.platform,
        "kind": found.kind,
        "unavailable": found.unavailable,
        # The origin the parser already stamped, "" when the file was mapped
        # and nothing in it names a broker — then the user is asked below.
        "broker": platforms.detected_broker(checked.importable),
        "transactions": checked.fresh,
        "rows": _tx_rows(checked.fresh),
        # Warnings worth reading are the ones about rows being committed;
        # the duplicates carry their own tier and their own explanation.
        "flagged": _issue_rows([c for c in checked.flagged if not c.duplicate]),
        "duplicates": _issue_rows(checked.duplicates),
        "duplicate_transactions": dupes,
        "rejected": _issue_rows(checked.rejected),
        "skipped": found.result.skipped,
    }


def _ingest_uploads(ns: str, uploads: list[tuple[str, bytes]],
                    provider: llm.Provider, api_key: str,
                    history: list[dict]) -> None:
    """Turn the just-attached files into a pending import and say so.

    One statement at a time: several at once would need several previews and
    several confirmations, and a second file is far more often the same export
    twice than two different brokers. The rest are named as ignored rather
    than silently dropped.
    """
    name, data = uploads[0]
    # Reading, mapping and validating a statement takes a beat; hold the space
    # as the table it is about to become rather than a spinner (skeletons.py).
    shimmer = skeletons.reserve("table", rows=4, cols=5, title=True)
    try:
        pending = _prepare_import(name, data, provider, api_key)
    finally:
        shimmer.clear()

    n = len(pending["transactions"])
    dupes = len(pending["duplicates"])
    if n or dupes:
        st.session_state[_pending_key(ns)] = pending
        if n and dupes:
            key = "chat.import_found_deduped"
        elif n:
            key = "chat.import_found"
        else:
            # Nothing new at all: the same export a second time. The preview
            # still opens — it is where the repeated rows are listed and
            # where they can be imported anyway.
            key = "chat.import_all_duplicates"
        note = tr(key, filename=name, n=n, dupes=dupes, label=pending["label"])
    elif pending["unavailable"]:
        # The model never answered, so the file was never judged. Telling the
        # user to fix their export here would send them off to do the wrong
        # work entirely.
        note = tr("chat.import_unavailable", filename=name)
    elif pending["kind"] == llm_map.KIND_POSITIONS:
        # A portfolio report: it says what is held today, not how it was
        # bought. There is genuinely nothing to import, so say that and name
        # the export that does carry the movements.
        note = tr("chat.import_positions", filename=name) + "\n\n" + tr(
            "chat.import_positions_help")
    else:
        note = tr("chat.import_none", filename=name) + "\n\n" + tr(
            "chat.import_none_help")
    if len(uploads) > 1:
        note += "\n\n" + tr("chat.import_one_at_a_time",
                             files=", ".join(u[0] for u in uploads[1:]))
    history.append(_stamp(
        {"role": "assistant", "content": note, "action": "import"}))
    auth.save_chat(history)


def _commit_import(ns: str, pending: dict, history: list[dict],
                   broker: str = "", duplicates: bool = False) -> None:
    """Write the previewed batch to the ledger and record it as undoable.

    `broker` is the origin the user named for a file no parser owned; it is
    stamped in front of every note so the Fees and Custody views can place
    these rows (platforms.stamp_broker). `duplicates` adds back the rows the
    ledger already holds — only ever from the preview's own checkbox.
    """
    paths = auth.user_paths()
    txs = list(pending["transactions"])
    if duplicates:
        txs += pending.get("duplicate_transactions") or []
    if broker:
        txs = platforms.stamp_broker(txs, broker)
    # First real import wipes the demo book — an invented cost basis must
    # never end up mixed into a real one.
    demo.clear(paths.db)
    ids = add_many(txs, paths.db)
    last_import.save(
        last_import.ImportRecord(
            filename=pending["filename"],
            imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tx_ids=ids,
            wiped=False,
            platform=pending["platform"],
        ),
        paths.last_import,
    )
    history.append(_stamp({
        "role": "assistant",
        "content": tr("chat.import_done", n=len(ids),
                      total=len(all_transactions(paths.db))) + " " + tr(
                          "chat.import_undo_hint"),
        "action": "import",
    }))
    auth.save_chat(history)
    st.session_state.pop(_pending_key(ns), None)


def _broker_option(key: str) -> str:
    return (tr("chat.import_broker_other") if key == platforms.OTHER
            else platforms.broker_label(key))


def _broker_choice(ns: str, pending: dict) -> str:
    """The origin to stamp on this batch.

    A recognised statement names its own broker, so it is only shown. Anything
    the parsers didn't own has to be told: "" until the user answers, which
    holds the import button — a batch with no origin lands in the ledger
    attributed to whatever its notes happened to start with.
    """
    detected = pending.get("broker")
    if detected:
        st.caption(tr("chat.import_broker_known",
                      broker=platforms.broker_label(detected)))
        return detected
    # accept_new_options: the roster only holds brokers with a parser, and
    # naming the real one beats filing the batch under "other".
    picked = st.selectbox(
        tr("chat.import_broker"),
        platforms.broker_options(),
        index=None,
        format_func=_broker_option,
        placeholder=tr("chat.import_broker_pick"),
        accept_new_options=True,
        help=tr("chat.import_broker_help"),
        key=f"{ns}_import_broker",
    )
    return picked or ""


def _render_pending_import(ns: str, history: list[dict], box) -> bool:
    """The preview card and its two buttons. True while one is waiting."""
    pending = st.session_state.get(_pending_key(ns))
    if not pending:
        return False

    with box, st.chat_message("assistant"):
        st.caption(tr("chat.import_preview", filename=pending["filename"],
                      label=pending["label"]))
        # Seven columns in a chat bubble already crowd a desktop; on a phone
        # they pan, so every preview grid stacks into per-row cards there
        # (the symbol heads the card, the rest read as label/value lines).
        if pending["rows"]:
            data_table(pd.DataFrame(pending["rows"]), title="ticker",
                       fmt=_PREVIEW_FMT, hide_index=True,
                       height=200, width="stretch")
        dupes = pending.get("duplicates") or []
        for key, rows in (("chat.import_warnings", pending["flagged"]),
                          ("chat.import_rejected", pending["rejected"])):
            if rows:
                with st.expander(tr(key, n=len(rows))):
                    data_table(pd.DataFrame(rows), title="ticker",
                               fmt=_PREVIEW_FMT, hide_index=True,
                               width="stretch")
        if pending["skipped"]:
            with st.expander(tr("chat.import_skipped",
                                n=len(pending["skipped"]))):
                data_table(pd.DataFrame(pending["skipped"]),
                           hide_index=True, width="stretch")
        # The duplicates are the one tier that is *not* about to be written,
        # so it opens by itself: the count in the message above only makes
        # sense next to the rows it is talking about. The checkbox is the
        # escape hatch for the honest repeat — two identical fills, a broker
        # that really did pay the same dividend twice.
        include = False
        if dupes:
            with st.expander(tr("chat.import_duplicates", n=len(dupes)),
                             expanded=not pending["rows"]):
                data_table(pd.DataFrame(dupes), title="ticker",
                           fmt=_PREVIEW_FMT, hide_index=True,
                           width="stretch")
                include = st.checkbox(tr("chat.import_duplicates_anyway"),
                                      key=f"{ns}_import_dupes")
        n = len(pending["rows"]) + (len(dupes) if include else 0)
        origin = _broker_choice(ns, pending)
        with st.container(horizontal=True):
            if st.button(tr("chat.import_button", n=n),
                         type="primary", key=f"{ns}_do_import",
                         disabled=not origin or not n):
                _commit_import(ns, pending, history, origin, include)
                st.rerun()
            if st.button(tr("chat.import_cancel"), key=f"{ns}_drop_import"):
                st.session_state.pop(_pending_key(ns), None)
                st.rerun()
    return True


# ------------------------------------------------------- clock and activity
# What the thread shows about time, and about the work behind an answer.
#
# Three pieces, all modelled on Claude Code's terminal: a working line that
# ticks while the answer is being built, the tool lines it leaves behind
# ("what was fetched for this"), and a dim clock under every turn. The clock
# and the elapsed cost are stored on the turn dict, so a reload redraws
# exactly what was on screen; turns written before this shipped carry neither
# and simply show nothing.

_STEP_ARG_CHARS = 56  # of a tool's argument kept on its line
_STEP_ARG_KEYS = ("query", "url", "tickers", "ticker", "symbol")


# The reader's zone lives in widgets — the dashboard's daily card needs it too.
_viewer_tz = viewer_tz


def _clock(ts: float | None) -> str:
    """A turn's stamp as HH:MM in the reader's zone, or '' when unstamped."""
    if not ts:
        return ""
    try:
        when = datetime.fromtimestamp(float(ts), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return when.astimezone(_viewer_tz()).strftime("%H:%M")


def _took(seconds: float | None) -> str:
    """How long an answer took: 8.4s inside a minute, then 1m 12s."""
    if not seconds or seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _stamp(turn: dict, took: float | None = None) -> dict:
    """Stamp a turn on its way onto the thread (answers also get their cost).

    setdefault, not assignment: a turn that already carries a stamp is being
    re-saved, and its clock must keep saying when it was written.
    """
    turn.setdefault("ts", time.time())
    if took is not None:
        turn["took"] = round(took, 1)
    return turn


def _render_meta(msg: dict) -> None:
    """The dim footer under a turn: its clock, and for an answer its cost."""
    bits = [b for b in (_clock(msg.get("ts")), _took(msg.get("took"))) if b]
    if bits:
        st.html(f'<div class="ts-chat-meta">{escape(" · ".join(bits))}</div>')


# An answer used to carry up to five rows of chrome — lens, tool lines, the
# attachment captions, the source list, the clock — all of them weighted like
# captions, all competing with the prose they describe. They collapse into two
# rows here, ordered prose > provenance > process: what shaped the answer above
# it, where it came from below it, and the tool trace behind a counter.


def _steps_label(msg: dict) -> str:
    """The counter that stands in for the tool trace, cost included."""
    took = _took(msg.get("took"))
    steps = msg.get("steps") or []
    if steps:
        return tr("chat.steps_n", n=len(steps), took=took)
    return tr("chat.no_tools", took=took)


def _render_turn_head(ns: str, msg: dict, index: int) -> None:
    """Above an answer: which lens produced it, and what it cost to build.

    The tool lines move inside the counter's popover — they are worth reading
    once, when something looks wrong, and not on every scroll past a turn.
    """
    skills, steps = msg.get("skills"), msg.get("steps")
    if not (skills or steps or msg.get("took")):
        return
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_thead_{index}"):
        if skills:
            st.html('<span class="ts-chat-lens">'
                    + escape(_lens_label(skills)) + "</span>")
        if steps:
            with st.popover(_steps_label(msg), key=f"{ns}_steps_{index}"):
                _render_steps(steps)
        elif msg.get("took"):
            st.html('<span class="ts-chat-quota">'
                    + escape(_steps_label(msg)) + "</span>")


def _render_turn_foot(ns: str, msg: dict, index: int) -> None:
    """Under an answer: how many pages it stands on, and when it was written.

    The domains go in a popover — a reader checking provenance wants the list,
    a reader following the argument does not, and six hostnames under every
    answer served neither.
    """
    web = msg.get("web") or []
    clock = _clock(msg.get("ts"))
    if not (web or clock):
        return
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_tfoot_{index}"):
        if web:
            with st.popover(tr("chat.sources_n", n=len(web)),
                            icon=":material/link:", key=f"{ns}_src_{index}"):
                for source in web:
                    url = source.get("url", "")
                    st.markdown(f"[{_host(url)}]({url})")
        if clock:
            st.html(f'<span class="ts-chat-clock">{escape(clock)}</span>')


def _render_files(msg: dict) -> None:
    """What was attached to a turn, as chips inside the turn that carried it."""
    for f in msg.get("files", []):
        st.html('<span class="ts-chat-file">' + escape(f["name"]) + "</span>")


def _notice(box, key: str, icon: str = ":material/error:"):
    """A container shaped like an answer, for the four ways a turn can fail.

    Rate limits, a spent quota and a dead provider used to print full-width
    Streamlit blocks outside the conversation: they broke the thread in half
    and never said what had become of the question. Given the answer's own
    avatar column and bubble, each one reads as what it is — the reply that
    turn got — and the question stays visible above it.
    """
    with box:
        holder = st.container(key=key)
    with holder:
        return st.chat_message("assistant", avatar=icon)


def _render_turn(ns: str, msg: dict, index: int) -> None:
    """One stored turn, drawn the way it will be re-drawn on every reload."""
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            _render_turn_head(ns, msg, index)
            st.markdown(msg["content"])
            _render_files(msg)
            _render_turn_foot(ns, msg, index)
        else:
            st.markdown(msg["content"])
            _render_files(msg)
            _render_meta(msg)


def _step_arg(args: dict) -> str:
    """The argument that identifies a call — the query, the URL, the tickers."""
    value = next((args[k] for k in _STEP_ARG_KEYS if args.get(k)), None)
    if value is None:
        value = next((v for _, v in sorted(args.items()) if v), "")
    text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return " ".join(text.split())[:_STEP_ARG_CHARS]


def _step_out(call) -> str:
    """The one-line result summary under a tool call.

    A search is counted in hits (the URLs it returned); everything else in
    characters, which is the honest measure of what reached the prompt — a
    page read that hit a paywall says so by being tiny.
    """
    result = call.result or ""
    if call.name == "search_web":
        hits = sum(1 for ln in result.splitlines()
                   if ln.strip().startswith("http"))
        if hits:
            return tr("chat.step_results", n=hits)
    return tr("chat.step_chars", n=len(result))


def _steps(evidence, hits: list, live: list) -> list[dict]:
    """What ran for this answer, as tool lines.

    Two code paths produce the same shape: the model-directed gather's own
    calls (chat/agent.py), and the fixed pre-flight's search and quote lookup.
    Which one ran is plumbing — what the reader wants is the list of things
    the answer was built on, in the order they happened.
    """
    steps = [
        {"tool": call.name, "arg": _step_arg(call.args), "out": _step_out(call)}
        for call in getattr(evidence, "calls", [])
    ]
    if hits:
        steps.append({"tool": "search_web", "arg": "",
                      "out": tr("chat.step_results", n=len(hits))})
    if live:
        steps.append({
            "tool": "get_quotes",
            "arg": ", ".join(q.ticker for q in live)[:_STEP_ARG_CHARS],
            "out": tr("chat.step_quotes", n=len(live)),
        })
    return steps


def _render_steps(steps: list[dict]) -> None:
    """The tool lines above an answer: dot, call, and its one-line result."""
    if not steps:
        return
    rows = []
    for s in steps:
        arg = escape(s.get("arg") or "")
        rows.append(
            '<div class="ts-step"><span class="ts-dot">●</span>'
            f'<span class="ts-tool">{escape(s.get("tool", ""))}</span>'
            + (f'<span class="ts-arg">{arg}</span>' if arg else "")
            + "</div>"
        )
        if s.get("out"):
            rows.append(f'<div class="ts-step-out">⎿ {escape(s["out"])}</div>')
    st.html(f'<div class="ts-steps">{"".join(rows)}</div>')


# The elapsed clock has to tick in the browser. Everything the working line
# covers — routing, the gather's tool loop, the searches and page reads, the
# wait on the provider's first token — runs on the script thread, which is
# exactly when the server sends nothing: a server-rendered "12s" would freeze
# at whatever the last phase change wrote. So the script ships a start moment
# once and the browser counts from it; a phase change replaces the label and
# leaves the clock running (window.__tsWork survives the swap), and the
# element going away is what stops the timer, so a retired line never keeps
# an interval alive.
_WORK_JS = """
<script>
(function () {
  const els = document.querySelectorAll(".ts-work");
  const el = els[els.length - 1];
  if (!el) return;
  const S = window.__tsWork || (window.__tsWork = {});
  if (S.iv) { clearInterval(S.iv); S.iv = null; }
  if (el.dataset.reset === "1" || !S.t0) S.t0 = Date.now();
  const still = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const glyphs = ["✻", "✽", "✻", "✢", "·", "✢"];
  let i = 0;
  const tick = function () {
    if (!el.isConnected) { clearInterval(S.iv); S.iv = null; return; }
    if (!still) {
      el.querySelector(".ts-work-glyph").textContent = glyphs[i++ % glyphs.length];
    }
    el.querySelector(".ts-work-time").textContent =
      Math.floor((Date.now() - S.t0) / 1000) + "s";
  };
  tick();
  S.iv = setInterval(tick, 220);
})();
</script>
"""


# Where each phase sits on the bar. Four steps, not a continuum: the script
# only knows which phase it is in, and pretending otherwise (a creeping bar)
# would be a lie the reader learns to ignore.
_WORK_STEPS = {"thinking": 18, "gathering": 42, "searching": 70, "writing": 92}


class _Working:
    """The line that says what the assistant is doing right now.

    One slot in the bubble, rewritten per phase: routing and gathering, the
    web pass, then the wait on the first token. It is cleared by whoever
    finishes the turn — the streaming tap on its first chunk, or the error and
    action paths that never stream at all — so it can never outlive the work.
    """

    def __init__(self, container) -> None:
        self._slot = container.empty()
        self._started = False

    def phase(self, key: str) -> None:
        """Show `key`'s label ("thinking", "gathering", "searching",
        "writing"), starting the clock on the first call of the turn."""
        reset = "0" if self._started else "1"
        self._started = True
        label = escape(tr(f"chat.work_{key}"))
        # A discrete bar, advanced by the phase change itself: an elapsed
        # count alone ("18s") says nothing about whether to keep waiting, and
        # an indeterminate bar would claim progress the script cannot see. It
        # animates nothing — every step is a server-rendered width.
        pct = _WORK_STEPS.get(key, 0)
        self._slot.html(
            f'<div class="ts-work" data-reset="{reset}">'
            '<span class="ts-work-glyph">✻</span>'
            f'<span class="ts-work-label">{label}</span>'
            '<span class="ts-work-time">0s</span></div>'
            '<div class="ts-work-track">'
            f'<div class="ts-work-fill" style="width:{pct}%"></div></div>'
            + _WORK_JS,
            unsafe_allow_javascript=True,
        )

    def clear(self) -> None:
        self._slot.empty()


# Injected by render_conversation, so any surface that draws the conversation
# gets it. No raw "less-than" anywhere in this block: DOMPurify drops a whole
# style block whose text holds one (see web/css.py).
_CONV_CSS = """
<style>
/* Terminal-flavoured chrome around each turn: monospace, dim, one step below
   a caption, so it reads as instrumentation and never competes with the
   answer itself. */
.ts-chat-meta {
  font-family: "Martian Mono", monospace;
  font-size: var(--ag-fs-2xs); color: var(--ag-text-faint);
  letter-spacing: -0.02em; margin-top: 0.2rem;
}
.ts-steps {
  font-family: "Martian Mono", monospace;
  font-size: var(--ag-fs-2xs); line-height: 1.75;
  letter-spacing: -0.02em; margin-bottom: 0.4rem;
}
.ts-step {white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.ts-dot {color: var(--ag-brand-accent); margin-right: 0.5em;}
.ts-tool {color: var(--ag-text-secondary);}
.ts-arg {color: var(--ag-text-faint); margin-left: 0.5em;}
.ts-step-out {
  color: var(--ag-text-faint); padding-left: 1.3em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ts-work {
  display: flex; align-items: center; gap: 0.55em;
  font-family: "Martian Mono", monospace;
  font-size: var(--ag-fs-xs); color: var(--ag-text-muted);
  letter-spacing: -0.02em; padding: 0.15rem 0;
}
.ts-work-glyph {color: var(--ag-brand-accent); width: 1em; text-align: center;}
.ts-work-time {color: var(--ag-text-faint);}
/* Four discrete steps under the working line. No animation of its own: the
   width changes when the phase does, and nothing pretends to progress in
   between. */
.ts-work-track {
  height: 3px; border-radius: var(--ag-radius-xs);
  background: var(--ag-border); overflow: hidden; margin: 0.15rem 0 0.1rem;
}
.ts-work-fill {height: 100%; background: var(--ag-brand-accent);}

/* The turn's two chrome rows. Everything here is one step below a caption and
   none of it competes with the prose: the lens says why the answer leans the
   way it does, the counter stands in for the tool trace, the clock and the
   source count sit under the bubble. */
.ts-chat-lens {
  font-size: var(--ag-fs-2xs); font-weight: 600; color: var(--ag-purple-400);
  background: var(--ag-purple-900); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-pill); padding: 0.05rem 0.45rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ts-chat-clock, .ts-chat-cause {
  font-family: "Martian Mono", monospace; font-size: var(--ag-fs-2xs);
  color: var(--ag-text-faint); letter-spacing: -0.02em; white-space: nowrap;
}
/* An attachment belongs to the turn that carried it, so it is a chip inside
   the bubble rather than a caption floating under it. */
/* Streamlit under-sizes a caption's container, so the element after it paints
   over the text — the same quirk app.py pads around for its metric rows. In a
   380px panel every caption wraps to two or three lines, so it bites here on
   nearly all of them. */
.st-key-chatpanel [data-testid="stCaptionContainer"] {
  padding-bottom: 0.9rem; line-height: 1.5;
}

/* Capability rows: one rule *between* each, no card chrome. The rule is a
   border-bottom and the first row has none — as a border-top on every row it
   landed on top of the caption above the list, striking the text through. */
/* The opening screen: the artboard's body box — 20px top, 16px sides once the
   scroll region's own padding is counted, 18px between blocks — as a full-
   height flex column so the drop zone can be pushed to its foot. */
[class*="st-key-panel_empty"] [data-testid="stVerticalBlock"] {
  gap: 1.1rem; height: 100%; min-height: 100%;
}
.st-key-chatpanel .st-key-panel_empty { padding: 0.6rem 0.4rem 0; }
.st-key-chatpanel .st-key-panel_empty [data-testid="stHtml"]:has(.ts-chat-drop) {
  margin-top: auto;
}
/* Intro: 16px/600 over 12px/1.55, 6px apart — the artboard's own steps. */
.ts-empty-head {display: flex; flex-direction: column; gap: 0.35rem;}
.ts-empty-title {
  font-size: var(--ag-fs-lg); font-weight: 600; line-height: 1.35;
  color: var(--ag-text-primary);
}
.ts-empty-body {
  font-size: var(--ag-fs-sm); line-height: 1.55;
  color: var(--ag-text-secondary);
}
/* The capability list: ruled top and bottom, 9px rows, icon and text in two
   columns so a wrapped line hangs with the text. */
.ts-caps {display: flex; flex-direction: column; gap: 1px;}
.ts-cap {
  display: flex; gap: 0.65rem; align-items: flex-start; padding: 0.55rem 0;
  border-top: 1px solid var(--ag-rule-panel);
  font-size: var(--ag-fs-sm); line-height: 1.5;
  color: var(--ag-text-secondary);
}
.ts-cap:last-child {border-bottom: 1px solid var(--ag-rule-panel);}
.ts-cap svg {flex-shrink: 0; margin-top: 1px; color: var(--ag-brand-accent);}
.ts-cap b {color: var(--ag-text-primary); font-weight: 600;}
/* Suggestions: one tile per question, tight, left-aligned, and each one a
   press away from being sent as typed. Three selectors, not one: the button,
   its inner flex wrapper and its markdown container each centre the label on
   their own — the same fight nav.py picks for the top-bar results. */
[class*="st-key-panel_starters"] [data-testid="stVerticalBlock"] { gap: 0.4rem; }
[class*="st-key-panel_starters"] button {
  justify-content: flex-start; text-align: left; min-height: 0;
  padding: 9px 11px; border-radius: var(--ag-radius-sm);
  background-color: var(--ag-surface-card); border: 1px solid var(--ag-border);
  color: var(--ag-text-primary);
}
[class*="st-key-panel_starters"] button:hover,
[class*="st-key-panel_starters"] button:focus,
[class*="st-key-panel_starters"] button:active {
  background-color: var(--ag-surface-hover); border-color: var(--ag-border-focus);
  color: var(--ag-text-primary);
}
[class*="st-key-panel_starters"] button > div { justify-content: flex-start; }
[class*="st-key-panel_starters"] button [data-testid="stMarkdownContainer"] {
  width: 100%; text-align: left;
}
[class*="st-key-panel_starters"] button p {
  font-size: var(--ag-fs-sm); font-weight: 400; line-height: 1.35;
  text-align: left; white-space: normal;
}
/* The caps label sits inside the container, so it drops the padding it needs
   when it heads a settings section. */
.ts-chat-group-tight { padding: 0.25rem 0 0; }
/* The "or with your key" rule: a label with a line through it. */
.ts-chat-or {
  display: flex; align-items: center; gap: 0.6rem; margin: 0.8rem 0 0.2rem;
  font-family: "Martian Mono", monospace; font-size: var(--ag-fs-2xs);
  color: var(--ag-text-faint);
}
.ts-chat-or::before, .ts-chat-or::after {
  content: ""; flex: 1; height: 1px; background: var(--ag-border);
}
.ts-chat-drop {
  display: flex; align-items: center; gap: 0.55rem;
  border: 1px dashed var(--ag-border-focus); border-radius: var(--ag-radius-sm);
  padding: 0.75rem; font-size: var(--ag-fs-xs); color: var(--ag-text-muted);
}
.ts-chat-drop svg { flex-shrink: 0; }
.ts-chat-file {
  display: inline-block; font-size: var(--ag-fs-xs); color: var(--ag-purple-400);
  background: var(--ag-purple-900); border: 1px solid var(--ag-purple-800);
  border-radius: var(--ag-radius-xs); padding: 0.05rem 0.4rem;
  margin-top: 0.35rem;
}
</style>
"""


# ------------------------------------------------------------- conversation

# The tail of the conversation actually sent to the model (engine.recent):
# the full thread stays on screen and on disk.
_recent = engine.recent


def render_conversation(ns: str, provider: llm.Provider, model: str,
                        api_key: str) -> None:
    """Draw the history, take input, and stream the next answer.

    ns namespaces the widget keys so two surfaces can coexist. The
    account-scoped history is hydrated from disk on first touch this session
    (auth.load_chat) and written back after every turn (auth.save_chat), so it
    survives a reload, a new session, or an ephemeral redeploy.
    """
    css.inject(_CONV_CSS)
    conv = auth.active_conversation()
    hist_key = _hist_key(conv)
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
        if not history:
            _render_empty_state(ns)
        for index, msg in enumerate(history):
            _render_turn(ns, msg, index)

    _render_rail(ns)
    text, files = _submitted(st.chat_input(
        tr("chat.placeholder"), key=f"{ns}_input",
        accept_file="multiple", file_type=list(autodetect.supported_types()),
        max_upload_size=MAX_UPLOAD_MB,
    ))
    # A suggestion clicked on the opening screen. Popped, not read: it must
    # fire once, and typed text always wins if both land on the same run.
    seed = st.session_state.pop(_seed_key(ns), None)
    if seed and not text:
        text = seed
    if text or files:
        # Burst protection on top of the free chain's daily cap: every turn
        # fans out into routing/search/provider calls, so a runaway client
        # (or a pasted loop) must hit a wall before the providers do. Keyed
        # by the account's data dir — reconnecting doesn't reset it.
        rl_key = f"chat::{auth.user_paths().root}"
        if not ratelimit.allow(rl_key):
            with _notice(box, f"{ns}_notice_rate", ":material/hourglass_top:"):
                st.markdown(tr("chat.rate_limited",
                               seconds=ratelimit.retry_after(rl_key)))
            # Return, not st.stop(): the panel draws its free counter after
            # this call, and Streamlit drops what a stopped script writes on
            # its way out. Nothing follows the conversation on the surfaces
            # that render it, so the two end the run alike.
            return
        # A new question supersedes a failed one: the turn that never got an
        # answer goes with its error, since two user turns in a row would
        # reach the provider as one malformed exchange.
        if _failure(history):
            history.pop()
        turn: dict = _stamp(
            {"role": "user", "content": text or tr("chat.import_ask")})
        if files:
            # Only the names go on the thread; the bytes ride in session state
            # and are consumed by the ingest below on this same run.
            turn["files"] = [{"name": f.name} for f in files]
            st.session_state[_uploads_key(ns)] = [
                (f.name, f.getvalue()) for f in files
            ]
        history.append(turn)
        # Written before the answer rather than with it. Generating is the
        # long part of a turn — routing, searches, then the stream — and a
        # reload during it used to take the unsaved question down with the
        # session, leaving no trace that anything had been asked. Stored, the
        # trailing unanswered turn is the same shape Retry leaves behind, so
        # the generate block below picks it up on the next run and answers it.
        #
        # Not for an attached statement: only the bytes in session state can
        # be imported and they do not survive a reload, so that turn would
        # come back as a question nothing can answer.
        if not files:
            auth.save_chat(history)
        with box:
            _render_turn(ns, turn, len(history) - 1)

    # An attached statement is an import, not a question: it is parsed,
    # validated and previewed here, and never reaches the model as text.
    uploads = st.session_state.pop(_uploads_key(ns), None)
    if uploads:
        _ingest_uploads(ns, uploads, provider, api_key, history)
        st.rerun()

    # "Import my trades", with nothing attached. The app answers this one
    # itself (see _import_hint).
    if history and history[-1]["role"] == "user" and not _failure(history):
        note = _import_hint(ns, history[-1]["content"])
        if note is not None:
            history.append({"role": "assistant", "content": note,
                            "action": "import"})
            auth.save_chat(history)
            st.rerun()

    # Generate whenever the last turn is a user turn still awaiting a reply
    # (covers both a fresh message and a Regenerate through one code path).
    if history and history[-1]["role"] == "user" and not _failure(history):
        # The working line opens before any of the work behind it: the action
        # probe, the routing calls, the searches and the wait on the provider
        # all pass with the bubble otherwise empty, and it names the step the
        # wait is currently in while counting the seconds it has taken.
        #
        # The bubble is opened first and the line drawn *inside* it, so the
        # arrival of the first token changes the bubble's contents instead of
        # replacing a row above it with a bubble — no layout jump at the one
        # moment the reader is staring at the spot.
        started = time.time()
        with box:
            bubble = st.chat_message("assistant")
        work = _Working(bubble)
        work.phase("thinking")
        # App actions first: an executed action (favorite / alert / group)
        # answers with a deterministic localized confirmation — no main model
        # call, no free-quota spend.
        act = _try_action(provider, api_key, history[-1]["content"])
        if act is not None:
            work.clear()
            note = _action_reply(act)
            answered = _stamp({"role": "assistant", "content": note,
                               "action": act.kind}, time.time() - started)
            with bubble:
                st.markdown(note)
                _render_turn_foot(ns, answered, len(history))
            history.append(answered)
            auth.save_chat(history)
            if _maybe_autotitle(conv, history, provider, api_key):
                st.rerun()
        else:
            # Every free unit this turn spends, so a turn that ends in a
            # failure can give them all back: the spend happens before the
            # model is called (the last moment the turn can be refused), and
            # without the refund a dead provider costs the reader a message it
            # never wrote — then costs another one on Retry.
            spent: list[int] = []
            if provider.id == "free":
                if not _spend_free_quota():
                    work.clear()
                    history.pop()  # drop the turn we won't answer
                    auth.save_chat(history)
                    with _notice(box, f"{ns}_notice_quota",
                                 ":material/hourglass_top:"):
                        st.markdown(_cap_message())
                        # The way out of a shared cap is a key of your own, so
                        # the notice offers it instead of describing it.
                        if st.button(tr("chat.free_add_key"),
                                     icon=":material/key:", type="primary",
                                     key=f"{ns}_free_key"):
                            _open_view("settings")
                    return  # see the rate-limit branch above
                spent.append(1)
            with bubble:
                # Routing and search run under the working line above; it
                # survives into write_stream and is retired by
                # _retire_on_first_chunk the moment the model's first token
                # arrives (write_stream itself shows nothing until then).
                try:
                    # Session state is read here, on the script thread; the
                    # three lookups then run concurrently off it (routing,
                    # search + page reads, quotes — ~15s back to back).
                    prefs = auth.load_prefs()
                    view = _view_context().strip()
                    watchlist = auth.watchlist_path()
                    db = auth.db_path()
                    memory_db = auth.memory_path()
                    focus = st.session_state.get("picker_selected") or ""
                    msgs = _recent(history)
                    # Two shapes of the same step. When the provider has tool
                    # use, the model picks what to fetch (chat/agent.py) while
                    # routing runs beside it; otherwise the fixed pre-flight
                    # fetches its usual guess. A gather that never ran (no
                    # tools, dead key, timeout) falls through to the fixed one;
                    # a gather that ran and chose nothing is obeyed.
                    work.phase("gathering")
                    skills, evidence = engine.in_parallel(
                        lambda: _resolve_skills(provider, api_key, history,
                                                prefs, view),
                        lambda: _gather(provider, api_key, msgs, prefs,
                                        watchlist, db, memory_db, conv["id"],
                                        focus),
                    )
                    skills = skills or []
                    evidence = evidence or agent.Evidence(ok=False)
                    hits, live = [], []
                    if not evidence.ok:
                        work.phase("searching")
                        hits, live = engine.in_parallel(
                            lambda: _gather_web(provider, api_key, history,
                                                prefs, view),
                            lambda: _live_quotes(history[-1]["content"],
                                                 watchlist, focus),
                        )
                        hits, live = hits or [], live or []
                    # Reserved now, written after the answer: the row states
                    # the lens and what the turn cost, and the cost is not
                    # known until the last token has landed.
                    head = st.container()
                    steps = _steps(evidence, hits, live)
                    # Everything fetched rides on the outgoing copy of the user
                    # turn, not the system prompt — the stored history keeps
                    # the user's own text, and prompt caches stay warm.
                    if evidence:
                        msgs[-1]["content"] = evidence.augment(
                            msgs[-1]["content"])
                    if hits:
                        msgs[-1]["content"] = chat_web.augment(
                            msgs[-1]["content"], hits)
                    if live:
                        msgs[-1]["content"] = market.augment(
                            msgs[-1]["content"], live)
                    system = _system_prompt(skills)
                    # Last, after augmentation: the page extracts and quotes
                    # just appended to the newest turn are the biggest thing
                    # in the request (chat/tokens.py).
                    msgs = tokens.fit(msgs, system=system)
                    work.phase("writing")
                    answer = _stream_with_fallback(
                        work, provider, api_key, model, system, msgs, prefs,
                        spent=spent)
                    web_sources = chat_web.sources(hits) or evidence.sources()
                    answered = _stamp({"role": "assistant", "content": answer},
                                      time.time() - started)
                    if skills:
                        answered["skills"] = skills
                    if steps:  # redrawn with the turn on every reload
                        answered["steps"] = steps
                    if web_sources:
                        answered["web"] = web_sources
                    with head:
                        _render_turn_head(ns, answered, len(history))
                    _render_turn_foot(ns, answered, len(history))
                except Exception as exc:  # classified per provider; unknown -> re-raise
                    work.clear()
                    # Nothing was answered, so nothing was owed: both the unit
                    # this turn opened with and any spent falling down the
                    # chain go back before the failure is pinned.
                    _refund_free_quota(sum(spent))
                    # The chain tags the exception with the provider that
                    # actually raised (the chosen one, or the last fallback).
                    failed = getattr(exc, "chat_provider", provider)
                    err = failed.error_key(exc)
                    if err is None:
                        # Unclassified — the crash page is still the right
                        # answer here. But the question is on disk now, so
                        # leaving it unmarked would re-run the same crash on
                        # every reload of the thread. Pin the generic failure
                        # first: the reader gets their question back with
                        # Retry under it instead of a page that will not load.
                        history[-1]["error"] = ["chat.api_error", failed.label]
                        auth.save_chat(history)
                        raise
                    # The question stays on the thread with its failure
                    # pinned beside it (rendered below) instead of vanishing
                    # along with it: that turn is what Retry replays.
                    history[-1]["error"] = [err, failed.label]
                    auth.save_chat(history)
                    st.rerun()
            history.append(answered)  # already carries lens, trace and sources
            auth.save_chat(history)  # persist the completed user+assistant turn
            if _maybe_autotitle(conv, history, provider, api_key):
                st.rerun()

    if _render_pending_import(ns, history, box):
        return  # a batch is waiting on the user — regenerating makes no sense

    failure = _failure(history)
    if failure:
        # A dead provider must not cost the reader their question: the error
        # sits under it, and Retry only clears the mark — the same trailing
        # user turn then generates again through the one path above.
        err, label = failure
        with _notice(box, f"{ns}_notice_fail"):
            st.markdown(tr(err, provider=label))
            # One mono line with the technical cause, for the reader who wants
            # to know which provider and which failure before pressing Retry.
            cause = " · ".join(bit for bit in (
                label.lower(), err.rsplit(".", 1)[-1],
                _clock(history[-1].get("ts"))) if bit)
            st.html(f'<span class="ts-chat-cause">{escape(cause)}</span>')
            with st.container(horizontal=True):
                if st.button(tr("chat.retry"), icon=":material/refresh:",
                             type="primary", key=f"{ns}_retry"):
                    history[-1].pop("error", None)
                    auth.save_chat(history)
                    st.rerun()
                if st.button(tr("chat.error_drop"), icon=":material/close:",
                             key=f"{ns}_drop_failed"):
                    history.pop()
                    auth.save_chat(history)
                    st.rerun()
        return

    if history and history[-1]["role"] == "assistant":
        # Regenerate only. Clearing the thread is destructive and used to sit
        # one button away from it, inside the scroll region — it now lives in
        # the settings view, behind a confirmation.
        with box, st.container(horizontal=True):
            if st.button(tr("chat.regenerate"), icon=":material/refresh:",
                         key=f"{ns}_regen"):
                history.pop()
                auth.save_chat(history)
                st.rerun()


# ----------------------------------------------------------- drawer chrome
# The two header rows and the composer rail. Between them they replace the
# settings expander that used to sit above the conversation: what the reader
# needs at rest (which thread, which model, how much free quota is left) is
# now always on screen in 70px, and the controls that change an answer sit
# beside the input they change.


def _free_left() -> int:
    """Free messages this account has left today (never below zero).

    The account's own counter is half the wall: the free chain also has a
    process-wide pot, and an account holding 12 units in front of an empty pot
    holds none. engine.free_left states whichever binds first, so the strip
    cannot promise a message the next send refuses.
    """
    return engine.free_left(auth.load_prefs())


# Width presets. The drag handle stays as fine tuning, remembered per browser,
# but it never looked like a control — so the three widths anyone actually
# asks for are named buttons in the header: compact to read beside the page,
# wide for tables and sources, full for the import review. A press rewrites
# the same --chat-w the handle drives and stores it under the same key, so the
# two mechanisms cannot disagree.
_WIDTHS = (
    ("compact", "380px", ":material/width_normal:"),
    ("wide", "720px", ":material/width_wide:"),
    ("full", "100vw", ":material/fullscreen:"),
)

_WIDTH_JS = """
<script>
(function () {
  var w = "%s";
  document.documentElement.style.setProperty('--chat-w', w);
  try { localStorage.setItem('chatPanelWidth', w); } catch (e) {}
})();
</script>
"""


def _render_width_presets(ns: str) -> None:
    """Three width buttons. Pressing one only records the choice.

    Session state drives which button looks active and which width is still
    waiting to be applied; the width itself lives in the browser
    (localStorage), so a drag survives a rerun and a preset survives a reload.
    """
    active = st.session_state.get("chat_width", "compact")
    for name, _width, icon in _WIDTHS:
        if st.button("", icon=icon, key=f"{ns}_w_{name}",
                     type="primary" if name == active else "tertiary",
                     help=tr(f"chat.width_{name}")):
            st.session_state["chat_width"] = name
            st.session_state["chat_width_apply"] = name
            st.rerun()


def _apply_width() -> None:
    """Hand a just-pressed preset to the browser, once.

    It cannot be emitted from the button branch: st.rerun() discards whatever
    the run had already written, so the script would never reach the page. And
    it must not be emitted on every run either — that would overwrite a drag
    with the last preset pressed on the next fragment rerun.
    """
    pending = st.session_state.pop("chat_width_apply", None)
    if pending is None:
        return
    width = dict((name, w) for name, w, _icon in _WIDTHS).get(pending)
    if width:
        st.html(_WIDTH_JS % width, unsafe_allow_javascript=True)


def _render_panel_head(ns: str, conv: dict) -> None:
    """Row 1: the open thread (the way into the list), new, widths, close.

    The old row spent 72% of the width on the word "Assistant" and 28% on a
    text Close button, neither of which said anything the launcher had not
    already said. Both are gone: the title is the thread, and closing is an
    icon.
    """
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_head"):
        # An unnamed thread is a placeholder, so it is greyed the way the
        # artboard has it. The colour rides in the label (Streamlit's own
        # markdown directive) rather than in CSS, which cannot tell an
        # untitled thread from a titled one.
        named = bool((conv.get("title") or "").strip())
        label = _conv_label(conv, 22)
        if st.button(label if named else f":gray[{label}]",
                     icon=":material/forum:", type="tertiary", width="stretch",
                     key=f"{ns}_open_threads", help=tr("chat.threads_title")):
            _open_view("threads")
        if st.button("", icon=":material/add:", type="tertiary",
                     key=f"{ns}_new", help=tr("chat.new")):
            auth.new_conversation()
            _open_view("thread")
        _render_width_presets(ns)
        if st.button("", icon=":material/close:", type="tertiary",
                     key=f"{ns}_close", help=tr("chat.close")):
            st.session_state["chat_panel_open"] = False
            # The panel container itself is created outside this fragment, so
            # closing has to repaint the whole app to take it away.
            st.rerun(scope="app")


def _render_status_strip(ns: str, provider: llm.Provider, model: str):
    """Row 2: what is answering, what is left of the free pot, a way to change it.

    This is the line the app already had a string for ("chat.using") and never
    showed: until now neither question — which model is this, and how many
    free messages do I have — could be answered without opening something.

    Returns the counter's slot (None when the provider is the reader's own
    key, which has no counter). The two buttons stay where they are — moving
    them below the thread would make a press wait for the turn underneath —
    but the count itself is written by _fill_quota after the turn has spent
    what it spends. See _free_left.
    """
    slot = None
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_status"):
        if st.button(provider.label, icon=":material/auto_awesome:",
                     type="tertiary", key=f"{ns}_status_model",
                     help=tr("chat.using", provider=provider.label, model=model)):
            _open_view("settings")
        if not provider.needs_key:
            slot = st.empty()
        if st.button(tr("chat.settings_short"), icon=":material/settings:",
                     type="tertiary", key=f"{ns}_gear"):
            _open_view("settings")
    return slot


def _fill_quota(slot) -> None:
    """Write the free counter into the slot the strip left for it.

    Called at the very end of the panel run: the turn below the strip spends
    the unit it is about to state, so a count written on the way past says
    what was left *before* the message the reader just sent — and stays wrong
    until some later rerun happens to repaint the panel.
    """
    if slot is None:
        return
    with slot:
        st.html('<span class="ts-chat-quota">'
                + escape(tr("chat.free_left", left=_free_left(),
                            cap=_free_daily_cap()))
                + "</span>")


def _render_rail(ns: str) -> None:
    """Per-message controls, beside the input they act on.

    Internet and the skill lens change the *next* answer, so they belong at
    the composer rather than in an account settings panel opened once a
    quarter. Both are buttons, not widgets: a press is one rerun, and the
    skill picker only pays for itself when it is opened.
    """
    prefs = auth.load_prefs()
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_rail"):
        if chat_web.available():
            on = bool(prefs.get("chat_web", True))
            if st.button(tr("chat.web_chip"), icon=":material/language:",
                         type="primary" if on else "secondary",
                         key=f"{ns}_rail_web", help=tr("chat.web_help")):
                prefs["chat_web"] = not on
                auth.save_prefs(prefs)
                st.rerun()
        mode = _skill_mode(prefs)
        with st.popover(tr("chat.skills_chip", mode=tr(f"chat.skills_{mode}")),
                        icon=":material/auto_awesome:", key=f"{ns}_rail_skills"):
            _pick_skills()


# ------------------------------------------------------------- settings view


def _provider_tiles(ns: str, columns: int = 2) -> llm.Provider:
    """Provider chooser as tiles; remembers the choice in session + prefs.

    A segmented control cannot hold four providers at 380px — the logos and
    brand names overflow the row — and its label plus hint is chrome the panel
    already provides. Tiles wrap instead, and each one says whether it needs a
    key of its own.
    """
    provs = _offered_providers()
    ids = [p.id for p in provs]
    prefs = auth.load_prefs()
    pid = (st.session_state.get("llm_provider") or prefs.get("llm_provider")
           or llm.default_provider_id())
    if pid not in ids:
        pid = ids[0]
    cols = st.columns(columns)
    for n, prov in enumerate(provs):
        hint = tr("chat.key_needed") if prov.needs_key else tr("chat.free_tag")
        if cols[n % columns].button(
                prov.label, key=f"{ns}_prov_{prov.id}", width="stretch",
                help=hint, type="primary" if prov.id == pid else "secondary"):
            st.session_state["llm_provider"] = prov.id
            if prov.id != prefs.get("llm_provider"):
                prefs["llm_provider"] = prov.id
                auth.save_prefs(prefs)
            st.rerun()
    st.session_state["llm_provider"] = pid
    if pid != prefs.get("llm_provider"):
        prefs["llm_provider"] = pid
        auth.save_prefs(prefs)
    return llm.PROVIDERS[pid]


def _render_setup(ns: str) -> None:
    """First run, with the free path first.

    The keyless chain has existed for a while and the setup screen never
    offered it: a reader arriving with no key was shown a provider list and an
    API-key field, which reads as "you cannot use this yet". The free
    assistant is now the primary button and the key form sits below it, for
    the reader who already knows which provider they want.
    """
    # The panel gave up its own padding when the header rows went full-bleed,
    # so this screen carries the same padded body as the other views.
    with st.container(key=f"{ns}_view"):
        st.markdown(f"##### {tr('chat.setup_title')}")
        st.caption(tr("chat.setup_body"))

        free = next((p for p in _offered_providers() if not p.needs_key), None)
        if free is not None:
            if st.button(tr("chat.free_cta"), icon=":material/auto_awesome:",
                         type="primary", width="stretch", key=f"{ns}_use_free"):
                prefs = auth.load_prefs()
                prefs["llm_provider"] = free.id
                auth.save_prefs(prefs)
                st.session_state["llm_provider"] = free.id
                st.rerun()
            st.caption(tr("chat.free_cta_note", cap=_free_daily_cap()))
            st.html('<div class="ts-chat-or">' + escape(tr("chat.setup_or"))
                    + "</div>")

        provider = _provider_tiles(f"{ns}_setup", columns=3)
        if provider.needs_key:
            _key_gate(provider)  # renders the form; reruns the fragment on submit
        else:
            # Pressing a keyless tile already reran the fragment, and the panel
            # skips this screen entirely once one is active — so there is nothing
            # to rerun here, only the terms of the free chain to state.
            st.caption(tr("chat.free_note"))


def _render_settings_view(ns: str, conv: dict) -> None:
    """The drawer's body, swapped for the account-level settings.

    Provider, model and key are set once and then left alone for months,
    which is exactly why they had no business in the slot above every
    conversation. Here they get room, and the destructive action that used to
    ride inside the message list ends up where a destructive action belongs:
    at the bottom, behind a confirmation.
    """
    with st.container(horizontal=True, vertical_alignment="center",
                      key=f"{ns}_settingshead"):
        if st.button(tr("chat.back_thread"), icon=":material/chevron_left:",
                     type="tertiary", key=f"{ns}_settings_back"):
            _open_view("thread")

    with st.container(key=f"{ns}_view"):
        st.html('<div class="ts-chat-group">' + escape(tr("chat.sec_provider"))
                + "</div>")
        provider = _provider_tiles(ns)

        st.html('<div class="ts-chat-group">' + escape(tr("chat.sec_model"))
                + "</div>")
        if len(provider.models) > 1:
            _pick_model(provider, f"{ns}_model_{provider.id}")
        else:
            st.caption(provider.default_model)
        if not provider.needs_key:
            # The free chain's pot, as a figure and as a bar: the caption alone
            # never told anyone how close they were to the wall.
            cap, left = _free_daily_cap(), _free_left()
            with st.container(horizontal=True, vertical_alignment="center",
                              key=f"{ns}_quota"):
                st.caption(tr("chat.free_left_label"))
                st.html('<span class="ts-chat-quota">'
                        + escape(f"{left} / {cap}") + "</span>")
            st.progress(max(0.0, min(1.0, left / cap)) if cap else 0.0)
            st.caption(tr("chat.free_note"))

        if provider.needs_key:
            st.html('<div class="ts-chat-group">' + escape(tr("chat.sec_key"))
                    + "</div>")
            configured = active_key(provider)
            if configured:
                _show_key(provider, configured)
                if st.button(tr("chat.forget"), icon=":material/logout:",
                             key=f"{ns}_forget"):
                    _forget_key(provider.id)
                    st.rerun()
            else:
                _key_gate(provider)

        st.html('<div class="ts-chat-group">' + escape(tr("chat.sec_thread"))
                + "</div>")
        if st.session_state.get(f"{ns}_clearing"):
            with st.container(key=f"{ns}_clearconfirm"):
                st.markdown(tr("chat.delete_confirm",
                               title=_conv_label(conv, 24),
                               n=len(st.session_state.get(_hist_key(conv), []))))
                with st.container(horizontal=True):
                    if st.button(tr("chat.delete_yes"), icon=":material/delete:",
                                 key=f"{ns}_clear_yes"):
                        auth.delete_conversation(conv["id"])
                        st.session_state.pop(f"{ns}_clearing", None)
                        st.session_state.pop(_hist_key(conv), None)
                        _open_view("thread")
                    if st.button(tr("chat.cancel"), key=f"{ns}_clear_no"):
                        st.session_state.pop(f"{ns}_clearing", None)
                        st.rerun()
        elif st.button(tr("chat.clear_thread"), icon=":material/delete:",
                       key=f"{ns}_clear_thread"):
            st.session_state[f"{ns}_clearing"] = True
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
  border-radius: var(--ag-radius-sm); width: 36px; height: 36px; padding: 0;
  box-shadow: 0px 4px 12px var(--ag-cta-glow);
  background: var(--ag-brand-cta) !important;
  border-color: var(--ag-brand-cta) !important;
  color: var(--ag-text-primary) !important;
}
.st-key-chatfab button:hover {
  background: var(--ag-purple-700) !important;
  border-color: var(--ag-purple-700) !important;
  color: var(--ag-text-primary) !important;
}
.st-key-chatfab button * { color: var(--ag-text-primary) !important; }
/* Phones: DS 44px touch target, centered in the 3.75rem native header.
   After the base button rule above, so the mobile size wins on source order. */
@media (max-width: 640px) {
  .st-key-chatfab { top: 8px; }
  .st-key-chatfab button { width: 44px; height: 44px; }
}
.st-key-chatpanel {
  position: fixed; top: 0; right: 0; bottom: 0;   /* full height */
  /* width driven by the --chat-w var (set live by the width slider), never
     wider than the viewport. */
  width: min(var(--chat-w, 380px), 100vw); max-width: 100vw; z-index: 1000000;
  background: var(--ag-surface-page); border-left: 1px solid var(--ag-border);
  /* No padding of its own: the header rows are full-bleed with their own
     rules, and the views below pad themselves. */
  padding: 0; overflow: hidden;
  box-shadow: -10px 0 30px var(--ag-shadow-color);
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
  transform: translateY(-50%); width: 3px; height: 44px;
  border-radius: var(--ag-radius-xs);
  background: var(--ag-border); transition: background 0.15s;
}
.st-key-chatpanel .chat-resize-handle:hover::before {
  background: var(--ag-brand-accent);
}
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
  background: var(--ag-surface-page); padding-top: 0.4rem;
}
/* Attach and send sit together at the end of the field. Streamlit puts the
   upload group first (order -1), which left the paperclip at the far left of
   the row with the placeholder pushed off it. */
.st-key-chatpanel [data-testid="stChatInput"]
  div:has(> button[data-testid="stChatInputSubmitButton"]) { order: 2; }
.st-key-chatpanel [data-testid="stChatInput"]
  div:has(> [data-testid="stChatInputFileUploadButton"]) { order: 1; }
/* The artboard's attach affordance is a paperclip; Streamlit draws a plus,
   and draws it as an SVG component, so no CSS can swap the glyph. Hiding it
   and printing the Material ligature in its place can — the font is loaded
   app-wide for Streamlit's own icons. */
.st-key-chatpanel [data-testid="stChatInputFileUploadButton"] svg {
  display: none;
}
.st-key-chatpanel [data-testid="stChatInputFileUploadButton"] button::after {
  content: "attach_file"; font-family: "Material Symbols Rounded";
  font-size: 1.2rem; line-height: 1; color: var(--ag-text-muted);
}
/* Conversation palette. Streamlit's default avatars borrow the market-semantic
   redColor (user) and orangeColor (assistant) tokens — a pink face and an
   orange robot that read as "loss"/"alert" and clash with the brand. Recolour
   to the purple family (assistant = branded gradient like the launcher FAB,
   user = quiet navy) and give each turn a rounded, tinted bubble. */
.st-key-chatpanel [data-testid="stChatMessage"] {
  background: transparent; gap: 0.6rem; padding: 0.15rem 0;
}
/* Flat, not a gradient with a halo: at 26px the gradient reads as noise and
   the halo competes with the CTA glow the launcher already owns. */
.st-key-chatpanel [data-testid="stChatMessageAvatarAssistant"] {
  background: var(--ag-purple-800) !important;
}
.st-key-chatpanel [data-testid="stChatMessageAvatarUser"] {
  background: var(--ag-surface-card) !important;
  border: 1px solid var(--ag-border);
}
.st-key-chatpanel [data-testid="stChatMessageAvatarAssistant"] * {
  color: var(--ag-text-primary) !important;
}
.st-key-chatpanel [data-testid="stChatMessageAvatarUser"] * {
  color: var(--ag-purple-400) !important;
}
.st-key-chatpanel [data-testid="stChatMessageContent"] {
  border-radius: var(--ag-radius-md); padding: 0.55rem 0.85rem;
}
.st-key-chatpanel [data-testid="stChatMessage"]\
:has([data-testid="stChatMessageAvatarAssistant"])
  [data-testid="stChatMessageContent"] {
  background: var(--ag-surface-card); border: 1px solid var(--ag-border);
}
/* The reader's own turn on the nav-active purple rather than a 16% tint of
   the CTA: the tint sat too close to the card fill to read as a bubble. */
.st-key-chatpanel [data-testid="stChatMessage"]\
:has([data-testid="stChatMessageAvatarUser"])
  [data-testid="stChatMessageContent"] {
  background: var(--ag-purple-900); border: 1px solid var(--ag-purple-800);
}
.st-key-chatpanel .st-key-panel_forget { margin-top: 0.4rem; }
/* ---------------------------------------------------------- drawer chrome */
/* Two thin header rows in place of the old title bar plus settings expander:
   row 1 (thread, new, widths, close) and row 2 (model, free quota, settings).
   Together they cost ~70px and answer what the expander only answered once
   opened. */
.st-key-chatpanel .st-key-panel_head {
  gap: 0.35rem; padding: 0.5rem 0.6rem 0.5rem 0.75rem;
  border-bottom: 1px solid var(--ag-rule-panel);
}
/* The thread name is the row's only stretchy element, and it must truncate
   rather than wrap: two lines here would shove the conversation down. */
.st-key-chatpanel .st-key-panel_open_threads button {
  justify-content: flex-start !important; padding: 0.35rem; font-weight: 600;
  color: var(--ag-text-primary) !important;
}
.st-key-chatpanel .st-key-panel_open_threads button > div {
  justify-content: flex-start;
}
.st-key-chatpanel .st-key-panel_open_threads button
  [data-testid="stMarkdownContainer"] { width: 100%; text-align: left; }
.st-key-chatpanel .st-key-panel_open_threads button p {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  text-align: left;
}
/* The greyed placeholder for an unnamed thread arrives as Streamlit's own
   :gray[…] span, which carries an inline colour — the token has to shout to
   land on it. */
.st-key-chatpanel .st-key-panel_open_threads button span[style*="color"] {
  color: var(--ag-text-secondary) !important;
}
/* Icon-only buttons in both header rows: square, no label width. */
.st-key-chatpanel .st-key-panel_head button {
  min-width: 0; padding: 0.3rem; border-radius: var(--ag-radius-nav);
}
.st-key-chatpanel .st-key-panel_new,
.st-key-chatpanel .st-key-panel_close,
.st-key-chatpanel [class*="st-key-panel_w_"] { width: max-content !important; }
/* Width presets read as one segmented group: a track behind three squares,
   the active one filled with the nav-active purple. */
.st-key-chatpanel [class*="st-key-panel_w_"] button {
  width: 28px; height: 28px; background: transparent;
  border-color: transparent; color: var(--ag-text-muted) !important;
}
.st-key-chatpanel [class*="st-key-panel_w_"] button:hover {
  background: var(--ag-surface-hover); color: var(--ag-text-primary) !important;
}
.st-key-chatpanel [class*="st-key-panel_w_"]
  button[data-testid="stBaseButton-primary"] {
  background: var(--ag-purple-900) !important;
  border-color: var(--ag-purple-900) !important;
  color: var(--ag-purple-400) !important;
}
/* 1k's limits: the 720px preset cannot fit under 1100px without covering the
   page it is meant to sit beside, and phones have no presets at all. */
@media (max-width: 1099px) { .st-key-chatpanel .st-key-panel_w_wide { display: none; } }
@media (max-width: 640px) {
  .st-key-chatpanel [class*="st-key-panel_w_"] { display: none; }
}

.st-key-chatpanel .st-key-panel_status {
  gap: 0.5rem; padding: 0.25rem 0.75rem;
  border-bottom: 1px solid var(--ag-rule-panel);
}
.st-key-chatpanel .st-key-panel_status button {
  min-width: 0; padding: 0.1rem 0.5rem; font-size: var(--ag-fs-xs);
  border-radius: var(--ag-radius-pill); font-weight: 600;
}
.st-key-chatpanel .st-key-panel_status_model button {
  background: var(--ag-purple-900); color: var(--ag-purple-400) !important;
}
.st-key-chatpanel .st-key-panel_status_model button * {
  color: var(--ag-purple-400) !important;
}
/* The way into settings sits at the far end of the strip. */
.st-key-chatpanel .st-key-panel_gear { margin-left: auto; width: max-content !important; }
.st-key-chatpanel .st-key-panel_gear button {
  background: transparent; border-color: transparent; font-weight: 500;
  color: var(--ag-text-muted) !important;
}
/* Counters and stamps stay in the instrumentation font, one step below a
   caption, so they never read as part of an answer. */
.ts-chat-quota {
  font-family: "Martian Mono", monospace; font-size: var(--ag-fs-2xs);
  color: var(--ag-text-muted); letter-spacing: -0.02em; white-space: nowrap;
}
/* Section caps: the one label style shared by the settings sections and the
   thread list's date groups. */
.ts-chat-group {
  font-size: var(--ag-fs-2xs); font-weight: 600; letter-spacing: 0.06em;
  color: var(--ag-text-faint); padding: 0.5rem 0 0.25rem;
}

/* Composer rail: the two controls that change the next answer, as chips. The
   rail's top edge is what separates the composer from the conversation. */
.st-key-chatpanel .st-key-panel_rail {
  gap: 0.35rem; padding: 0.5rem 0.75rem 0;
  border-top: 1px solid var(--ag-rule-panel);
}
.st-key-chatpanel .st-key-panel_rail button {
  min-width: 0; padding: 0.15rem 0.6rem; font-size: var(--ag-fs-xs);
  font-weight: 500; border-radius: var(--ag-radius-pill);
}
.st-key-chatpanel .st-key-panel_rail
  button[data-testid="stBaseButton-primary"] { font-weight: 600; }
.st-key-chatpanel .st-key-panel_rail_web,
.st-key-chatpanel .st-key-panel_rail_skills { width: max-content !important; }
.st-key-chatpanel .st-key-panel_rail
  button[data-testid="stBaseButton-primary"] {
  background: var(--ag-purple-900) !important;
  border-color: var(--ag-purple-900) !important;
  color: var(--ag-purple-400) !important;
}
.st-key-chatpanel .st-key-panel_rail button[data-testid="stBaseButton-primary"] * {
  color: var(--ag-purple-400) !important;
}
/* Skill chips inside the rail's popover: same pill vocabulary, wrapping. */
.st-key-panel_skillchips { flex-wrap: wrap; gap: 0.3rem; }
.st-key-panel_skillchips button {
  min-width: 0; padding: 0.15rem 0.55rem; font-size: var(--ag-fs-xs);
  border-radius: var(--ag-radius-nav);
}
.st-key-panel_skillchips > div { width: max-content !important; }

/* Threads and settings views: both replace the conversation, so both pad
   themselves and both use the header row's grammar. */
.st-key-chatpanel .st-key-panel_threadhead,
.st-key-chatpanel .st-key-panel_settingshead {
  gap: 0.5rem; padding: 0.5rem 0.75rem 0.25rem;
}
.st-key-chatpanel .st-key-panel_view { padding-inline: 0.75rem; overflow: auto; }
/* The open thread is marked the way the sidebar marks the current page: a
   filled row with an accent edge. It stays pressable — from this view,
   pressing it is the way back to it. */
.st-key-chatpanel .st-key-panel_row_active {
  background: var(--ag-purple-900); border-left: 2px solid var(--ag-brand-accent);
  border-radius: var(--ag-radius-nav);
}
.st-key-chatpanel [class*="st-key-panel_row_"] { padding: 0.1rem 0.25rem; }
.st-key-chatpanel [class*="st-key-panel_row_"] button {
  justify-content: flex-start; text-align: left;
}
.st-key-chatpanel [class*="st-key-panel_menu_"] { width: max-content !important; }
/* A confirmation is not an error block: it is the row, raised. */
.st-key-chatpanel .st-key-panel_confirm,
.st-key-chatpanel .st-key-panel_clearconfirm {
  background: var(--ag-surface-card); border: 1px solid var(--ag-border-focus);
  border-radius: var(--ag-radius-sm); padding: 0.6rem; margin: 0.25rem 0.75rem;
}
/* Delete is the one admitted exception to the reserved market reds: this is
   destruction, not a price. */
.st-key-chatpanel [class*="st-key-panel_delyes_"] button,
.st-key-chatpanel .st-key-panel_clear_yes button,
.st-key-chatpanel .st-key-panel_clear_thread button {
  color: var(--ag-down) !important;
}
.st-key-chatpanel [class*="st-key-panel_delyes_"] button:hover,
.st-key-chatpanel .st-key-panel_clear_yes button:hover {
  background: var(--ag-down-fill) !important;
  border-color: var(--ag-down-fill) !important;
}
/* The conversation and the input carry the horizontal padding the panel gave
   up, so the header rows can run full-bleed. */
.st-key-chatpanel .st-key-panel_scroll { padding-inline: 0.6rem; }
.st-key-chatpanel [data-testid="stChatInput"] { margin-inline: 0.75rem; }

/* Turn chrome rows: thin, tight, and the popover triggers inside them read as
   instrumentation rather than buttons. */
.st-key-chatpanel [class*="_thead_"], .st-key-chatpanel [class*="_tfoot_"] {
  gap: 0.4rem; align-items: center;
}
.st-key-chatpanel [class*="_thead_"] button,
.st-key-chatpanel [class*="_tfoot_"] button {
  min-width: 0; padding: 0.05rem 0.35rem; background: transparent;
  border-color: transparent; font-family: "Martian Mono", monospace;
  font-size: var(--ag-fs-2xs); color: var(--ag-text-faint) !important;
}
.st-key-chatpanel [class*="_thead_"] button:hover,
.st-key-chatpanel [class*="_tfoot_"] button:hover {
  background: var(--ag-surface-hover); color: var(--ag-text-secondary) !important;
}
.st-key-chatpanel [class*="_thead_"] > div,
.st-key-chatpanel [class*="_tfoot_"] > div { width: max-content !important; }
/* The clock is pushed to the far end of the foot row. */
.st-key-chatpanel [class*="_tfoot_"] > div:last-child { margin-left: auto; }
/* A failure keeps the answer's shape but takes a visible edge, so it is not
   mistaken for something the model said. */
.st-key-chatpanel [class*="_notice_"] [data-testid="stChatMessageContent"] {
  border: 1px solid var(--ag-border-focus);
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
  // Any CSS length, not just a pixel count: the header's presets store
  // '380px', '720px' or '100vw' under this same key. Bare integers are what
  // every build before the presets wrote, and they must get their unit back:
  // 'min(380, 100vw)' is invalid, which drops the width declaration and
  // stretches the panel across the whole page.
  const raw = (localStorage.getItem('chatPanelWidth') || '').trim();
  const saved = /^[0-9]+$/.test(raw) ? raw + 'px' : raw;
  if (/^[0-9]+(px|vw)$/.test(saved)) {{
    root.style.setProperty('--chat-w', saved);
    if (saved !== raw) {{  // rewrite the legacy unit-less value once
      try {{ localStorage.setItem('chatPanelWidth', saved); }} catch (e) {{}}
    }}
  }}

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
      if (px) localStorage.setItem('chatPanelWidth', px + 'px');
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
    switching view, provider, model or key) reruns only the panel.

    Fully self-contained: with no provider key configured it shows the BYOK
    setup inline; once configured it draws the two header rows and then
    whichever view the drawer is on — the thread, the thread list, or the
    account settings.
    """
    provider = active_provider()
    if provider is None:  # no SDK installed — should not happen once deps sync
        st.error("No LLM provider is installed.")
        return

    if provider.needs_key and not active_key(provider):
        _render_setup("panel")  # free path first, key form under it
        return

    conv = auth.active_conversation()
    model = _active_model(provider)
    _render_panel_head("panel", conv)
    quota = _render_status_strip("panel", provider, model)
    _apply_width()  # outside the header row: a script block would gap it

    # The counter is filled last, whichever way the body ends — including the
    # early return on a spent cap, which is the one moment the number is
    # worth reading.
    try:
        view = _drawer_view()
        if view == "threads":
            _render_threads_view("panel")
            return
        if view == "settings":
            _render_settings_view("panel", conv)
            return

        key = active_key(provider)  # settings may have just switched it
        if provider.needs_key and not key:
            _key_gate(provider)  # newly-selected provider has no key -> prompt
            return
        render_conversation("panel", provider, model, key)
    finally:
        _fill_quota(quota)


# ------------------------------------------------- open across a page reload
# A reload is a brand-new Streamlit session, so an open panel held only in
# session state would collapse back to the launcher icon — mid-conversation,
# and with no way to tell that the thread is still there. The flag is really a
# per-user setting, so it lives in prefs alongside the other things that
# survive a reload (currency, recent searches).
_OPEN_PREF = "chat_panel_open"


def _panel_is_open() -> bool:
    """Whether the panel is open on this run, seeded from prefs once.

    Prefs are read on the first run of a session only. After that session
    state is the truth: a close has to stay closed for the rest of the run
    that follows it, while prefs still say open until the write-through below
    lands."""
    if _OPEN_PREF not in st.session_state:
        st.session_state[_OPEN_PREF] = bool(auth.load_prefs().get(_OPEN_PREF, False))
    return bool(st.session_state[_OPEN_PREF])


def _remember_open(is_open: bool) -> None:
    """Write the state through to prefs, and only when it actually changed.

    Persisting here rather than at each toggle is what keeps it honest: the
    launcher, the close icon, the Home card's "ask about this" and the daily
    brief all set the flag and then rerun through `render_side_panel`, so one
    write-through covers every one of them without each having to remember to
    save. The guard keeps a settled state from re-uploading prefs (save_prefs
    mirrors to the bucket) on every script run."""
    prefs = auth.load_prefs()
    if bool(prefs.get(_OPEN_PREF, False)) == is_open:
        return
    prefs[_OPEN_PREF] = is_open
    auth.save_prefs(prefs)


def ask(question: str) -> None:
    """Open the assistant with `question` already asked. Does not return.

    The entry point for the pages' "analyse this" buttons (the ticker header's
    Analyse with AI). `question` lands on the same seed key the opening
    screen's suggestion chips write, so it goes through the identical turn
    pipeline — rate limit, action probe, skill routing, quota — instead of a
    shortcut that would drift from it. The drawer is forced back to the thread
    view: a seed left sitting behind the thread list or the settings screen
    would fire whenever the reader happened to come back.

    Reruns the whole app, not the fragment: the panel is drawn by app.py, so a
    fragment-scoped rerun would set the flag and repaint nothing.
    """
    st.session_state[_seed_key("panel")] = question
    st.session_state[_OPEN_PREF] = True
    st.session_state["chat_drawer_view"] = "thread"
    st.rerun(scope="app")


def render_side_panel(view_label: str) -> None:
    """Overlay assistant: launcher icon + slide-in panel. Call from app.py
    BEFORE page.run() — the launcher is position: fixed, so DOM order doesn't
    matter, and rendering first keeps it alive when a page raises or calls
    st.stop(). Every page, signed-in users only."""
    st.session_state["_chat_view"] = view_label  # read by _view_context()
    css.inject(_PANEL_CSS)

    is_open = _panel_is_open()
    _remember_open(is_open)
    if not is_open:
        with st.container(key="chatfab"):
            if st.button("", icon=":material/auto_awesome:", key="chat_fab_open",
                         type="primary", help=tr("chat.title")):
                st.session_state["chat_panel_open"] = True
                st.rerun()
        return

    # No title row of its own: the panel's first row is the header the
    # fragment draws (thread name, new, widths, close), so a rerun that
    # switches view or thread repaints it too.
    with st.container(key="chatpanel"):
        _panel_body()
    # Emitted after the panel exists so the handle can attach. Outside the
    # fragment, so sending a chat message does not re-run this script.
    st.html(_RESIZE_JS, unsafe_allow_javascript=True)
