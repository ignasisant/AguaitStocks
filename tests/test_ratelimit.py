"""The chat burst limiter (stocks.web.ratelimit)."""

from __future__ import annotations

from stocks.web import ratelimit


def _fresh(monkeypatch):
    monkeypatch.setattr(ratelimit, "_events", {})


def test_allows_up_to_the_cap_then_blocks(monkeypatch):
    _fresh(monkeypatch)
    assert all(ratelimit.allow("a", max_events=3, window_s=60) for _ in range(3))
    assert not ratelimit.allow("a", max_events=3, window_s=60)


def test_keys_are_independent(monkeypatch):
    _fresh(monkeypatch)
    assert not all(ratelimit.allow("a", max_events=1, window_s=60) for _ in range(2))
    assert ratelimit.allow("b", max_events=1, window_s=60)


def test_the_window_slides(monkeypatch):
    _fresh(monkeypatch)
    now = {"t": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now["t"])
    assert ratelimit.allow("a", max_events=1, window_s=10)
    assert not ratelimit.allow("a", max_events=1, window_s=10)
    now["t"] += 11
    assert ratelimit.allow("a", max_events=1, window_s=10)


def test_retry_after_counts_down_to_zero(monkeypatch):
    _fresh(monkeypatch)
    now = {"t": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now["t"])
    ratelimit.allow("a", max_events=1, window_s=10)
    assert ratelimit.retry_after("a", window_s=10) == 10
    now["t"] += 4
    assert ratelimit.retry_after("a", window_s=10) == 6
    assert ratelimit.retry_after("nobody", window_s=10) == 0
