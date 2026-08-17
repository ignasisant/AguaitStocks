"""SEC ticker-map search tests — fixture map on disk, no network."""

import json

import pytest

from stocks.data import edgar

# File order mirrors the real map: roughly descending market cap.
FIXTURE = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "2": {"cik_str": 1730168, "ticker": "AVGO", "title": "Broadcom Inc."},
    "3": {"cik_str": 2488, "ticker": "AMD", "title": "ADVANCED MICRO DEVICES INC"},
    "4": {"cik_str": 1559720, "ticker": "ABNB", "title": "Airbnb, Inc."},
    "5": {"cik_str": 70858, "ticker": "BAC", "title": "BANK OF AMERICA CORP /DE/"},
    "6": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "7": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "8": {"cik_str": 70858, "ticker": "BAC-PB", "title": "BANK OF AMERICA CORP /DE/"},
    "9": {"cik_str": 70858, "ticker": "BML-PG", "title": "BANK OF AMERICA CORP /DE/"},
}


@pytest.fixture(autouse=True)
def fake_map(tmp_path, monkeypatch):
    cache = tmp_path / "edgar_tickers.json"
    cache.write_text(json.dumps(FIXTURE))
    monkeypatch.setattr(edgar, "TICKER_CACHE", cache)
    # Reset the process-level memo so each test loads the fixture.
    monkeypatch.setattr(edgar, "_CIK_MAP", None)
    monkeypatch.setattr(edgar, "_TITLE_MAP", None)
    monkeypatch.setattr(edgar, "_ROWS", None)


def test_search_by_company_name():
    assert edgar.search_companies("nvidia") == [("NVDA", "Nvidia Corp")]


def test_search_by_exact_ticker_ranks_first():
    assert edgar.search_companies("AMD")[0] == ("AMD", "Advanced Micro Devices Inc")


def test_ticker_prefix_beats_name_and_substring_matches():
    tickers = [t for t, _ in edgar.search_companies("a")]
    # Rank 1 ticker prefixes in file order, then name word-prefixes
    # (BAC via "AMERICA", Alphabet), then NVDA (substring-only).
    assert tickers == ["AAPL", "AVGO", "AMD", "ABNB", "BAC", "GOOGL", "GOOG", "NVDA"]


def test_multi_word_name_query_dedupes_share_classes():
    # BAC-PB / BML-PG carry the same CIK and title; only the plain listing shows.
    assert edgar.search_companies("bank of america") == [
        ("BAC", "Bank Of America Corp /De/")
    ]


def test_plain_dual_listings_both_survive():
    assert [t for t, _ in edgar.search_companies("alphabet")] == ["GOOGL", "GOOG"]


def test_exact_hyphenated_ticker_still_reachable():
    assert edgar.search_companies("BAC-PB")[0][0] == "BAC-PB"


def test_mixed_case_titles_kept_verbatim():
    assert edgar.search_companies("airbnb") == [("ABNB", "Airbnb, Inc.")]


def test_limit_and_empty_query():
    assert len(edgar.search_companies("a", limit=2)) == 2
    assert edgar.search_companies("") == []
    assert edgar.search_companies("   ") == []


def test_no_match():
    assert edgar.search_companies("zzzz") == []


def test_existing_lookups_still_work():
    assert edgar.cik_for("AAPL") == "0000320193"
    assert edgar.title_for("NVDA") == "Nvidia Corp"
    assert edgar.title_for("AAPL") == "Apple Inc."


def test_fuzzy_typo_falls_back():
    # No exact tier matches "nvidai"; fuzzy catches the transposition.
    assert edgar.search_companies("nvidai")[0] == ("NVDA", "Nvidia Corp")


def test_fuzzy_multiword_typo():
    assert edgar.search_companies("bank of amrica")[0][0] == "BAC"


def test_fuzzy_dedupes_share_classes():
    tickers = [t for t, _ in edgar.search_companies("bank of amrica")]
    assert "BAC-PB" not in tickers and "BML-PG" not in tickers


def test_fuzzy_skipped_when_exact_matches():
    # "a" matches plenty exactly; fuzzy must not run or reorder anything.
    tickers = [t for t, _ in edgar.search_companies("a")]
    assert tickers == ["AAPL", "AVGO", "AMD", "ABNB", "BAC", "GOOGL", "GOOG", "NVDA"]


def test_fuzzy_needs_min_query_length():
    assert edgar.search_companies("zz") == []
