"""Tests for the DEGIRO Transactions.csv parser (strict-shape import)."""

from __future__ import annotations

from stocks.portfolio import degiro

# English export: unnamed currency columns follow each money column.
EN_CSV = (
    "Date,Time,Product,ISIN,Exchange,Venue,Quantity,Price,,Local value,,"
    "Value,,Exchange rate,Transaction costs,,Total,,Order ID\n"
    "03-01-2024,14:30,APPLE INC. - COMMON ST,US0378331005,NDQ,XNAS,"
    "10,125.00,USD,-1250.00,USD,-1151.65,EUR,1.0854,-2.50,EUR,-1154.15,EUR,a1\n"
    "05-03-2024,10:00,APPLE INC. - COMMON ST,US0378331005,NDQ,XNAS,"
    "-4,170.00,USD,680.00,USD,630.21,EUR,1.0790,-2.50,EUR,627.71,EUR,a2\n"
)

# Spanish export: same positions, localised headers and comma decimals.
ES_CSV = (
    "Fecha,Hora,Producto,ISIN,Bolsa de,Centro de ejecución,Número,Precio,,"
    "Valor local,,Valor,,Tipo de cambio,Costes de transacción,,Total,,ID Orden\n"
    "03-01-2024,14:30,APPLE INC. - COMMON ST,US0378331005,NDQ,XNAS,"
    '10,"125,00",USD,"-1250,00",USD,"-1151,65",EUR,"1,0854","-2,50",EUR,'
    '"-1154,15",EUR,a1\n'
)


def test_english_export_buy_and_sell():
    result = degiro.parse_csv(EN_CSV)
    assert result.skipped == []
    buy, sell = result.transactions
    assert (buy.action, buy.ticker, buy.quantity, buy.price) == (
        "buy", "US0378331005", 10, 125.0,
    )
    assert buy.currency == "USD" and buy.date == "2024-01-03"
    assert buy.fee == 0.0  # costs charged in EUR, trade is USD — not mixed
    assert "APPLE" in buy.note
    assert (sell.action, sell.quantity) == ("sell", 4)


def test_spanish_export_with_comma_decimals():
    result = degiro.parse_csv(ES_CSV)
    assert result.skipped == []
    (buy,) = result.transactions
    assert (buy.quantity, buy.price, buy.currency) == (10, 125.0, "USD")
    assert buy.date == "2024-01-03"


def test_fee_kept_when_charged_in_trade_currency():
    text = (
        "Date,Time,Product,ISIN,Exchange,Venue,Quantity,Price,,Local value,,"
        "Value,,Exchange rate,Transaction costs,,Total,,Order ID\n"
        "03-01-2024,14:30,AIRBUS SE,NL0000235190,EPA,XPAR,"
        "5,140.00,EUR,-700.00,EUR,-700.00,EUR,,-2.50,EUR,-702.50,EUR,b1\n"
    )
    (buy,) = degiro.parse_csv(text).transactions
    assert buy.fee == 2.50


def test_inconsistent_row_quarantined():
    text = (
        "Date,Time,Product,ISIN,Exchange,Venue,Quantity,Price,,Local value,,"
        "Value,,Exchange rate,Transaction costs,,Total,,Order ID\n"
        "03-01-2024,14:30,APPLE,US0378331005,NDQ,XNAS,"
        "10,125.00,USD,-500.00,USD,-460.00,EUR,1.0854,,,-462.50,EUR,c1\n"
    )
    result = degiro.parse_csv(text)
    assert result.transactions == []
    assert "inconsistent" in result.skipped[0]["reason"]


def test_refuses_foreign_shape():
    result = degiro.parse_csv(
        "date,ticker,action,quantity,price\n2024-01-02,AAPL,buy,10,180\n"
    )
    assert result.transactions == []
    assert "not a DEGIRO" in result.skipped[0]["reason"]

    # A Ticker column is an explicit tell that this is another broker's file.
    t212ish = (
        "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share\n"
        "Market buy,2024-01-03,US0378331005,AAPL,Apple,10,125.00\n"
    )
    result = degiro.parse_csv(t212ish)
    assert result.transactions == []
    assert "Ticker column" in result.skipped[0]["reason"]


def test_number_locale_heuristics():
    assert degiro._num("1.234,56") == 1234.56
    assert degiro._num("1,234.56") == 1234.56
    assert degiro._num("-2,5") == -2.5
    assert degiro._num("0,123") == 0.123
    assert degiro._num("10") == 10.0
    assert degiro._num("1,234,567") == 1234567.0


def test_empty_input():
    assert degiro.parse_csv("").transactions == []
