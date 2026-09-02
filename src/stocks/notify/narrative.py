"""Optional one-line LLM narrative for the daily digest.

Provider resolution, first success wins:
  1. The user's own BYOK key (Fernet-encrypted in prefs.json, same sliding
     90-day TTL the chat honours) — their key, their billing, their provider
     choice. The digest only *reads* the key: it never slides the window, so
     an account that stopped chatting still goes cold on schedule.
  2. The operator's free chain ([free_llm] secrets / FREE_LLM_* env).
  3. None — the digest ships computed-only.

Everything here is sandboxed: any exception or a hard timeout falls through
to the next step. A digest must never fail, or even stall, because of an LLM.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from stocks.chat import engine

_TTL = engine.BYOK_TTL  # the shared "remembered for 90 days, sliding" promise
_LANG_NAME = {"en": "English", "es": "Spanish"}
MAX_CHARS = 300

# BYOK decryption and provider resolution live in the shared chat engine now
# (stocks/chat/engine.py); these names stay for the callers and tests.
_decrypt_byok = engine.decrypt_byok


def _prompt(data, lang: str) -> tuple[str, list[dict]]:
    system = (
        "You are the portfolio assistant for TopStocks, a stock-tracking app. "
        "Given today's portfolio numbers, write exactly 1-2 sentences of "
        f"insight in {_LANG_NAME.get(lang, 'English')}. Plain text only: no "
        "markdown, no emoji, no preamble. Mention what drove the day and "
        "anything worth watching."
    )
    facts = {
        "date": data.date.isoformat(),
        "total": data.total,
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


_attempts = engine.attempts


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
