"""Provider fallback in the web chat panel (_stream_with_fallback).

The panel used to run exactly one provider per turn, so a saturated BYOK
provider (Gemini's 503s) killed every message even with a healthy free chain
behind it. These tests pin the chain semantics: fall through only while the
bubble is still empty, tag the raising provider on the exception, and skip a
capped free fallback without burying the original failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from stocks.web import chat_core


@dataclass
class _Provider:
    id: str
    label: str
    chunks: tuple = ("ok",)
    fail_before: int | None = None  # raise before yielding chunk N
    default_model: str = "m"
    calls: list = field(default_factory=list)

    def stream(self, api_key, model, system, messages):
        self.calls.append(model)
        for i, c in enumerate(self.chunks):
            if self.fail_before is not None and i == self.fail_before:
                raise RuntimeError(f"{self.id} boom")
            yield c
        if self.fail_before is not None and self.fail_before >= len(self.chunks):
            raise RuntimeError(f"{self.id} boom")


class _Pending:
    resolved = False

    def clear(self):
        self.resolved = True


@pytest.fixture
def panel(monkeypatch):
    """Headless stand-ins for the Streamlit calls the helper makes."""
    captions: list[str] = []
    monkeypatch.setattr(chat_core.st, "write_stream",
                        lambda gen: "".join(gen))
    monkeypatch.setattr(chat_core.st, "caption", captions.append)
    monkeypatch.setattr(chat_core, "_spend_free_quota", lambda: True)
    return captions


def _run(monkeypatch, chosen, fallbacks):
    monkeypatch.setattr(chat_core.engine, "attempts",
                        lambda prefs: [(p, "", "") for p in fallbacks])
    return chat_core._stream_with_fallback(
        _Pending(), chosen, "key", "", "system", [], {})


def test_falls_through_when_chosen_dies_before_first_token(panel, monkeypatch):
    chosen = _Provider("gemini", "Gemini", fail_before=0)
    free = _Provider("free", "TopStocks AI", chunks=("free ", "answer"))
    answer = _run(monkeypatch, chosen, [chosen, free])
    assert answer == "free answer"
    assert chosen.calls and free.calls  # both were tried, in order
    assert panel and "TopStocks AI" in panel[0]  # fallback note shown


def test_mid_answer_failure_propagates_without_fallback(panel, monkeypatch):
    chosen = _Provider("gemini", "Gemini", chunks=("partial",), fail_before=1)
    free = _Provider("free", "TopStocks AI")
    with pytest.raises(RuntimeError) as exc:
        _run(monkeypatch, chosen, [chosen, free])
    assert exc.value.chat_provider is chosen
    assert not free.calls  # text was on screen — no mid-bubble switch


def test_all_candidates_dead_raises_last_failure(panel, monkeypatch):
    chosen = _Provider("gemini", "Gemini", fail_before=0)
    free = _Provider("free", "TopStocks AI", fail_before=0)
    with pytest.raises(RuntimeError, match="free boom") as exc:
        _run(monkeypatch, chosen, [chosen, free])
    assert exc.value.chat_provider is free


def test_capped_free_fallback_reraises_chosen_failure(panel, monkeypatch):
    monkeypatch.setattr(chat_core, "_spend_free_quota", lambda: False)
    chosen = _Provider("gemini", "Gemini", fail_before=0)
    free = _Provider("free", "TopStocks AI")
    with pytest.raises(RuntimeError, match="gemini boom") as exc:
        _run(monkeypatch, chosen, [chosen, free])
    assert exc.value.chat_provider is chosen
    assert not free.calls  # cap spent — candidate skipped, not attempted


def test_chosen_success_needs_no_fallback(panel, monkeypatch):
    chosen = _Provider("gemini", "Gemini", chunks=("hola",))
    free = _Provider("free", "TopStocks AI")
    answer = _run(monkeypatch, chosen, [chosen, free])
    assert answer == "hola"
    assert not free.calls
    assert not panel  # no fallback note on a normal answer
