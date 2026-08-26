"""Transient-notice windowing + the no-re-raise invariant it exists to enforce."""

import re
from pathlib import Path
from urllib.error import URLError

from yfinance.exceptions import YFRateLimitError

from stocks.web.notices import _NOTICES, _WINDOW_S, _kind, _should_show

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"


def test_kind_maps_both_transient_failures():
    assert _kind(YFRateLimitError()) == "rate_limit"
    assert _kind(URLError("no route")) == "offline"
    assert set(_NOTICES) == {"rate_limit", "offline"}


def test_repeat_inside_window_is_suppressed():
    # Six sections failing on one throttle must yield one toast, not six.
    state: dict[str, float] = {}
    assert _should_show("rate_limit", state, 100.0)
    assert not any(_should_show("rate_limit", state, 100.0 + i) for i in (0.1, 1, 3.9))


def test_window_expiry_and_kinds_are_independent():
    state: dict[str, float] = {}
    assert _should_show("rate_limit", state, 100.0)
    assert _should_show("offline", state, 100.0)  # different kind, own window
    assert _should_show("rate_limit", state, 100.0 + _WINDOW_S)


def test_no_fetch_site_re_raises_to_the_app_level_guard():
    """Fragments rerun without app.py on the stack, so a bare `raise` in a
    transient handler surfaces Streamlit's crash card (the bug this module
    fixes). Every handler must degrade locally instead."""
    pattern = re.compile(
        r"except \((?:YFRateLimitError|URLError), (?:YFRateLimitError|URLError)\)"
        r"[^\n]*:\n\s+raise\b"
    )
    offenders = [
        p.relative_to(WEB).as_posix()
        for p in WEB.rglob("*.py")
        if pattern.search(p.read_text())
    ]
    assert offenders == []
