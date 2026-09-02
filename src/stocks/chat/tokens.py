"""Context budget for a chat turn: count tokens, and trim the thread to fit.

`engine.recent` trims by *message count* (MAX_CONTEXT_MSGS), which is the
wrong unit and was only ever a proxy: twenty one-line messages and twenty
messages each carrying three read web pages differ by two orders of magnitude,
and the expensive one is invisible to a counter. Worse, the growth happens
after the trim — chat_web.augment and market.augment staple page extracts and
live quotes onto the *outgoing copy* of the last user turn, so the biggest
message in the request is one that no cap has ever seen.

This module is the cap in the right unit, applied last, after augmentation.
Oldest turns go first; the newest turn is never dropped (it is the question),
only truncated from its tail if it alone busts the budget — the tail is where
the appended material lives, so the user's actual words survive.

Counting is tiktoken's when it is installed and its BPE data is on disk,
otherwise a character ratio. Both are estimates for Anthropic and Gemini,
whose tokenizers are not public — which is fine, because this is a guard
against pathological threads, not a billing meter. It must never be the reason
a turn fails, so every path here degrades instead of raising.
"""

from __future__ import annotations

from functools import lru_cache

# One turn's ceiling, system prompt included. Set well under every backend's
# real context window: the free chain's per-minute token limits bite long
# before any model's window does.
MAX_CONTEXT_TOKENS = 24_000

# GPT-4o-family BPE. An approximation for the other providers, and closer than
# the ratio below for all of them.
ENCODING = "o200k_base"

# Fallback when tiktoken is unavailable. English prose is ~4 chars/token;
# 3.6 leans pessimistic on purpose, since overestimating trims one message too
# many while underestimating overruns the window.
_CHARS_PER_TOKEN = 3.6

# Per-message framing the providers add around role and content. Small, but it
# is the difference between "just fits" and a 400 on a long thread.
_MSG_OVERHEAD = 4

TRIM_MARK = "\n[…trimmed to fit the context budget]"


@lru_cache(maxsize=1)
def _encoder():
    """The tiktoken encoder, or None to fall back to the ratio.

    Cached because loading reads (and on a cold machine downloads) the BPE
    table — 2s the first time. A miss is not an error: no tiktoken, no cache
    dir and no network all mean "count by characters"."""
    try:
        import tiktoken

        return tiktoken.get_encoding(ENCODING)
    except Exception:
        return None


def count(text: str) -> int:
    """Tokens in `text`, estimated."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return int(len(text) / _CHARS_PER_TOKEN) + 1
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return int(len(text) / _CHARS_PER_TOKEN) + 1


def count_messages(messages: list[dict]) -> int:
    """Tokens in a message list, framing included."""
    return sum(count(m.get("content", "")) + _MSG_OVERHEAD for m in messages)


def truncate(text: str, budget: int) -> str:
    """`text` cut to `budget` tokens from the tail, marked where it was cut.

    Tail-first because that is where augmentation puts its material: the
    user's question is at the top of the message, the page extracts and quotes
    below it. Cutting the other way would drop the question."""
    if budget <= 0:
        return TRIM_MARK.strip()
    if count(text) <= budget:
        return text
    room = max(0, budget - count(TRIM_MARK))
    enc = _encoder()
    if enc is None:
        return text[: int(room * _CHARS_PER_TOKEN)].rstrip() + TRIM_MARK
    try:
        return enc.decode(enc.encode(text, disallowed_special=())[:room]).rstrip() \
            + TRIM_MARK
    except Exception:
        return text[: int(room * _CHARS_PER_TOKEN)].rstrip() + TRIM_MARK


def fit(
    messages: list[dict],
    *,
    system: str = "",
    budget: int = MAX_CONTEXT_TOKENS,
) -> list[dict]:
    """The tail of `messages` that fits alongside `system`, oldest dropped first.

    The system prompt is not trimmable — it is the persona, the portfolio
    snapshot and the skills, and a half-eaten one is worse than a short thread
    — so it comes off the budget before anything else. The last message always
    survives; if it alone overruns what is left, its tail is truncated.

    Returns the same dicts (not copies) when nothing needs trimming, so the
    common case costs one count.
    """
    room = budget - count(system)
    if not messages:
        return messages
    if count_messages(messages) <= room:
        return messages

    kept = list(messages)
    while len(kept) > 1 and count_messages(kept) > room:
        kept = kept[1:]
        # Anthropic requires the first turn to be the user's; dropping a user
        # turn can expose the assistant reply that answered it.
        while len(kept) > 1 and kept[0].get("role") != "user":
            kept = kept[1:]

    if count_messages(kept) > room:
        last = dict(kept[-1])
        last["content"] = truncate(last.get("content", ""), room - _MSG_OVERHEAD)
        kept = kept[:-1] + [last]
    return kept
