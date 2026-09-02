"""UK share identification: same-day, the 30-day rule, and the s.104 pool.

Worked examples follow HMRC's own shape (CG51560 onwards): the point of each
is that FIFO would give a different, wrong answer.
"""

import pytest

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.positions import build


def flat(amount: float, currency: str, day: str) -> float:
    """No FX: the ledger is already in the reporting currency."""
    return amount


def s104(txs):
    return build(txs, to_base=flat, base="GBP", matching="s104")


def fifo(txs):
    return build(txs, to_base=flat, base="GBP", matching="fifo")


def buy(day, qty, price, fee=0.0, ticker="LLOY.L"):
    return Transaction(day, ticker, "buy", qty, price, "GBP", fee)


def sell(day, qty, price, fee=0.0, ticker="LLOY.L"):
    return Transaction(day, ticker, "sell", qty, price, "GBP", fee)


# --- the pool ---

def test_the_pool_averages_its_acquisitions():
    # 100 @ £1 and 100 @ £3 pool to 200 @ £2; selling 100 costs £200.
    positions, sales = s104([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ])
    assert len(sales) == 1
    assert sales[0].matched == "pool"
    assert sales[0].cost == pytest.approx(200)
    assert sales[0].gain == pytest.approx(300)
    # And the remaining 100 shares keep the same average.
    assert positions[0].quantity == pytest.approx(100)
    assert positions[0].cost == pytest.approx(200)


def test_fifo_and_the_pool_disagree_on_the_same_trades():
    """The reason this mode exists: FIFO would sell the £1 lot."""
    txs = [
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ]
    assert fifo(txs)[1][0].cost == pytest.approx(100)  # oldest lot
    assert s104(txs)[1][0].cost == pytest.approx(200)  # averaged


def test_buy_commissions_are_in_the_pooled_cost():
    _, sales = s104([
        buy("2024-01-10", 100, 1.0, fee=10.0),
        sell("2025-01-10", 100, 2.0, fee=5.0),
    ])
    assert sales[0].cost == pytest.approx(110)
    assert sales[0].proceeds == pytest.approx(195)  # net of the sell fee


# --- same-day rule ---

def test_a_same_day_buy_is_matched_before_the_pool():
    _, sales = s104([
        buy("2024-01-10", 100, 1.0),      # pool at £1
        buy("2025-03-01", 100, 4.0),      # same day as the sale
        sell("2025-03-01", 100, 5.0),
    ])
    assert [s.matched for s in sales] == ["same_day"]
    assert sales[0].cost == pytest.approx(400)  # not the £100 pool cost
    assert sales[0].gain == pytest.approx(100)


def test_a_same_day_buy_covers_only_its_own_size():
    _, sales = s104([
        buy("2024-01-10", 100, 1.0),
        buy("2025-03-01", 40, 4.0),
        sell("2025-03-01", 100, 5.0),
    ])
    assert [s.matched for s in sales] == ["same_day", "pool"]
    assert sales[0].quantity == pytest.approx(40)
    assert sales[1].quantity == pytest.approx(60)
    assert sales[1].cost == pytest.approx(60)  # 60 × the £1 pool average


# --- 30-day (bed and breakfast) rule ---

def test_a_repurchase_inside_30_days_is_matched_to_the_disposal():
    """Sell at a loss, buy back a week later: the loss is not banked."""
    _, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-03-08", 100, 4.5),
    ])
    assert [s.matched for s in sales] == ["thirty_day"]
    # Matched against the £4.50 repurchase, so the "loss" is £50, not £600.
    assert sales[0].cost == pytest.approx(450)
    assert sales[0].gain == pytest.approx(-50)


def test_a_repurchase_after_30_days_leaves_the_pool_match_alone():
    _, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-04-15", 100, 4.5),
    ])
    assert [s.matched for s in sales] == ["pool"]
    assert sales[0].gain == pytest.approx(-600)


def test_the_thirty_day_window_is_inclusive_of_day_30():
    _, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-03-31", 100, 4.5),  # exactly 30 days later
    ])
    assert [s.matched for s in sales] == ["thirty_day"]


def test_day_31_is_outside_it():
    _, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-04-01", 100, 4.5),
    ])
    assert [s.matched for s in sales] == ["pool"]


def test_shares_claimed_by_the_30_day_rule_never_reach_the_pool():
    positions, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-03-08", 100, 4.5),
    ])
    # The repurchase was consumed by the disposal, so what is left is the old
    # holding: 100 shares at £10, not a £4.50 pool.
    assert positions[0].quantity == pytest.approx(100)
    assert positions[0].cost == pytest.approx(1_000)


def test_a_partial_repurchase_splits_across_the_rules():
    _, sales = s104([
        buy("2023-01-10", 100, 10.0),
        sell("2025-03-01", 100, 4.0),
        buy("2025-03-08", 30, 4.5),
    ])
    assert [s.matched for s in sales] == ["thirty_day", "pool"]
    assert sales[0].quantity == pytest.approx(30)
    assert sales[1].quantity == pytest.approx(70)
    assert sales[1].cost == pytest.approx(700)


def test_the_pool_carries_the_earliest_acquisition_date():
    _, sales = s104([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 150, 5.0),
    ])
    assert sales[0].buy_date == "2024-01-10"
    assert sales[0].matched == "pool"


# --- shared behaviour with the FIFO path ---

def test_overselling_still_raises():
    with pytest.raises(ValueError, match="exceeds held"):
        s104([buy("2024-01-10", 10, 1.0), sell("2024-02-01", 20, 2.0)])


def test_a_split_scales_the_pool_not_its_cost():
    positions, _ = s104([
        buy("2024-01-10", 100, 10.0),
        Transaction("2024-06-01", "LLOY.L", "split", 4, 0.0, "GBP", 0.0),
    ])
    assert positions[0].quantity == pytest.approx(400)
    assert positions[0].cost == pytest.approx(1_000)


def test_two_tickers_keep_separate_pools():
    _, sales = s104([
        buy("2024-01-10", 100, 1.0, ticker="AAA.L"),
        buy("2024-01-10", 100, 9.0, ticker="BBB.L"),
        sell("2025-01-10", 100, 5.0, ticker="AAA.L"),
    ])
    assert len(sales) == 1
    assert sales[0].ticker == "AAA.L"
    assert sales[0].cost == pytest.approx(100)


def test_fifo_sales_are_labelled_as_such():
    _, sales = fifo([buy("2024-01-10", 10, 1.0), sell("2024-02-01", 10, 2.0)])
    assert sales[0].matched == "fifo"
