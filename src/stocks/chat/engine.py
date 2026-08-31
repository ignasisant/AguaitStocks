"""Headless chat engine — the web assistant's brain without the Streamlit UI.

Everything the side panel (web/chat_core.py) and the Telegram bot
(stocks/chat/bot.py) share lives here: the persona built from the investor
profile, the portfolio snapshot for the system prompt, skill routing, the
BYOK→free provider resolution (also used by notify/narrative.py), the free
-tier daily quota, and answer() — one complete chat turn against explicit
paths, no session state. chat_core wraps these helpers with st.session_state
and its cached loaders; this module never imports streamlit.

Write discipline: answer() saves chat.json only after a completed
user+assistant pair (matching the web panel), and mutates/saves prefs.json
only for the free-quota counter. A live web session writing the same files is
last-write-wins — accepted, the overlap window is a single turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from stocks import obs
from stocks.chat import market
from stocks.secrets_env import secret
from stocks.web import chat_skills, chat_web

if TYPE_CHECKING:
    import pandas as pd

    from stocks.chat.tools import Action
    from stocks.web.llm import Provider

# Remembered-key lifetime. The window *slides*: every successful use of a
# stored key pushes BYOK_TTL out again, so an active account never re-enters
# its key, while an abandoned one goes cold on its own. BYOK_MAX_AGE is the
# absolute ceiling measured from the moment the key was entered and is never
# refreshed, so no stored key can live indefinitely. Expiry is not just a read
# check: prune_byok deletes the ciphertext, so dead keys stop sitting in
# prefs.json (and in the bucket mirror).
BYOK_TTL = 90 * 24 * 3600  # sliding window, seconds
BYOK_MAX_AGE = 180 * 24 * 3600  # hard cap since first save, seconds
_BYOK_TOUCH_MIN = 24 * 3600  # slide at most once a day (each write hits the bucket)
_BYOK_ORDER = ("anthropic", "openai", "gemini")

# The free chain runs on the operator's shared keys, so each account gets a
# modest daily allowance — one runaway user must not drain the quota every
# other account depends on. Override: FREE_LLM_DAILY_CAP env (headless) or
# [free_llm] daily_cap (secrets.toml).
FREE_DAILY_CAP = 30

# How many past messages to actually send the model per request. The full
# thread stays on disk; only this tail is re-sent, so cost stops growing
# quadratically with conversation length. ~10 exchanges of memory.
MAX_CONTEXT_MSGS = 20

# Prepended to the system prompt for Telegram turns: Telegram renders no
# markdown tables/headers, so steer the model at the source.
TELEGRAM_CONTEXT = (
    "The user is chatting from Telegram. Answer in plain text: no markdown "
    "tables, no headers, no code fences; short paragraphs and simple dashes "
    "for lists.\n\n"
)


# ------------------------------------------------------------ provider keys


def byok_fields(pid: str) -> tuple[str, str, str]:
    """The three prefs keys holding one provider's remembered key."""
    return f"{pid}_key_enc", f"{pid}_key_saved_at", f"{pid}_key_first_at"


def byok_alive(prefs: dict, pid: str) -> bool:
    """True while `pid`'s stored key is inside both windows (sliding + cap).

    Entries written before the cap existed have no `_key_first_at`; their own
    save time stands in for it, so the ceiling counts from the true origin
    rather than restarting on upgrade.
    """
    enc_k, saved_k, first_k = byok_fields(pid)
    if not prefs.get(enc_k):
        return False
    try:
        saved = float(prefs.get(saved_k, 0) or 0)
        first = float(prefs.get(first_k, saved) or saved)
    except (TypeError, ValueError):
        return False
    now = time.time()
    return now - saved <= BYOK_TTL and now - first <= BYOK_MAX_AGE


def decrypt_byok(prefs: dict, pid: str) -> str:
    """The user's stored key for `pid`, or '' (missing / expired / bad token)."""
    try:
        from cryptography.fernet import Fernet

        enc_key = secret("CHAT_ENC_KEY", "chat", "enc_key")
        enc_k, _, _ = byok_fields(pid)
        if not enc_key or not byok_alive(prefs, pid):
            return ""
        return Fernet(enc_key).decrypt(prefs[enc_k].encode()).decode()
    except Exception:
        return ""


