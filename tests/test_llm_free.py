"""Free-chain fallback semantics (web/llm.py) — pure, no network, no secrets."""

import pytest

import stocks.web.llm as llm
from stocks.web.llm import FreeTierExhausted, _FreeBackend


def _ok(bid, chunks):
    def stream(api_key, model, system, messages):
        yield from chunks

    return _FreeBackend(bid, f"key-{bid}", f"model-{bid}", stream)


def _dead(bid, exc=None):
    exc = exc or RuntimeError("429 rate limited")

    def stream(api_key, model, system, messages):
        raise exc
        yield  # unreachable; makes this a generator like the real backends

    return _FreeBackend(bid, f"key-{bid}", f"model-{bid}", stream)


def _dies_midway(bid, exc):
    def stream(api_key, model, system, messages):
        yield "partial "
        raise exc

    return _FreeBackend(bid, f"key-{bid}", f"model-{bid}", stream)


def test_first_healthy_backend_wins(monkeypatch):
    monkeypatch.setattr(
        llm, "_free_backends", lambda: [_ok("groq", ["g1", "g2"]), _ok("cerebras", ["c"])]
    )
    assert list(llm._free_stream("", "auto", "sys", [])) == ["g1", "g2"]


def test_falls_back_when_first_backend_fails(monkeypatch):
    monkeypatch.setattr(
        llm, "_free_backends", lambda: [_dead("groq"), _ok("cerebras", ["a", "b"])]
    )
    assert list(llm._free_stream("", "auto", "sys", [])) == ["a", "b"]


def test_exhausted_when_all_backends_fail(monkeypatch):
    monkeypatch.setattr(llm, "_free_backends", lambda: [_dead("a"), _dead("b")])
    with pytest.raises(FreeTierExhausted):
        list(llm._free_stream("", "auto", "sys", []))


def test_exhausted_when_unconfigured(monkeypatch):
    monkeypatch.setattr(llm, "_free_backends", lambda: [])
    with pytest.raises(FreeTierExhausted):
        list(llm._free_stream("", "auto", "sys", []))


def test_mid_answer_failure_reraises_instead_of_switching(monkeypatch):
    # Once text has streamed to the screen, hopping to another backend would
    # splice two half-answers from different models — re-raise instead.
    boom = RuntimeError("connection reset")
    monkeypatch.setattr(
        llm, "_free_backends", lambda: [_dies_midway("a", boom), _ok("b", ["never"])]
    )
    out = []
    with pytest.raises(RuntimeError, match="connection reset"):
        for chunk in llm._free_stream("", "auto", "sys", []):
            out.append(chunk)
    assert out == ["partial "]


def test_error_mapping():
    assert llm._free_error(FreeTierExhausted()) == "chat.free_exhausted"
    assert llm._free_error(RuntimeError("anything else")) == "chat.api_error"


def test_backends_follow_fixed_order_and_model_override(monkeypatch):
    # Chain order comes from _FREE_BACKEND_DEFAULTS, not secrets order; extra
    # non-backend keys (daily_cap) are ignored; "<id>_model" overrides.
    monkeypatch.setattr(
        llm,
        "_free_secrets",
        lambda: {"cerebras": "csk-x", "groq": "gsk-x",
                 "groq_model": "qwen-32b", "daily_cap": 5},
    )
    got = llm._free_backends()
    assert [b.id for b in got] == ["groq", "cerebras"]
    assert got[0].model == "qwen-32b"
    assert got[1].model == "llama-3.3-70b"


def test_blank_keys_are_skipped(monkeypatch):
    monkeypatch.setattr(llm, "_free_secrets", lambda: {"groq": "  ", "cerebras": ""})
    assert llm._free_backends() == []


def test_free_provider_registration(monkeypatch):
    p = llm.PROVIDERS["free"]
    assert p.needs_key is False
    assert p.default_model == "auto"

    monkeypatch.setattr(llm, "_free_backends", lambda: [])
    assert not p.available()
    assert llm.default_provider_id() == llm.DEFAULT_PROVIDER

    monkeypatch.setattr(llm, "_free_backends", lambda: [_ok("groq", ["x"])])
    assert p.available()
    assert llm.default_provider_id() == "free"
