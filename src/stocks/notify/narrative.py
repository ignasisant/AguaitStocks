"""Optional one-line LLM narrative for the daily digest.

Provider resolution, first success wins:
  1. The user's own BYOK key (Fernet-encrypted in prefs.json, same 15-day TTL
     the chat honours) — their key, their billing, their provider choice.
  2. The operator's free chain ([free_llm] secrets / FREE_LLM_* env).
  3. None — the digest ships computed-only.

Everything here is sandboxed: any exception or a hard timeout falls through
to the next step. A digest must never fail, or even stall, because of an LLM.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from stocks.secrets_env import secret

_TTL = 15 * 24 * 3600  # keep in sync with chat_core._TTL (the "15 days" promise)
_BYOK_ORDER = ("anthropic", "openai", "gemini")
_LANG_NAME = {"en": "English", "es": "Spanish"}
MAX_CHARS = 300


def _decrypt_byok(prefs: dict, pid: str) -> str:
    """The user's stored key for `pid`, or '' (missing / expired / bad token)."""
    try:
        from cryptography.fernet import Fernet

        enc_key = secret("CHAT_ENC_KEY", "chat", "enc_key")
        token = prefs.get(f"{pid}_key_enc")
        saved_at = prefs.get(f"{pid}_key_saved_at", 0)
        if not enc_key or not token or time.time() - saved_at > _TTL:
            return ""
        return Fernet(enc_key).decrypt(token.encode()).decode()
    except Exception:
        return ""


def _prompt(data, lang: str) -> tuple[str, list[dict]]:
    system = (
        "You are the portfolio assistant for Aguait, a stock-tracking app. "
        "Given today's portfolio numbers, write exactly 1-2 sentences of "
        f"insight in {_LANG_NAME.get(lang, 'English')}. Plain text only: no "
        "markdown, no emoji, no preamble. Mention what drove the day and "
        "anything worth watching."
    )
    facts = {
        "date": data.date.isoformat(),
        "total_eur": data.total_eur,
        "day_change": data.day and {"eur": round(data.day[0], 2),
                                    "pct": round(data.day[1] * 100, 2)},
        "week_change": data.week and {"eur": round(data.week[0], 2),
                                      "pct": round(data.week[1] * 100, 2)},
        "session_moves_pct": {t: round(v * 100, 2) for t, v in data.movers[:10]},
        "earnings_next_7d": [
            {"ticker": e.ticker, "date": e.date.isoformat(), "in_days": e.days_until}
            for e in data.earnings
            if e.date
        ],
    }
    import json

    return system, [{"role": "user", "content": json.dumps(facts)}]


def _attempts(prefs: dict):
    """(provider, api_key, model) candidates in resolution order."""
    from stocks.web import llm

    seen = []
    preferred = prefs.get("llm_provider")
    for pid in dict.fromkeys([preferred, *_BYOK_ORDER]):
        if not pid or pid == "free" or pid not in llm.PROVIDERS:
            continue
        key = _decrypt_byok(prefs, pid)
        if key:
            provider = llm.PROVIDERS[pid]
            seen.append((provider, key, prefs.get(f"{pid}_model") or ""))
    free = llm.PROVIDERS["free"]
    if free.available():
        seen.append((free, "", ""))
    return seen


def _sanitize(text: str) -> str | None:
    line = " ".join(text.split()).strip()
    if not line:
        return None
    return line[:MAX_CHARS].rstrip() if len(line) > MAX_CHARS else line


def highlight(data, prefs: dict, lang: str, timeout_s: float = 45.0) -> str | None:
    """1-2 sentence narrative for the digest, or None. Never raises."""
    try:
        system, messages = _prompt(data, lang)
        attempts = _attempts(prefs)
    except Exception:
        return None
    for provider, key, model in attempts:
        # No `with`: executor shutdown would block on the hung worker and
        # defeat the timeout. The daemon-less thread dies with the process —
        # acceptable for a short-lived cron.
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(provider.complete, key, model, system, messages)
            out = _sanitize(future.result(timeout=timeout_s))
            if out:
                return out
        except Exception:
            continue  # timeout, bad key, rate limit — next candidate
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return None
