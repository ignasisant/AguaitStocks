"""Validation-layer tests — pure, no network, no real ledger DB."""

from datetime import date

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult
from stocks.portfolio.validate import resolve_splits, validate

TODAY = date(2026, 8, 2)
KNOWN = {"AAPL", "SEZL", "GOOG"}


def _buy(ticker="AAPL", day="2025-01-03", qty=5.0, price=100.0):
    return Transaction(date=day, ticker=ticker, action="buy", quantity=qty, price=price)


def _sell(ticker="AAPL", day="2025-06-01", qty=2.0, price=150.0):
    return Transaction(date=day, ticker=ticker, action="sell", quantity=qty, price=price)


def _result(txs, skipped=None):
    return ParseResult(transactions=list(txs), skipped=list(skipped or []))


def test_clean_batch_is_importable():
    v = validate(_result([_buy(), _sell()]), [], known=KNOWN, today=TODAY)
    assert len(v.importable) == 2
    assert not v.rejected and not v.flagged


def test_future_and_ancient_dates_rejected():
    v = validate(
        _result([_buy(day="2026-12-31"), _buy(day="2015-01-01", ticker="GOOG")]),
        [],
        known=KNOWN,
        today=TODAY,
    )
    msgs = [i.message for c in v.rejected for i in c.errors]
    assert any("future" in m for m in msgs)
    assert any("predates" in m for m in msgs)
    assert v.importable == []


def test_unknown_ticker_warns_but_imports():
    v = validate(_result([_buy(ticker="DHER")]), [], known=KNOWN, today=TODAY)
    assert len(v.importable) == 1
    assert len(v.flagged) == 1
    assert "DHER" in v.flagged[0].warnings[0].message


def test_lookup_rescues_unknown_ticker():
    v = validate(
        _result([_buy(ticker="RMS.PA")]),
        [],
        known=set(KNOWN),
        lookup=lambda t: True,
        today=TODAY,
    )
    assert not v.flagged and len(v.importable) == 1


def test_malformed_ticker_rejected():
    v = validate(_result([_buy(ticker="TOOLONGSYM")]), [], known=KNOWN, today=TODAY)
    assert len(v.rejected) == 1
    assert "malformed" in v.rejected[0].errors[0].message


def test_oversell_rejected_including_prior_ledger():
    prior = [_buy(qty=3)]
    v = validate(
        _result([_sell(qty=5, day="2025-06-01")]), prior, known=KNOWN, today=TODAY
    )
    assert len(v.rejected) == 1
    assert "exceeds" in v.rejected[0].errors[0].message


def test_sell_covered_by_prior_ledger_passes():
    prior = [_buy(qty=10)]
    v = validate(_result([_sell(qty=5)]), prior, known=KNOWN, today=TODAY)
    assert not v.rejected


def test_duplicate_against_ledger_warns():
    prior = [_buy()]
    v = validate(_result([_buy()]), prior, known=KNOWN, today=TODAY)
    assert len(v.flagged) == 1
    assert "already in ledger" in v.flagged[0].warnings[0].message


def test_split_ratio_derived_from_held_quantity():
    # Real SEZL sequence: hold 6.20009223, split adds 31.00046115 -> exactly 6:1.
    txs = [
        _buy(ticker="SEZL", day="2025-02-24", qty=5.84960108, price=294.55),
        _sell(ticker="SEZL", day="2025-03-07", qty=5.84960108, price=220.99),
        _buy(ticker="SEZL", day="2025-03-28", qty=6.20009223, price=209.67),
    ]
    skipped = [
        {
            "row": 9, "type": "STOCK SPLIT", "reason": "stock split",
            "date": "2025-03-31", "ticker": "SEZL",
            "quantity": 31.00046115, "amount": 0.0, "currency": "USD",
        }
    ]
    result = _result(txs, skipped)
    v = validate(result, [], known=KNOWN, today=TODAY)
    splits = [t for t in v.importable if t.action == "split"]
    assert len(splits) == 1
    assert splits[0].quantity == 6.0
    assert splits[0].date == "2025-03-31"
    assert result.skipped == []  # resolved rows leave the skipped list


def test_underivable_split_stays_skipped():
    skipped = [
        {
            "row": 2, "type": "STOCK SPLIT", "reason": "stock split",
            "date": "2025-03-31", "ticker": "SEZL",
            "quantity": 10.0, "amount": 0.0, "currency": "USD",
        }
    ]
    result = _result([], skipped)  # nothing held -> no ratio
    assert resolve_splits(result, []) == []
    assert "underivable" in result.skipped[0]["reason"]


def test_sell_after_derived_split_not_flagged_as_oversell():
    txs = [
        _buy(ticker="SEZL", day="2025-03-28", qty=6.20009223, price=209.67),
        _sell(ticker="SEZL", day="2025-05-07", qty=20.0, price=100.59),
    ]
    skipped = [
        {
            "row": 5, "type": "STOCK SPLIT", "reason": "stock split",
            "date": "2025-03-31", "ticker": "SEZL",
            "quantity": 31.00046115, "amount": 0.0, "currency": "USD",
        }
    ]
    v = validate(_result(txs, skipped), [], known=KNOWN, today=TODAY)
    assert not v.rejected  # 6.2 held × 6 = 37.2 covers the 20-share sell
