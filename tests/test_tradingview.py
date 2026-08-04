"""TradingView consensus tests — no network.

Every test monkeypatches the single network seam (`_analyze`) and the config
lookup (`tv_symbols`), so nothing touches TradingView or the optional
`tradingview-ta` library. A raising `_analyze` also stands in for the library
being absent (the real one raises ImportError, caught the same way).
"""

import pytest

import stocks.data.tradingview as tv
from stocks.data.tradingview import Consensus, Symbol, _parse_spec


def make_analysis(buy, sell, neutral, reco="BUY", ma="BUY", osc="NEUTRAL"):
    class A:
        summary = {"RECOMMENDATION": reco, "BUY": buy, "SELL": sell, "NEUTRAL": neutral}
        moving_averages = {"RECOMMENDATION": ma}
        oscillators = {"RECOMMENDATION": osc}

    return A()


def boom(*_a, **_k):
    raise RuntimeError("no data")


# --- pure helpers -----------------------------------------------------------

def test_parse_spec_variants():
    assert _parse_spec("NASDAQ:NVDA") == Symbol("NVDA", "NASDAQ", "america")
    assert _parse_spec("BME:RCF@spain") == Symbol("RCF", "BME", "spain")
    assert _parse_spec("NVDA") is None  # no exchange -> unusable


def test_score_and_total():
    c = Consensus("X", "1d", "BUY", buy=18, sell=2, neutral=6)
    assert c.total == 26
    assert c.score == pytest.approx(16 / 26)


def test_score_zero_when_no_signals():
    c = Consensus("X", "1d", "NEUTRAL", 0, 0, 0)
    assert c.total == 0
    assert c.score == 0.0


# --- candidates / mapping ---------------------------------------------------

def test_candidates_mapped_single(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {"RCF": "EURONEXT:TEP@france"})
    assert tv.candidates("rcf") == [Symbol("TEP", "EURONEXT", "france")]


def test_candidates_unmapped_probes_us(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})
    cs = tv.candidates("nvda")
    assert [s.exchange for s in cs] == ["NASDAQ", "NYSE", "AMEX"]
    assert all(s.symbol == "NVDA" and s.screener == "america" for s in cs)


# --- consensus --------------------------------------------------------------

def test_consensus_builds_from_first_working_venue(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})

    def fake(sym, interval):
        assert interval == "1d"
        if sym.exchange == "NASDAQ":
            return make_analysis(18, 2, 6, reco="STRONG_BUY", ma="BUY", osc="NEUTRAL")
        raise RuntimeError

    monkeypatch.setattr(tv, "_analyze", fake)
    c = tv.consensus("AAPL")
    assert c.recommendation == "STRONG_BUY"
    assert (c.buy, c.sell, c.neutral) == (18, 2, 6)
    assert c.ma == "BUY" and c.osc == "NEUTRAL"
    assert c.score == pytest.approx(16 / 26)


def test_consensus_none_when_all_venues_fail(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})
    monkeypatch.setattr(tv, "_analyze", boom)  # simulates missing lib / dead symbol
    assert tv.consensus("AAPL") is None


def test_consensus_rejects_unknown_interval():
    with pytest.raises(ValueError):
        tv.consensus("AAPL", interval="7h")


# --- multi-timeframe --------------------------------------------------------

def test_consensus_multi_resolves_venue_once(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})
    calls = []

    def fake(sym, interval):
        calls.append((sym.exchange, interval))
        if sym.exchange == "NASDAQ":  # never the right venue for this name
            raise RuntimeError
        return make_analysis(10, 5, 11)

    monkeypatch.setattr(tv, "_analyze", fake)
    out = tv.consensus_multi("XYZ", intervals=("1h", "1d", "1W"))
    assert set(out) == {"1h", "1d", "1W"}
    # NASDAQ probed only while resolving the first frame; NYSE reused after.
    assert calls == [
        ("NASDAQ", "1h"), ("NYSE", "1h"),
        ("NYSE", "1d"),
        ("NYSE", "1W"),
    ]


def test_consensus_multi_skips_failing_interval(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})

    def fake(sym, interval):
        if interval == "1W":
            raise RuntimeError
        return make_analysis(12, 3, 11)

    monkeypatch.setattr(tv, "_analyze", fake)
    out = tv.consensus_multi("XYZ", intervals=("1d", "1W"))
    assert set(out) == {"1d"}


def test_consensus_multi_empty_when_unresolvable(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})
    monkeypatch.setattr(tv, "_analyze", boom)
    assert tv.consensus_multi("XYZ") == {}


# --- batch ------------------------------------------------------------------

def test_consensus_many_drops_misses(monkeypatch):
    monkeypatch.setattr(tv, "tv_symbols", lambda: {})

    def fake(sym, interval):
        if sym.symbol == "DEAD":
            raise RuntimeError
        return make_analysis(20, 1, 5)

    monkeypatch.setattr(tv, "_analyze", fake)
    out = tv.consensus_many(["AAPL", "DEAD"])
    assert set(out) == {"AAPL"}
    assert out["AAPL"].buy == 20


# --- config loader ----------------------------------------------------------

def test_tv_symbols_parsing(tmp_path):
    from stocks.config import tv_symbols

    p = tmp_path / "w.yaml"
    p.write_text("tv:\n  rcf: EURONEXT:TEP@france\n  NVDA: NASDAQ:NVDA\n")
    assert tv_symbols(p) == {"RCF": "EURONEXT:TEP@france", "NVDA": "NASDAQ:NVDA"}


def test_tv_symbols_missing_key(tmp_path):
    from stocks.config import tv_symbols

    p = tmp_path / "w.yaml"
    p.write_text("watchlist: []\n")
    assert tv_symbols(p) == {}
