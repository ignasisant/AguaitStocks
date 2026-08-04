"""Revolut crypto-statement parsing (stocks.portfolio.revolut_crypto)."""

from stocks.portfolio.revolut_crypto import parse_csv

HEADER = "Symbol,Type,Quantity,Price,Value,Fees,Date\n"


def test_buy_normalizes_to_pair_in_statement_currency():
    csv = HEADER + 'BTC,Buy,"0.05000000","€60,000.00","€3,000.00",€44.85,2025-03-04T09:12:00.000Z\n'
    result = parse_csv(csv)
    assert len(result.transactions) == 1
    tx = result.transactions[0]
    assert tx.ticker == "BTC-EUR"  # coin + sniffed fiat -> Yahoo pair
    assert tx.action == "buy"
    assert tx.quantity == 0.05
    assert tx.price == 60_000.0
    assert tx.currency == "EUR"
    assert tx.fee == 44.85
    assert tx.date == "2025-03-04"


def test_usd_statement_and_derived_price():
    # Blank per-coin price: derived from value / quantity.
    csv = HEADER + 'ETH,Sell,"2.00000000",,"$8,000.00",$12.00,2025-06-01T10:00:00.000Z\n'
    result = parse_csv(csv)
    tx = result.transactions[0]
    assert tx.ticker == "ETH-USD"
    assert tx.action == "sell"
    assert tx.price == 4_000.0
    assert tx.currency == "USD"


def test_rewards_transfers_and_exchanges_are_skipped_with_reasons():
    csv = HEADER + (
        "DOT,Staking Reward,0.5,€4.00,€2.00,€0.00,2025-01-10T00:00:00.000Z\n"
        "BTC,Send,0.01,€60000.00,€600.00,€0.00,2025-02-01T00:00:00.000Z\n"
        "ETH,Exchange,1.0,€2000.00,€2000.00,€0.00,2025-02-02T00:00:00.000Z\n"
    )
    result = parse_csv(csv)
    assert not result.transactions
    reasons = {s["ticker"]: s["reason"] for s in result.skipped}
    assert "reward" in reasons["DOT"]
    assert "transfer" in reasons["BTC"]
    assert "exchange" in reasons["ETH"]


def test_inconsistent_row_is_quarantined():
    # 0.05 × 60000 = 3000, but value says 5000 — corrupt, must not import.
    csv = HEADER + "BTC,Buy,0.05,€60000.00,€5000.00,€0.00,2025-03-04T09:12:00.000Z\n"
    result = parse_csv(csv)
    assert not result.transactions
    assert "inconsistent" in result.skipped[0]["reason"]


def test_missing_quantity_or_price_is_skipped():
    csv = HEADER + (
        "BTC,Buy,0,€60000.00,€0.00,€0.00,2025-03-04T09:12:00.000Z\n"
        "ETH,Buy,1.0,,,€0.00,2025-03-05T09:12:00.000Z\n"
    )
    result = parse_csv(csv)
    assert not result.transactions
    assert len(result.skipped) == 2


def test_prose_date_and_explicit_currency_column():
    csv = (
        "Symbol,Type,Quantity,Price,Value,Fees,Currency,Date\n"
        'BTC,Buy,0.10,55000.00,5500.00,10.00,GBP,"Jan 5, 2025, 2:31:41 PM"\n'
    )
    result = parse_csv(csv)
    tx = result.transactions[0]
    assert tx.ticker == "BTC-GBP"
    assert tx.currency == "GBP"
    assert tx.date == "2025-01-05"


def test_unknown_fiat_falls_back_to_usd_pair_but_keeps_currency():
    csv = (
        "Symbol,Type,Quantity,Price,Value,Fees,Currency,Date\n"
        "BTC,Buy,0.10,55000.00,5500.00,0.00,CHF,2025-01-05T00:00:00.000Z\n"
    )
    tx = parse_csv(csv).transactions[0]
    assert tx.ticker == "BTC-USD"  # no reliable Yahoo CHF pair
    assert tx.currency == "CHF"  # cost basis stays in the real currency
