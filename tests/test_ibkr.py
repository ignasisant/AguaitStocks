"""Tests for the IBKR activity-statement CSV parser (strict-shape import)."""

from __future__ import annotations

from stocks.portfolio import ibkr

IBKR_CSV = (
    "Statement,Header,Field Name,Field Value\n"
    "Statement,Data,BrokerName,Interactive Brokers\n"
    "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,"
    "Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,"
    "Realized P/L,MTM P/L,Code\n"
    'Trades,Data,Order,Stocks,USD,AAPL,"2024-01-03, 09:30:00",10,125.00,'
    "125.50,-1250,-1,1251,0,5,O\n"
    'Trades,Data,ClosedLot,Stocks,USD,AAPL,"2024-01-03, 09:30:00",10,120,'
    ",,,,,,\n"
    'Trades,Data,Order,Stocks,USD,AAPL,"2024-03-05, 10:00:00",-4,170.00,'
    "170.10,680,-1.02,-500,178.98,0,C\n"
    "Trades,SubTotal,,Stocks,USD,AAPL,,6,,,-570,-2.02,751,178.98,5,\n"
    'Trades,Data,Order,Forex,USD,EUR.USD,"2024-01-02, 09:00:00",1000,1.0854,'
    ",,-2,,,,\n"
    "Dividends,Header,Currency,Date,Description,Amount\n"
    "Dividends,Data,USD,2024-02-16,AAPL(US0378331005) Cash Dividend USD 0.24"
    " per Share (Ordinary Dividend),2.40\n"
    "Dividends,Data,Total,,,2.40\n"
    "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n"
    "Withholding Tax,Data,USD,2024-02-16,AAPL(US0378331005) Cash Dividend"
    " USD 0.24 per Share - US Tax,-0.36,\n"
)


def test_orders_import_with_commission_as_fee():
    result = ibkr.parse_csv(IBKR_CSV)
    trades = [t for t in result.transactions if t.action in ("buy", "sell")]
    buy, sell = trades
    assert (buy.action, buy.ticker, buy.quantity, buy.price) == ("buy", "AAPL", 10, 125.0)
    assert buy.fee == 1.0 and buy.currency == "USD" and buy.date == "2024-01-03"
    assert (sell.action, sell.quantity, sell.fee) == ("sell", 4, 1.02)


def test_dividend_from_description_and_withholding_listed():
    result = ibkr.parse_csv(IBKR_CSV)
    (div,) = [t for t in result.transactions if t.action == "dividend"]
    assert (div.ticker, div.price, div.currency, div.date) == (
        "AAPL", 2.40, "USD", "2024-02-16",
    )
    wht = [s for s in result.skipped if s["type"] == "withholding tax"]
    assert len(wht) == 1 and wht[0]["ticker"] == "AAPL"
    assert "fee on the matching dividend" in wht[0]["reason"]


def test_forex_skipped_closedlot_and_subtotals_dropped():
    result = ibkr.parse_csv(IBKR_CSV)
    forex = [s for s in result.skipped if "Forex" in s.get("type", "")]
    assert len(forex) == 1 and "not auto-imported" in forex[0]["reason"]
    # 2 stock orders + 1 dividend; ClosedLot/SubTotal/Total left no trace
    assert len(result.transactions) == 3
    assert len(result.skipped) == 2  # forex + withholding


def test_refuses_foreign_shape():
    result = ibkr.parse_csv(
        "date,ticker,action,quantity,price\n2024-01-02,AAPL,buy,10,180\n"
    )
    assert result.transactions == []
    assert "not an IBKR activity statement" in result.skipped[0]["reason"]

    result = ibkr.parse_csv("")
    assert result.transactions == []
    assert "not an IBKR activity statement" in result.skipped[0]["reason"]