def touch_byok(prefs: dict, pid: str) -> bool:
    """Slide `pid`'s window after a successful use. True when prefs changed.

    Mutates `prefs` in place — the caller saves. Throttled to one write a day
    so a busy panel doesn't re-upload prefs.json on every rerun.
    """
    _, saved_k, first_k = byok_fields(pid)
    if not byok_alive(prefs, pid):
        return False
    now = int(time.time())
    saved = int(float(prefs.get(saved_k, 0) or 0))
    if now - saved < _BYOK_TOUCH_MIN:
        return False
    prefs.setdefault(first_k, saved or now)  # legacy entry: origin = its save time
    prefs[saved_k] = now
    return True


def prune_byok(prefs: dict) -> bool:
    """Delete every expired stored key, ciphertext included. True when changed.

    Expiry has to remove the token, not merely refuse to read it: otherwise an
    abandoned account keeps a decryptable provider key in prefs.json and in the
    bucket forever. Mutates `prefs` in place — the caller saves.
    """
    changed = False
    for enc_k in [k for k in list(prefs) if k.endswith("_key_enc")]:
        pid = enc_k[: -len("_key_enc")]
        if byok_alive(prefs, pid):
            continue
        for field in byok_fields(pid):
            changed = prefs.pop(field, None) is not None or changed
    return changed


def maintain_byok(prefs: dict, pid: str | None = None) -> bool:
    """Slide the key just used (if any) and drop the dead ones. True when
    prefs changed and the caller should save."""
    touched = touch_byok(prefs, pid) if pid else False
    return prune_byok(prefs) or touched


def attempts(prefs: dict) -> list[tuple[Provider, str, str]]:
    """(provider, api_key, model) candidates in resolution order.

    First the user's preferred provider, then the BYOK order — each only with
    a decryptable key — then the operator's keyless free chain. The model is
    the user's saved pref or '' (callers substitute the provider default).
    """
    from stocks.web import llm

    seen = []
    preferred = prefs.get("llm_provider")
    for pid in dict.fromkeys([preferred, *_BYOK_ORDER]):
        if not pid or pid == "free" or pid not in llm.PROVIDERS:
            continue
        key = decrypt_byok(prefs, pid)
        if key:
            provider = llm.PROVIDERS[pid]
            seen.append((provider, key, prefs.get(f"{pid}_model") or ""))
    free = llm.PROVIDERS["free"]
    if free.available():
        seen.append((free, "", ""))
    return seen


# ------------------------------------------------------------- free quota


def free_daily_cap() -> int:
    try:
        return int(secret("FREE_LLM_DAILY_CAP", "free_llm", "daily_cap")
                   or FREE_DAILY_CAP)
    except (TypeError, ValueError):
        return FREE_DAILY_CAP


# Cost backstop across ALL accounts: the per-account cap bounds one user, this
# bounds the process — N signed-up accounts times the account cap is otherwise
# the real daily ceiling on shared free-tier keys. In-memory on purpose: the
# web app is one container (the bot process gets its own, much smaller, run),
# and a restart forgetting the counter only ever errs generous.
FREE_GLOBAL_DAILY_CAP = 400
_global_free: dict = {"day": "", "used": 0}


def free_global_daily_cap() -> int:
    try:
        return int(secret("FREE_LLM_GLOBAL_DAILY_CAP", "free_llm", "global_daily_cap")
                   or FREE_GLOBAL_DAILY_CAP)
    except (TypeError, ValueError):
        return FREE_GLOBAL_DAILY_CAP


def _spend_global_free() -> bool:
    day = time.strftime("%Y-%m-%d")
    if _global_free["day"] != day:
        _global_free["day"], _global_free["used"] = day, 0
    if _global_free["used"] >= free_global_daily_cap():
        obs.event("llm.free.global_cap")
        return False
    _global_free["used"] += 1
    return True


