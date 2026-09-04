"""Picking a parser for an uploaded statement with nothing but its filename.

The Import page asks the user which broker it is; chat can't. What matters
here is that a file a dedicated parser owns never reaches the LLM fallback
(and never reaches the wrong parser), and that everything else does reach it.
"""

from __future__ import annotations

import json

from stocks.portfolio import autodetect

LEDGER_CSV = (
    "date,ticker,action,quantity,price,currency,fee,note\n"
    "2025-01-02,AAPL,buy,10,180.5,USD,1.0,first lot\n"
)

T212_CSV = (
    "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
    "Currency (Price / share),Exchange rate,Total,Currency (Total),"
    "Withholding tax,Currency (Withholding tax),Notes,ID\n"
    "Market buy,2024-01-03 14:30:15,US0378331005,AAPL,Apple Inc,"
    "10.0000000,125.00,USD,1.0854,1151.65,EUR,,,,EOF1\n"
)

# Semicolons, Spanish headers, comma decimals: no registered parser owns it.
UNKNOWN_CSV = (
    "Fecha;Valor;Operación;Títulos;Precio\n"
    "02/01/2024;AAPL;Compra;10;180,50\n"
)

MAPPING = {
    "header_row": 0,
    "columns": {"date": 0, "ticker": 1, "action": 2, "quantity": 3, "price": 4},
    "date_format": "%d/%m/%Y", "decimal": ",", "thousands": ".",
    "action_map": {"Compra": "buy"},
}


class _StubProvider:
    classifier_model = "stub-mini"
    default_model = "stub"

    def __init__(self, mapping=MAPPING):
        self.mapping = mapping
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append(messages)
        return json.dumps(self.mapping)


def test_a_known_broker_export_never_reaches_the_model():
    provider = _StubProvider()
    found = autodetect.detect("t212.csv", T212_CSV.encode(), provider)

    assert found.platform == "trading212" and found.label == "Trading 212"
    assert found.recognised and len(found.result.transactions) == 1
    assert provider.calls == []  # no call made, no tokens spent


def test_ledger_format_lands_on_the_generic_parser():
    found = autodetect.detect("ledger.csv", LEDGER_CSV.encode(), _StubProvider())
    assert found.platform == "generic"
    assert found.result.transactions[0].ticker == "AAPL"


def test_an_unrecognised_file_falls_through_to_column_mapping():
    provider = _StubProvider()
    found = autodetect.detect("extracto.csv", UNKNOWN_CSV.encode(), provider)

    assert found.platform == autodetect.LLM_KEY
    assert len(provider.calls) == 1
    tx = found.result.transactions[0]
    assert (tx.date, tx.ticker, tx.action, tx.price) == (
        "2024-01-02", "AAPL", "buy", 180.5)


def test_without_a_provider_an_unknown_file_is_reported_not_guessed():
    found = autodetect.detect("extracto.csv", UNKNOWN_CSV.encode())
    assert not found.recognised
    assert found.result.skipped[0]["reason"] == "no parser recognised this file"


def test_extension_gates_which_parsers_are_tried():
    """A .xlsx must not be handed to the CSV-only parsers as decoded bytes."""
    provider = _StubProvider()
    found = autodetect.detect("book.xlsx", b"PK\x03\x04 not a real book", provider)
    assert found.platform == autodetect.LLM_KEY
    assert not found.recognised  # unreadable, but reported rather than raised


def test_a_parser_that_raises_is_treated_as_declining():
    """One broken parser must not take the whole cascade down with it."""
    from stocks.portfolio import platforms

    class _Boom:
        key, label, file_types = "boom", "Boom", ("csv",)

        @staticmethod
        def parse(filename, data):
            raise RuntimeError("bad export")

    original = platforms.PLATFORMS
    platforms.PLATFORMS = (_Boom, *original)
    try:
        found = autodetect.detect("ledger.csv", LEDGER_CSV.encode())
        assert found.platform == "generic"
    finally:
        platforms.PLATFORMS = original


# The three alias-based parsers all claim each other's files when asked
# directly; detection must not let them.


REVOLUT_STOCK_CSV = (
    "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate\n"
    "2024-12-24T15:05:11Z,NVO,BUY - MARKET,34.2896331,USD 87.49,USD 3000,USD,1.04\n"
)

REVOLUT_CRYPTO_CSV = (
    "Symbol,Type,Quantity,Price,Value,Fees,Date\n"
    'BTC,Buy,"0.05000000","\u20ac60,000.00","\u20ac3,000.00",\u20ac44.85,'
    "2025-03-04T09:12:00.000Z\n"
)


def test_a_revolut_stock_statement_is_not_claimed_by_the_crypto_parser():
    """The crypto parser would import NVO as the pair NVO-USD."""
    found = autodetect.detect("stocks.csv", REVOLUT_STOCK_CSV.encode())
    assert found.platform == "revolut"
    assert found.result.transactions[0].ticker == "NVO"


def test_a_revolut_crypto_statement_is_not_claimed_by_the_stock_parser():
    """The stock parser would import BTC bare, losing the quote currency."""
    found = autodetect.detect("crypto.csv", REVOLUT_CRYPTO_CSV.encode())
    assert found.platform == "revolut_crypto"
    assert found.result.transactions[0].ticker == "BTC-EUR"


def test_a_ledger_csv_is_not_claimed_by_revolut():
    """Revolut's resolver matches on date/ticker/quantity/price alone, and
    would drop the fee and note columns it doesn't know about."""
    found = autodetect.detect("ledger.csv", LEDGER_CSV.encode())
    tx = found.result.transactions[0]
    assert found.platform == "generic"
    assert tx.fee == 1.0 and tx.note == "first lot"


def test_supported_types_covers_every_parser_plus_the_fallback():
    types = autodetect.supported_types()
    assert {"csv", "xlsx", "pdf"} <= set(types)
    assert types == tuple(sorted(types))


def test_loose_parser_list_matches_platforms():
    from stocks.portfolio import platforms

    platform_keys = {p.key for p in platforms.PLATFORMS}
    assert set(autodetect._LOOSE) <= platform_keys


def test_fingerprint_list_matches_loose_parsers():
    from stocks.portfolio import platforms

    platform_keys = {p.key for p in platforms.PLATFORMS}
    assert set(autodetect._FINGERPRINT) <= platform_keys
    assert set(autodetect._FINGERPRINT) <= set(autodetect._LOOSE)
