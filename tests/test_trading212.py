"""Tests for the Trading 212 CSV parser (strict-shape import)."""

from __future__ import annotations

from stocks.portfolio import trading212

HEADER = (
    "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
    "Currency (Price / share),Exchange rate,Total,Currency (Total),"
    "Withholding tax,Currency (Withholding tax),Notes,ID\n"
)

T212_CSV = HEADER + (
    "Market buy,2024-01-03 14:30:15,US0378331005,AAPL,Apple Inc,"
    "10.0000000,125.00,USD,1.0854,1151.65,EUR,,,,EOF1\n"
    "Limit sell,2024-03-05 10:00:00,US0378331005,AAPL,Apple Inc,"
    "4.0000000,170.00,USD,1.0790,630.21,EUR,,,,EOF2\n"
    "Dividend (Dividends paid by us corporations),2024-02-16 12:00:00,"
    "US0378331005,AAPL,Apple Inc,10.0000000,0.24,USD,,2.04,EUR,0.36,USD,,EOF3\n"
    "Deposit,2024-01-02 09:00:00,,,,,,,,1000.00,EUR,,,Bank transfer,EOF4\n"
    "Interest on cash,2024-04-01 00:00:00,,,,,,,,1.23,EUR,,,,EOF5\n"
)


def test_buy_sell_in_instrument_currency():
    result = trading212.parse_csv(T212_CSV)
    buy, sell, div = result.transactions
    assert (buy.action, buy.ticker, buy.quantity, buy.price) == ("buy", "AAPL", 10, 125.0)
    assert buy.currency == "USD"  # instrument ccy, not the EUR account total
    assert buy.fee == 0.0 and buy.date == "2024-01-03"
    assert (sell.action, sell.quantity, sell.price) == ("sell", 4, 170.0)


def test_dividend_gross_with_withholding_as_fee():
    div = trading212.parse_csv(T212_CSV).transactions[2]
    assert div.action == "dividend"
    assert div.price == 2.40  # 10 × 0.24 gross, in USD
    assert div.currency == "USD"
    assert div.fee == 0.36  # withholding, same currency


def test_cash_rows_skipped_with_reason():
    skipped = trading212.parse_csv(T212_CSV).skipped
    assert [s["type"] for s in skipped] == ["Deposit", "Interest on cash"]
    assert "cash movement" in skipped[0]["reason"]
    assert "interest" in skipped[1]["reason"]


def test_refuses_foreign_shape():
    # Generic ledger CSV: has action but none of the T212 columns.
    result = trading212.parse_csv(
        "date,ticker,action,quantity,price\n2024-01-02,AAPL,buy,10,180\n"
    )
    assert result.transactions == []
    assert len(result.skipped) == 1
    assert "not a Trading 212 export" in result.skipped[0]["reason"]
    assert "no. of shares" in result.skipped[0]["reason"]


def test_refuses_revolut_shape():
    revolut_csv = (
        "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency\n"
        "2024-01-02T10:00:00.000Z,AAPL,BUY - MARKET,10,$180.50,\"$1,806.00\",USD\n"
    )
    result = trading212.parse_csv(revolut_csv)
    assert result.transactions == []
    assert "not a Trading 212 export" in result.skipped[0]["reason"]


def test_corrupt_row_skipped_not_imported():
    text = HEADER + (
        "Market buy,2024-01-03 14:30:15,US0378331005,AAPL,Apple Inc,"
        "0,125.00,USD,1.0854,0,EUR,,,,EOF1\n"  # zero shares
    )
    result = trading212.parse_csv(text)
    assert result.transactions == []
    assert "no quantity" in result.skipped[0]["reason"]


def test_empty_input():
    assert trading212.parse_csv("").transactions == []
