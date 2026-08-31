"""Process-wide sliding-window rate limiter for expensive interactions.

The free LLM chain already has a per-account *daily* cap (engine.
spend_free_quota); this is the other half — burst protection. A script
posting a chat turn every 200ms would otherwise fan out into web searches,
routing calls and provider requests (spending real money on BYOK keys and
shared free-tier quota) as fast as the event loop allows.

In-memory on purpose: the app is a single Cloud Run container (the storage
consistency model already assumes that), so a process dict with a lock is
exact, free, and needs no schema. A restart forgets the counters, which for
burst control is fine.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_lock = threading.Lock()
_events: dict[str, deque[float]] = {}

# One chat turn every few seconds sustained, with room for a quick exchange.
CHAT_MAX_TURNS = 20
CHAT_WINDOW_S = 300


def allow(key: str, *, max_events: int = CHAT_MAX_TURNS,
          window_s: float = CHAT_WINDOW_S) -> bool:
    """Record one event for `key`; False when the window is already full.

    `key` scopes the limit — use something account-stable (the user's data
    dir), not the Streamlit session id, so reconnecting doesn't reset it.
    """
    now = time.monotonic()
    with _lock:
        q = _events.setdefault(key, deque())
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= max_events:
            return False
        q.append(now)
        return True


def retry_after(key: str, *, window_s: float = CHAT_WINDOW_S) -> int:
    """Seconds until the oldest event for `key` leaves the window (>= 0)."""
    now = time.monotonic()
    with _lock:
        q = _events.get(key)
        if not q:
            return 0
        return max(0, round(window_s - (now - q[0])))
