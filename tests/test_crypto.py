"""Crypto pair detection, naming and search (stocks.data.crypto)."""

from stocks.data.crypto import (
    crypto_name,
    is_crypto,
    search_crypto,
    split_pair,
    to_pair,
)


def test_split_pair_matches_fiat_pairs():
    assert split_pair("BTC-USD") == ("BTC", "USD")
    assert split_pair("eth-eur") == ("ETH", "EUR")
    assert split_pair("MATIC-GBP") == ("MATIC", "GBP")


def test_split_pair_rejects_stocks_and_class_shares():
    # Bare codes are never crypto — SOL is a real NYSE ticker.
    assert split_pair("SOL") is None
    assert split_pair("AAPL") is None
    # Class shares and exchange suffixes must not match.
    assert split_pair("BRK-B") is None
    assert split_pair("HEI-A") is None
    assert split_pair("RMS.PA") is None
    # Unknown quote currencies don't count.
    assert split_pair("BTC-JPY") is None


def test_is_crypto():
    assert is_crypto("BTC-USD")
    assert is_crypto("FLOKI-USD")  # outside the curated map still counts
    assert not is_crypto("NVDA")
    assert not is_crypto("BTC")


def test_to_pair_uses_currency_with_usd_fallback():
    assert to_pair("btc", "eur") == "BTC-EUR"
    assert to_pair("ETH") == "ETH-USD"
    assert to_pair("BTC", "CHF") == "BTC-USD"  # no reliable Yahoo CHF pair


def test_crypto_name_from_pair_or_code():
    assert crypto_name("BTC-USD") == "Bitcoin"
    assert crypto_name("ETH") == "Ethereum"
    assert crypto_name("FLOKI-USD") is None
    assert crypto_name("AAPL") is None


def test_search_by_code_and_name():
    hits = search_crypto("btc")
    assert hits[0] == ("BTC-USD", "Bitcoin")
    names = dict(search_crypto("bitcoin"))
    assert names["BTC-USD"] == "Bitcoin"
    assert "BCH-USD" in names  # Bitcoin Cash matches by name
    assert search_crypto("") == []
    assert search_crypto("zzzzz") == []