def spend_free_quota(prefs: dict) -> bool:
    """Consume one unit of today's free allowance; False when it's spent.

    Mutates `prefs` in place — the caller saves, so the web panel and the
    Telegram bot share one counter ("free_msgs::<date>"). Keys from previous
    days are dropped on spend so prefs.json never accumulates. The account
    counter is checked first so a capped-out user can't drain the global pot.
    """
    day = time.strftime("%Y-%m-%d")
    key = f"free_msgs::{day}"
    used = int(prefs.get(key, 0))
    if used >= free_daily_cap():
        return False
    if not _spend_global_free():
        return False
    for stale in [k for k in prefs if k.startswith("free_msgs::") and k != key]:
        prefs.pop(stale)
    prefs[key] = used + 1
    return True


# ---------------------------------------------------------------- persona
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


def persona(prof: dict) -> str:
    """The 'who am I advising' sentence, from a loaded investor profile
    (auth.load_profile()). Falls back to the historical default when the user
    hasn't filled the form yet (prof['set'] is False)."""
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
    cons = [_CONSTRAINT_EN[c] for c in prof.get("constraints", [])
            if c in _CONSTRAINT_EN]
    if cons:
        out += "Always respect these constraints: " + _join_en(cons) + ". "
    notes = (prof.get("notes") or "").strip()
    if notes:
        out += f"Additional context from the user: {notes} "
    return out


# ------------------------------------------------------------ book snapshot


def _fmt_eur(x) -> str:
    return f"€{x:,.0f}" if x is not None and x == x else "n/a"  # x==x screens NaN


def book_snapshot(tbl: pd.DataFrame | None, watchlist: Path) -> str:
    """The system prompt's snapshot of the user's real book.

    `tbl` is the live-priced positions frame (web: cached enriched_positions;
    headless: enriched_frame) or None — then the watchlist's positions
    (shares/cost only) are the fallback. Watchlist-but-not-held names are
    appended either way.
    """
    from stocks.config import load_watchlist
    from stocks.config import positions as load_positions

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
        holds = load_positions(watchlist)
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
        h.ticker for h in load_watchlist(watchlist) if h.ticker not in held
    ]
    if watching:
        book += "\n\nAlso on the watchlist (not held): " + ", ".join(watching)
    return book


def enriched_frame(db: Path) -> pd.DataFrame | None:
    """Uncached headless analog of web/portfolio_data.enriched_positions:
    ledger → FIFO positions → live-priced EUR frame + weight + day change
    from the basket history's last two closes. None when there is no ledger
    or no open positions. Skips the web-only market-closed day override (a
    display nicety the prompt doesn't need)."""
    from stocks.analysis.portfolio import (
        position_values_history,
        positions_frame_eur,
    )
    from stocks.portfolio.ledger import all_transactions
    from stocks.portfolio.positions import build

    txs = all_transactions(db)
    if not txs:
        return None
    positions, _ = build(txs)
    tbl = positions_frame_eur(positions)
    if tbl.empty:
        return None
    value = tbl["value_eur"].dropna().sum()
    tbl["weight"] = tbl["value_eur"] / value if value else float("nan")
    vals = position_values_history(positions, period="1mo")
    if len(vals) >= 2:
        last, prev = vals.iloc[-1], vals.iloc[-2]
        tbl["day_pct"] = (last / prev - 1).reindex(tbl.index)
    else:
        tbl["day_pct"] = float("nan")
    return tbl.sort_values("weight", ascending=False, na_position="last")


def portfolio_context(watchlist: Path, db: Path) -> str:
    """Headless twin of chat_core._portfolio_context, from explicit paths."""
    tbl = enriched_frame(db) if db.exists() else None
    return book_snapshot(tbl, watchlist)


# ------------------------------------------------------------ system prompt


def system_prompt(profile: dict, context: str,
                  skill_ids: list[str] | None = None) -> str:
    """Persona + the caller's context block (view + book snapshot) + the
    analysis frameworks chosen for this turn."""
    return (
        "You are a concise investing assistant embedded in a personal stock "
        "tracker. " + persona(profile) + "You are not a licensed financial "
        "advisor: give analysis and trade-offs, not directives, and flag when "
        "something needs the user's own judgement. The context below is "
        "current as of this message; treat the figures as the user's real "
        "position, and let the current view guide what they are most likely "
        f"asking about. Today is {date.today().isoformat()}. Some user "
        "messages carry appended web page extracts and live market quotes "
        "fetched at send time; when present, ground your answer in them, "
        "prefer those figures over anything you remember, and cite the "
        "source URLs you use. Never claim you cannot access the internet or "
        "current prices — say what the fetched material does or does not "
        "cover.\n\n"
        f"{context}"
        + chat_skills.skills_block(skill_ids or [])
    )


