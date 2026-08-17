"""Tests for the ClickTrade / Saxo "Trades executed" parser (strict-shape import)."""

from __future__ import annotations

import io
from datetime import datetime

import openpyxl

from stocks.portfolio import clicktrade

# Spanish CSV export: semicolon-delimited, comma decimals, C/V column.
ES_CSV = (
    "Instrumento;Símbolo del instrumento;ISIN del instrumento;"
    "Tipo de instrumento;Divisa del instrumento;Fecha de la operación;C/V;"
    "Cantidad;Precio;Valor de la operación;Importe registrado\n"
    "Telefónica SA;TEF:xmce;ES0178430E18;Acciones;EUR;03-01-2024;Compra;"
    "100;3,95;395,00;-398,50\n"
    "Telefónica SA;TEF:xmce;ES0178430E18;Acciones;EUR;05-03-2024;Venta;"
    "40;4,20;168,00;166,25\n"
)

# English CSV with an FX row that must be skipped, not imported.
EN_CSV = (
    "Instrument,Instrument Symbol,Instrument ISIN,Instrument Type,"
    "Instrument currency,Trade Time,B/S,Amount,Price,Traded Value\n"
    "Apple Inc.,AAPL:xnas,US0378331005,Stock,USD,2024-01-03T14:30:00,Bought,"
    "10,125.00,1250.00\n"
    "EURUSD,EURUSD,,FxSpot,USD,2024-01-04T09:00:00,Sold,1000,1.09,1090.00\n"
)


def _xlsx(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_spanish_csv_buy_and_sell():
    result = clicktrade.parse("ops.csv", ES_CSV.encode())
    assert result.skipped == []
    buy, sell = result.transactions
    assert (buy.action, buy.ticker, buy.quantity, buy.price) == (
        "buy", "TEF.MC", 100, 3.95,
    )
    assert buy.currency == "EUR" and buy.date == "2024-01-03"
    assert buy.fee == 3.50  # derived: |booked| − qty × price
    assert "TELEF" in buy.note.upper()
    assert (sell.action, sell.quantity, sell.fee) == ("sell", 40, 1.75)


def test_english_csv_skips_fx_rows():
    result = clicktrade.parse("trades.csv", EN_CSV.encode())
    (buy,) = result.transactions
    assert (buy.ticker, buy.action, buy.currency) == ("AAPL", "buy", "USD")
    assert buy.date == "2024-01-03"
    (skipped,) = result.skipped
    assert skipped["type"] == "FxSpot"


def test_xlsx_with_preamble_and_native_cells():
    data = _xlsx([
        ["Operaciones ejecutadas"],
        ["Cuenta: 12345 — 01-01-2024 a 31-12-2024"],
        [],
        ["Instrumento", "Símbolo del instrumento", "ISIN del instrumento",
         "Divisa del instrumento", "Fecha de la operación", "C/V",
         "Cantidad", "Precio", "Valor de la operación"],
        ["Apple Inc.", "AAPL:xnas", "US0378331005", "USD",
         datetime(2024, 1, 3, 14, 30), "Compra", 10, 125.0, 1250.0],
    ])
    result = clicktrade.parse("ops.xlsx", data)
    assert result.skipped == []
    (buy,) = result.transactions
    assert (buy.ticker, buy.quantity, buy.price, buy.date) == (
        "AAPL", 10, 125.0, "2024-01-03",
    )


def test_unknown_exchange_falls_back_to_isin():
    text = EN_CSV.replace("AAPL:xnas", "ABC:xwar")
    (buy,) = clicktrade.parse("t.csv", text.encode()).transactions
    assert buy.ticker == "US0378331005"


def test_inconsistent_row_quarantined():
    text = EN_CSV.replace("10,125.00,1250.00", "10,12.50,1250.00")
    result = clicktrade.parse("t.csv", text.encode())
    assert all(t.ticker != "AAPL" for t in result.transactions)
    assert any("inconsistent" in s["reason"] for s in result.skipped)


def test_foreign_statement_refused_whole():
    t212 = "Action,Time,ISIN,Ticker,No. of shares,Price / share\nbuy,x,y,z,1,2\n"
    result = clicktrade.parse("t.csv", t212.encode())
    assert result.transactions == []
    assert "not a ClickTrade/Saxo trades report" in result.skipped[0]["reason"]
