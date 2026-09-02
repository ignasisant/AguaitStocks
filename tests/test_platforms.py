"""Tests for the import-platform registry and the generic CSV parser."""

from __future__ import annotations

from stocks.portfolio import generic, platforms
from stocks.portfolio.ledger import Transaction

LEDGER_CSV = (
    "date,ticker,action,quantity,price,currency,fee,note\n"
    "2025-01-02,AAPL,buy,10,180.5,USD,1.0,first lot\n"
    "2025-02-03,aapl,sell,4,190,USD,,\n"
    "2025-03-04,MSFT,dividend,0,12.34,USD,0,\n"
)


def test_generic_parses_ledger_format():
    result = generic.parse_csv(LEDGER_CSV)
    assert len(result.transactions) == 3
    assert result.skipped == []

    buy, sell, div = result.transactions
    assert (buy.ticker, buy.action, buy.quantity, buy.price) == ("AAPL", "buy", 10, 180.5)
    assert buy.fee == 1.0 and buy.note == "first lot"
    assert sell.ticker == "AAPL"  # upcased by Transaction
    assert sell.fee == 0.0  # blank optional fields default
    assert (div.action, div.price) == ("dividend", 12.34)


def test_generic_defaults_currency_and_ignores_extra_columns():
    text = (
        "Date,Ticker,Action,Quantity,Price,Broker\n"  # case-insensitive header
        "2025-01-02,SAP,buy,2,150,DEGIRO\n"
    )
    result = generic.parse_csv(text)
    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.currency == "USD"
    assert tx.note == ""


def test_generic_skips_bad_rows_with_reason():
    text = (
        "date,ticker,action,quantity,price\n"
        "2025-01-02,AAPL,buy,10,180\n"
        "2025-01-03,AAPL,transfer,1,1\n"  # unknown action
        "2025-01-04,AAPL,buy,ten,180\n"  # non-numeric quantity
    )
    result = generic.parse_csv(text)
    assert len(result.transactions) == 1
    assert [s["row"] for s in result.skipped] == [3, 4]
    assert "transfer" in result.skipped[0]["reason"]


def test_generic_missing_required_columns():
    result = generic.parse_csv("ticker,price\nAAPL,180\n")
    assert result.transactions == []
    assert len(result.skipped) == 1
    assert "date" in result.skipped[0]["reason"]
    assert "action" in result.skipped[0]["reason"]


def test_generic_empty_input():
    assert generic.parse_csv("").transactions == []
    result = generic.parse_csv(
        "date,ticker,action\n\n2025-01-02,AAPL,buy\n"
    )  # blank line ignored
    assert len(result.transactions) == 1
    assert result.skipped == []


def test_registry_keys_unique_and_lookup():
    keys = [p.key for p in platforms.PLATFORMS]
    assert len(keys) == len(set(keys))
    assert platforms.by_key("generic").label == "Generic CSV"
    # Unknown/legacy keys (pre-platform records) fall back to Revolut.
    assert platforms.by_key("no-such").key == "revolut"


def test_registry_dispatch_revolut_csv_and_generic():
    revolut_csv = (
        b"Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency\n"
        b"2025-01-02T10:00:00.000Z,AAPL,BUY - MARKET,10,$180.50,\"$1,806.00\",USD\n"
    )
    result = platforms.by_key("revolut").parse("statement.csv", revolut_csv)
    assert len(result.transactions) == 1
    assert result.transactions[0].ticker == "AAPL"

    result = platforms.by_key("generic").parse("book.csv", LEDGER_CSV.encode())
    assert len(result.transactions) == 3


# ------------------------------------------------------------ import origin


def _tx(note: str = "") -> Transaction:
    return Transaction("2025-01-02", "AAPL", "buy", 1, 10.0, note=note)


def test_broker_options_offers_known_brokers_and_other_last():
    options = platforms.broker_options()
    assert options[-1] == platforms.OTHER
    assert set(options[:-1]) == set(platforms.BROKER_NAMES)


def test_broker_key_slugs_a_typed_name_to_one_word():
    # Only the note's first word is read back, so a typed name must not keep
    # its spaces or punctuation.
    assert platforms.broker_key("Renta 4") == "renta_4"
    assert platforms.broker_key("My Bank, S.A.") == "my_bank_s_a"
    assert platforms.broker_key("   ") == platforms.OTHER


def test_detected_broker_reads_what_the_parser_stamped():
    assert platforms.detected_broker([_tx("revolut"), _tx("revolut")]) == "revolut"
    # revolut_crypto stamps "revolut crypto <coin>" — same broker.
    assert platforms.detected_broker([_tx("revolut crypto BTC")]) == "revolut"
    # Mapped/generic rows name no broker, and a mixed batch agrees on none:
    # both are the case where the user has to be asked.
    assert platforms.detected_broker([_tx("first lot")]) == ""
    assert platforms.detected_broker([_tx("revolut"), _tx("ibkr")]) == ""
    assert platforms.detected_broker([]) == ""


def test_stamp_broker_prefixes_the_note_and_keeps_the_rest():
    stamped = platforms.stamp_broker([_tx("Apple Inc"), _tx("")], "Renta 4")
    assert [t.note for t in stamped] == ["renta_4 Apple Inc", "renta_4"]


def test_stamp_broker_replaces_a_broker_word_instead_of_stacking_one():
    once = platforms.stamp_broker([_tx("clicktrade Apple Inc")], "degiro")
    assert once[0].note == "degiro Apple Inc"
    # Idempotent: re-stamping the same origin doesn't grow the note.
    assert platforms.stamp_broker(once, "degiro")[0].note == "degiro Apple Inc"
    # A parser's own stamp survives being re-stamped with the same broker.
    kept = platforms.stamp_broker([_tx("revolut crypto BTC")], "revolut")
    assert kept[0].note == "revolut crypto BTC"


def test_stamp_broker_leaves_the_input_rows_untouched():
    rows = [_tx("Apple Inc")]
    platforms.stamp_broker(rows, "ibkr")
    assert rows[0].note == "Apple Inc"