def recent(history: list[dict], limit: int = MAX_CONTEXT_MSGS) -> list[dict]:
    """The tail of the conversation sent to the model. Trims any leading
    assistant turn so the slice still opens with a user message (Anthropic
    requires it; the others don't care). Rebuilt as bare role/content dicts —
    stored turns carry extra keys (e.g. "skills") the provider APIs reject."""
    msgs = history[-limit:]
    while msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def resolve_skills(prefs: dict, provider: Provider, api_key: str,
                   history: list[dict], context: str = "") -> list[str]:
    """Skill ids to apply to the pending answer, per the saved mode.

    Auto routes the message through the provider's cheapest model. When the
    router call itself fails it falls back to the previous answer's skills —
    the answer always proceeds, and an unchanged skill set keeps the system
    prompt byte-identical, which keeps provider prompt caches warm."""
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
    ctx = context.strip()
    if prior:
        ctx += "\nEarlier user messages (topic continuity): " + " | ".join(prior)
    ids = chat_skills.classify(provider, api_key, history[-1]["content"], ctx)
    if ids is None:  # router failed — reuse the previous turn's lens
        prev = [m for m in history if m["role"] == "assistant" and m.get("skills")]
        return prev[-1]["skills"] if prev else []
    return ids


# -------------------------------------------------------------- web search


def web_enabled(prefs: dict) -> bool:
    """Whether this turn may touch the internet: the "chat_web" pref (default
    on) and a working ddgs install."""
    return chat_web.available() and bool(prefs.get("chat_web", True))


def plan_web(prefs: dict, provider: Provider, api_key: str,
             history: list[dict], context: str = "") -> list[str]:
    """Search queries for the pending answer ([] = none needed / web off).

    Same planner as the web panel (chat_web.plan on the provider's cheapest
    model), with the caller's context (the current view, for the panel) and
    prior user turns riding along for topic continuity."""
    if not web_enabled(prefs):
        return []
    prior = [m["content"][:200] for m in history[:-1] if m["role"] == "user"][-2:]
    ctx = f"Today is {date.today().isoformat()}." + (
        "\n" + context.strip() if context.strip() else "")
    if prior:
        ctx += "\nEarlier user messages (topic continuity): " + " | ".join(prior)
    return chat_web.plan(provider, api_key, history[-1]["content"], ctx)


def ground_web(prefs: dict, provider: Provider, api_key: str,
               history: list[dict], context: str = "") -> list[chat_web.Result]:
    """The pages this turn reads: planned searches plus any pasted links.

    The "chat_web" pref gates the whole thing — off means no internet at all,
    pasted links included."""
    if not web_enabled(prefs):
        return []
    return chat_web.collect(plan_web(prefs, provider, api_key, history, context),
                            history[-1]["content"])


def in_parallel(*calls: Callable[[], object],
                timeout: float | None = None) -> list:
    """Run this turn's independent lookups at once, in order of the results.

    Skill routing, search planning + page reading and quote fetching share no
    inputs, and back to back they are the bulk of a turn's latency (two
    classifier calls, three page fetches, a Yahoo round-trip). A call that
    raises or overruns yields None: one dead lookup must not take the answer
    with it. Callers on Streamlit must resolve session state *before* handing
    a closure over — these run off the script thread.
    """
    pool = ThreadPoolExecutor(max_workers=max(1, len(calls)))
    try:
        futures = [pool.submit(c) for c in calls]
        out: list = []
        for f in futures:
            try:
                out.append(f.result(timeout=timeout))
            except Exception:
                out.append(None)
        return out
    finally:
        # No wait: shutdown would block on whatever the timeout just escaped.
        pool.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------ thread titles


