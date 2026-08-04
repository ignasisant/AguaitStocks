"""Revolut statement CSV parser tests — pure string parsing, no network."""

from stocks.portfolio import revolut
from stocks.portfolio.revolut import _map_action, _money, _parse_date

# A representative Revolut trading account-statement export.
SAMPLE = (
    "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate\n"
    "2023-01-03T14:30:00.000Z,AAPL,BUY - MARKET,5,$130.15,$650.75,USD,1.05\n"
    "2023-02-01T10:00:00.000Z,TEP.PA,BUY - LIMIT,3,€90.00,€270.00,EUR,\n"
    "2023-06-01T00:00:00.000Z,AAPL,DIVIDEND,,,$4.20,USD,\n"
    "2023-07-01T09:00:00.000Z,AAPL,SELL - MARKET,2,$150.00,$300.00,USD,1.08\n"
    "2023-01-01T00:00:00.000Z,,CASH TOP-UP,,,\"$1,000.00\",USD,\n"
    "2023-08-01T00:00:00.000Z,AAPL,STOCK SPLIT,15,,,USD,\n"
    "2023-09-01T00:00:00.000Z,,CUSTODY FEE,,,$1.00,USD,\n"
)


def test_money_strips_symbols_and_commas():
    assert _money("$1,301.50") == 1301.50
    assert _money("€90.00") == 90.0
    assert _money("-$5.00") == -5.0
    assert _money("") == 0.0
    assert _money(None) == 0.0
    assert _money("1000") == 1000.0


def test_parse_date_from_iso_timestamp():
    assert _parse_date("2023-01-03T14:30:00.000Z") == "2023-01-03"
    assert _parse_date("2023-01-03") == "2023-01-03"


def test_map_action_keywords():
    assert _map_action("BUY - MARKET") == "buy"
    assert _map_action("SELL - LIMIT") == "sell"
    assert _map_action("SELL - STOP") == "sell"
    assert _map_action("DIVIDEND") == "dividend"
    assert _map_action("CASH TOP-UP") is None
    assert _map_action("STOCK SPLIT") is None
    assert _map_action("CUSTODY FEE") is None
    # Withholding adjustments must NOT import as dividends (they cancel in pairs).
    assert _map_action("DIVIDEND TAX (CORRECTION)") is None
    assert _map_action("REWARD") is None
    assert _map_action("RETURN OF CAPITAL") is None


def test_parse_csv_imports_trades_and_dividends():
    res = revolut.parse_csv(SAMPLE)
    actions = [t.action for t in res.transactions]
    assert actions == ["buy", "buy", "dividend", "sell"]

    buy = res.transactions[0]
    assert buy.ticker == "AAPL" and buy.quantity == 5 and buy.price == 130.15
    assert buy.currency == "USD" and buy.note == "revolut"

    eur_buy = res.transactions[1]
    assert eur_buy.ticker == "TEP.PA" and eur_buy.currency == "EUR"


def test_dividend_maps_total_to_price_zero_fee():
    res = revolut.parse_csv(SAMPLE)
    div = next(t for t in res.transactions if t.action == "dividend")
    assert div.price == 4.20  # gross total
    assert div.fee == 0.0


def test_skips_cash_split_and_fee_rows():
    res = revolut.parse_csv(SAMPLE)
    reasons = {r["type"]: r["reason"] for r in res.skipped}
    assert "CASH TOP-UP" in reasons and "not position-affecting" in reasons["CASH TOP-UP"]
    assert "STOCK SPLIT" in reasons and "split" in reasons["STOCK SPLIT"].lower()
    assert "CUSTODY FEE" in reasons


def test_price_derived_from_total_when_blank():
    csv = (
        "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency\n"
        "2023-01-03,AAPL,BUY - MARKET,4,,$400.00,USD\n"
    )
    res = revolut.parse_csv(csv)
    assert res.transactions[0].price == 100.0


def test_empty_csv_is_safe():
    assert revolut.parse_csv("").transactions == []


_HDR = "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate\n"


def test_rejects_negative_purchase_and_bad_price():
    csv = (
        _HDR
        + "2025-01-03T10:00:00Z,AAPL,BUY - MARKET,5,USD 100.00,USD -500,USD,1.04\n"
        + "2025-01-04T10:00:00Z,MSFT,BUY - MARKET,5,USD -10.00,,USD,1.04\n"
        + "2025-01-05T10:00:00Z,NVO,DIVIDEND,,,USD -3.20,USD,1.04\n"
    )
    res = revolut.parse_csv(csv)
    assert res.transactions == []
    reasons = " | ".join(s["reason"] for s in res.skipped)
    assert "negative total" in reasons
    assert "negative price" in reasons
    assert "not positive" in reasons  # dividend


def test_zero_price_sell_imports_as_worthless_disposal():
    # Real GTIJF row: OTC position written off — realizes the full loss.
    csv = _HDR + "2026-05-04T13:30:25Z,GTIJF,SELL - MARKET,36.36363636,USD 0,USD 0,USD,1.17\n"
    res = revolut.parse_csv(csv)
    assert len(res.transactions) == 1
    assert res.transactions[0].price == 0.0


def test_zero_price_buy_still_rejected():
    csv = _HDR + "2026-05-04T13:30:25Z,GTIJF,BUY - MARKET,36.36,USD 0,USD 0,USD,1.17\n"
    res = revolut.parse_csv(csv)
    assert res.transactions == []
    assert "zero price" in res.skipped[0]["reason"]


def test_rejects_inconsistent_qty_price_total():
    csv = _HDR + "2025-01-03T10:00:00Z,AAPL,BUY - MARKET,5,USD 100.00,USD 900,USD,1.04\n"
    res = revolut.parse_csv(csv)
    assert res.transactions == []
    assert "inconsistent" in res.skipped[0]["reason"]


def test_sell_fee_implied_from_total():
    # 20 × 123.96 = 2479.20 gross, 2473.07 received -> 6.13 commission (real row).
    csv = _HDR + "2026-06-10T14:51:03Z,SEZL,SELL - MARKET,20,USD 123.96,USD 2473.07,USD,1.15\n"
    tx = revolut.parse_csv(csv).transactions[0]
    assert tx.fee == 6.13


def test_buy_fee_zero_when_total_matches():
    csv = _HDR + "2024-12-24T15:05:11Z,NVO,BUY - MARKET,34.2896331,USD 87.49,USD 3000,USD,1.04\n"
    tx = revolut.parse_csv(csv).transactions[0]
    assert tx.fee == 0.0


def test_skipped_rows_keep_raw_fields_for_split_resolution():
    csv = _HDR + "2025-03-31T11:45:06Z,SEZL,STOCK SPLIT,31.00046115,,USD 0,USD,1.0853\n"
    skip = revolut.parse_csv(csv).skipped[0]
    assert skip["ticker"] == "SEZL"
    assert skip["quantity"] == 31.00046115
    assert skip["date"].startswith("2025-03-31")


def test_dividend_tax_correction_skipped_not_imported():
    csv = (
        _HDR
        + "2025-07-02T05:55:05Z,GOOG,DIVIDEND TAX (CORRECTION),,,USD -0.75,USD,1.18\n"
        + "2025-07-02T05:55:05Z,GOOG,DIVIDEND TAX (CORRECTION),,,USD 0.75,USD,1.18\n"
    )
    res = revolut.parse_csv(csv)
    assert res.transactions == []
    assert all("tax correction" in s["reason"] for s in res.skipped)
