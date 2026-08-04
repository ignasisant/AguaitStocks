"""Related-tickers parsing — pure, no network."""

import pytest

import stocks.data.related as related
from stocks.data.related import parse_related, related_tickers

PAYLOAD = {
    "finance": {
        "result": [
            {
                "symbol": "NVDA",
                "recommendedSymbols": [
                    {"symbol": "AMD", "score": 0.13},
                    {"symbol": "AVGO", "score": 0.12},
                    {"symbol": "TSM", "score": 0.11},
                ],
            }
        ],
        "error": None,
    }
}


def test_parse_related():
    assert parse_related(PAYLOAD) == ["AMD", "AVGO", "TSM"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"finance": {}},
        {"finance": {"result": None}},
        {"finance": {"result": []}},
        {"finance": {"result": [{"symbol": "NVDA"}]}},
        {"finance": {"result": [{"recommendedSymbols": None}]}},
    ],
)
def test_parse_related_malformed(payload):
    assert parse_related(payload) == []


def test_related_tickers_drops_self(monkeypatch):
    self_ref = {
        "finance": {
            "result": [
                {
                    "symbol": "NVDA",
                    "recommendedSymbols": [
                        {"symbol": "NVDA"},
                        {"symbol": "AMD"},
                    ],
                }
            ]
        }
    }
    monkeypatch.setattr(related, "get_json", lambda url: self_ref)
    assert related_tickers("NVDA") == ["AMD"]


def test_related_tickers_network_failure(monkeypatch):
    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(related, "get_json", boom)
    assert related_tickers("NVDA") == []