TITLE_MAX_CHARS = 60

_TITLE_SYSTEM = (
    "You name chat conversations. Reply with ONLY a title for the "
    "conversation that the message below opens: at most 6 words, no quotes, "
    "no trailing period, written in the same language as the message. Name "
    "the subject (ticker, company, topic), not the request."
)


def _trim(text: str) -> str:
    """Cap a title at TITLE_MAX_CHARS on a word boundary, not mid-word."""
    text = text.strip()
    if len(text) <= TITLE_MAX_CHARS:
        return text
    cut = text[:TITLE_MAX_CHARS]
    head, sep, _ = cut.rpartition(" ")
    return ((head if sep and len(head) >= TITLE_MAX_CHARS // 2 else cut).rstrip(
        " ,;:-") + "\u2026")


def title_for(provider: Provider, api_key: str, message: str) -> str:
    """A short conversation title for `message`.

    One call on the provider's cheapest model — the same shape as the skill
    router. Any failure (network, empty reply) degrades to a trimmed copy of
    the message itself, so a thread is never left unnamed."""
    fallback = _trim(" ".join(message.split()))
    try:
        raw = provider.complete(
            api_key,
            provider.classifier_model or provider.default_model,
            _TITLE_SYSTEM,
            [{"role": "user", "content": message[:500]}],
        )
    except Exception:
        return fallback
    title = " ".join((raw or "").split()).strip().strip("\"'\u201c\u201d").rstrip(".")
    return _trim(title) or fallback


def autotitle(chat_path: Path, provider: Provider, api_key: str,
              history: list[dict]) -> None:
    """Name the active thread from its opening question, once.

    Only fires on the first completed pair of a still-unnamed, never-renamed
    conversation, so the extra classifier call happens once per thread and
    never for a title the user chose. Failures are swallowed: a nameless
    thread must not cost the user an answer."""
    if len(history) != 2 or history[0]["role"] != "user":
        return
    from stocks.web import auth

    try:
        conv = auth.active_conversation(chat_path)
        if conv.get("title") or not conv.get("title_auto", True):
            return
        auth.autotitle_conversation(
            conv["id"], title_for(provider, api_key, history[0]["content"]),
            chat_path,
        )
    except Exception:
        return


# ----------------------------------------------------------------- actions


def action_context(watchlist: Path) -> str:
    """What the action parser needs, headless: the watchlist (resolves
    company names to symbols) and existing groups. No 'current view' — there
    is none on Telegram."""
    from stocks.config import load_watchlist
    from stocks.web import auth

    bits = []
    holds = load_watchlist(watchlist)
    if holds:
        bits.append("Watchlist: " + ", ".join(
            f"{h.ticker} ({h.name})" if h.name else h.ticker for h in holds))
    tags = auth.all_tags(watchlist)
    if tags:
        bits.append("Existing groups: " + ", ".join(tags))
    return "\n".join(bits)


def action_reply(act: Action, lang: str) -> str:
    """Localized confirmation line for an executed action (explicit lang).

    The per-tool wording lives with the tools (chat/tools.py); this only
    binds the recipient's language to the translator they hand it."""
    from stocks.chat import tools
    from stocks.web.i18n import translate

    return tools.reply(act, lambda key, **kw: translate(key, lang, **kw))


# ------------------------------------------------------------------ answer


@dataclass(frozen=True)
class Reply:
    """One chat turn's outcome. `error` is a locale key when text is empty.
    `sources` are the {title, url} dicts of web hits that grounded the
    answer (also stored on the history turn under "web", like the panel)."""

    text: str = ""
    skills: tuple[str, ...] = ()
    sources: tuple[dict, ...] = ()
    provider_id: str = ""
    error: str | None = None


def _keep_byok(prefs: dict, prefs_path: Path, pid: str) -> None:
    """After a served turn: slide the key that served it, drop expired ones.

    The free chain is the operator's key, so a free turn slides nothing — it
    only gets the prune.
    """
    from stocks.web import auth

    if maintain_byok(prefs, None if pid == "free" else pid):
        auth.save_prefs(prefs, prefs_path)


