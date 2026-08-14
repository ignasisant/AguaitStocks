"""Rate-limit backoff tests — data.fetch._retry against Yahoo's 429."""

import pytest
from yfinance.exceptions import YFRateLimitError

import stocks.data.fetch as fetch


def _no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)


def test_retry_returns_first_success(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert fetch._retry(fn) == "ok"
    assert len(calls) == 1


def test_retry_recovers_after_rate_limit(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise YFRateLimitError()
        return "ok"

    assert fetch._retry(fn, attempts=3) == "ok"
    assert len(calls) == 3


def test_retry_reraises_when_exhausted(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fn():
        calls.append(1)
        raise YFRateLimitError()

    with pytest.raises(YFRateLimitError):
        fetch._retry(fn, attempts=3)
    assert len(calls) == 3


def test_retry_backoff_is_exponential(monkeypatch):
    delays = []
    monkeypatch.setattr(fetch.time, "sleep", delays.append)

    def fn():
        raise YFRateLimitError()

    with pytest.raises(YFRateLimitError):
        fetch._retry(fn, attempts=3, base_delay=1.5)
    assert delays == [1.5, 3.0]


def test_other_exceptions_not_retried(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        fetch._retry(fn)
    assert len(calls) == 1
