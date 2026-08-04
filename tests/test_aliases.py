"""Broker-code alias tests — watchlist.yaml `aliases` -> Yahoo symbols."""

import pandas as pd

import stocks.data.fetch as fetch
from stocks.config import ticker_aliases

ALIASES = {"RCF": "TEP.PA", "HMI": "RMS.PA"}


def _patch_aliases(monkeypatch, aliases=ALIASES):
    monkeypatch.setattr(fetch, "ticker_aliases", lambda: dict(aliases))


# ------------------------------------------------------------------ config
def test_ticker_aliases_parses_yaml(tmp_path):
    f = tmp_path / "watchlist.yaml"
    f.write_text("aliases:\n  rcf: TEP.PA\n  HMI: RMS.PA\nwatchlist: []\n")
    assert ticker_aliases(f) == {"RCF": "TEP.PA", "HMI": "RMS.PA"}


def test_ticker_aliases_missing_file_or_section(tmp_path):
    assert ticker_aliases(tmp_path / "nope.yaml") == {}
    f = tmp_path / "watchlist.yaml"
    f.write_text("watchlist: []\n")
    assert ticker_aliases(f) == {}


# ------------------------------------------------------------------ resolve
def test_resolve_maps_broker_code_and_passes_through(monkeypatch):
    _patch_aliases(monkeypatch)
    assert fetch.resolve("RCF") == "TEP.PA"
    assert fetch.resolve("rcf") == "TEP.PA"  # ledger codes are upper, be safe
    assert fetch.resolve("NVDA") == "NVDA"


# ------------------------------------------------------------------ fetch layer
def test_fetch_many_downloads_resolved_but_keys_by_broker_code(monkeypatch):
    _patch_aliases(monkeypatch)
    requested = {}

    def fake_download(symbols, **kwargs):
        requested["symbols"] = list(symbols)
        idx = pd.date_range("2026-01-05", periods=2, name="Date")
        cols = pd.MultiIndex.from_product([symbols, ["Close"]])
        return pd.DataFrame(1.0, index=idx, columns=cols)

    monkeypatch.setattr(fetch.yf, "download", fake_download)
    out = fetch.fetch_many(["RCF", "NVDA"])
    assert requested["symbols"] == ["TEP.PA", "NVDA"]
    assert set(out) == {"RCF", "NVDA"}  # caller-facing keys stay broker codes


def test_latest_price_resolves_alias(monkeypatch):
    _patch_aliases(monkeypatch)
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol
            self.fast_info = {"lastPrice": 123.0}

    monkeypatch.setattr(fetch.yf, "Ticker", FakeTicker)
    assert fetch.latest_price("HMI") == 123.0
    assert seen["symbol"] == "RMS.PA"