def answer(*, prefs: dict, prefs_path: Path, chat_path: Path,
           watchlist: Path, db: Path, message: str, lang: str = "en",
           timeout_s: float = 90.0) -> Reply:
    """One complete chat turn: load history, resolve provider/skills, ask,
    append the completed pair, save. Mirrors the web panel's turn logic.

    On any failure the history is left unsaved (no dangling user turn) and
    the Reply carries a locale key: chat.free_cap, chat.free_exhausted or
    chat.api_error.
    """
    from stocks.chat import tools
    from stocks.web import auth

    history = auth.load_chat(chat_path)
    history.append({"role": "user", "content": message})

    atts = attempts(prefs)
    if not atts:
        return Reply(error="chat.free_exhausted")
    provider, key, _ = atts[0]

    # App actions first (favorite / alerts / groups): a deterministic
    # localized confirmation — no main-model call, no free-quota spend.
    if tools.maybe_action(message):
        act = tools.detect(provider, key, message, action_context(watchlist))
        if act is not None:
            try:
                tools.execute(act, watchlist)
            except Exception:
                act = None
        if act is not None:
            note = action_reply(act, lang)
            history.append({"role": "assistant", "content": note,
                            "action": act.kind})
            auth.save_chat(history, chat_path)
            autotitle(chat_path, provider, key, history)
            _keep_byok(prefs, prefs_path, provider.id)
            return Reply(text=note, provider_id=provider.id)

    # Skill routing, the web lookup and the quotes are independent, so they
    # run at the same time rather than stacking their latencies.
    skills, hits, live = in_parallel(
        lambda: resolve_skills(prefs, provider, key, history,
                               context=TELEGRAM_CONTEXT),
        lambda: ground_web(prefs, provider, key, history),
        lambda: market.lookup_for(message, watchlist),
        timeout=timeout_s,
    )
    skills, hits, live = skills or [], hits or [], live or []
    system = system_prompt(
        auth.load_profile(prefs),
        TELEGRAM_CONTEXT + portfolio_context(watchlist, db),
        skills,
    )
    # Web hits and live quotes ride on the outgoing copy of the user turn,
    # not the system prompt — the stored history keeps the user's own text
    # (same as the panel).
    msgs = recent(history)
    if hits:
        msgs[-1]["content"] = chat_web.augment(msgs[-1]["content"], hits)
    if live:
        msgs[-1]["content"] = market.augment(msgs[-1]["content"], live)
    web_sources = chat_web.sources(hits)

    capped = False
    for provider, key, model in atts:
        if provider.id == "free":
            if not spend_free_quota(prefs):
                capped = True
                continue
            auth.save_prefs(prefs, prefs_path)  # counter spent, like the web
        # No `with`: executor shutdown would block on a hung worker and defeat
        # the timeout. The thread dies with the short-lived process.
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(provider.complete, key,
                                 model or provider.default_model, system, msgs)
            text = (future.result(timeout=timeout_s) or "").strip()
        except Exception as exc:
            # timeout, bad key, rate limit — next candidate. Logged because the
            # user only ever sees chat.api_error; without this the reason for a
            # dead chain (retired model, free tier gone paid) is unrecoverable.
            obs.warn("chat.provider_failed", provider=provider.id,
                     model=model or provider.default_model,
                     error_type=type(exc).__name__, error=str(exc)[:300])
            continue
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if text:
            turn: dict = {"role": "assistant", "content": text}
            if skills:
                turn["skills"] = list(skills)
            if web_sources:
                turn["web"] = web_sources
            history.append(turn)
            auth.save_chat(history, chat_path)
            autotitle(chat_path, provider, key, history)
            _keep_byok(prefs, prefs_path, provider.id)
            obs.event("chat.answered", provider=provider.id,
                      model=model or provider.default_model,
                      chars=len(text), skills=list(skills),
                      web_sources=len(web_sources))
            return Reply(text=text, skills=tuple(skills),
                         sources=tuple(web_sources), provider_id=provider.id)

    obs.warn("chat.failed", reason="free_cap" if capped else "api_error",
             providers=[p.id for p, _k, _m in atts])
    return Reply(error="chat.free_cap" if capped else "chat.api_error")
