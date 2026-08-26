"""Transient data-fetch notices, rendered as auto-dismissing toasts.

Yahoo throttles Streamlit Cloud's shared egress IPs and plain urllib fetchers
(FX, logos) die on a dropped network, so a fetch failure is routine rather than
exceptional. The section that failed degrades in place — empty state, dimmed
cells — and calls `data_toast()` to say why. `st.toast` self-dismisses after 4s
("short"), so a transient miss never leaves a permanent banner in the layout;
app.py's CSS parks the toast bottom-left, clear of the chat launcher.

Why every fetch site handles its own failure instead of leaning on the
try/except around `page.run()` in app.py: most fetching sections are
`st.fragment`s. A fragment rerun (switching the ticker chart period, flipping
the financials view) re-executes only the fragment function — app.py is not on
the stack — so an exception raised inside one bypasses that guard entirely and
surfaces as Streamlit's crash card. The app-level guard remains only as a
backstop for the non-fragment page body.
"""

from __future__ import annotations

import time

import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.web.i18n import t as tr

# Message + icon per failure kind. Rate limits clear on their own (fetch._retry
# has already backed off); a URLError means the host is unreachable.
_NOTICES = {
    "rate_limit": ("common.toast_rate_limited", ":material/hourglass_top:"),
    "offline": ("common.toast_offline", ":material/wifi_off:"),
}

# One toast per kind per this window. A single run can have six sections fail on
# the same throttle; unwindowed, each queues an identical toast and the stack
# outlives the 4s dismissal as a wall of duplicates.
_WINDOW_S = 4.0

_STATE_KEY = "_notice_last_shown"


def _kind(exc: BaseException) -> str:
    return "rate_limit" if isinstance(exc, YFRateLimitError) else "offline"


def _should_show(kind: str, state: dict[str, float], now: float) -> bool:
    """Whether `kind` is outside its dedupe window; records the toast if so.

    Pure (state is passed in) so the windowing is unit-testable without a
    script run.
    """
    if now - state.get(kind, -_WINDOW_S) < _WINDOW_S:
        return False
    state[kind] = now
    return True


def data_toast(exc: BaseException) -> None:
    """Toast that a data fetch failed, deduped within `_WINDOW_S`.

    Never call from inside an `st.cache_data` function — `st.toast` is not
    cache-compatible, and a cache hit would skip the notice anyway. Call it from
    the `except` block around the cached call instead.
    """
    kind = _kind(exc)
    state = st.session_state.setdefault(_STATE_KEY, {})
    if _should_show(kind, state, time.monotonic()):
        key, icon = _NOTICES[kind]
        st.toast(tr(key), icon=icon)
