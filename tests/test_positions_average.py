"""Average-cost and LIFO matching: the two rules the UK pool made cheap.

Canada's adjusted cost base and France's prix moyen pondéré hold one averaged
parcel per ticker; Italy sells the newest shares first. Each test's point is
that FIFO would give a different — and, in that jurisdiction, wrong — answer.
"""

import pytest

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.positions import build


def flat(amount: float, currency: str, day: str) -> float:
    """No FX: the ledger is already in the reporting currency."""
    return amount


def avg(txs):
    return build(txs, to_base=flat, base="CAD", matching="average")


def lifo(txs):
    return build(txs, to_base=flat, base="EUR", matching="lifo")


def fifo(txs):
    return build(txs, to_base=flat, base="EUR", matching="fifo")


def buy(day, qty, price, fee=0.0, ticker="SHOP.TO"):
    return Transaction(day, ticker, "buy", qty, price, "CAD", fee)


def sell(day, qty, price, fee=0.0, ticker="SHOP.TO"):
    return Transaction(day, ticker, "sell", qty, price, "CAD", fee)


# --- averaged holdings ---

def test_the_holding_averages_its_purchases():
    positions, sales = avg([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ])
    assert len(sales) == 1
    assert sales[0].matched == "average"
    assert sales[0].cost == pytest.approx(200)
    assert sales[0].gain == pytest.approx(300)
    # And what stays behind carries the same average, not the older lot's cost.
    assert (positions[0].quantity, positions[0].cost) == (
        pytest.approx(100), pytest.approx(200)
    )


def test_fifo_and_the_average_disagree_on_the_same_trades():
    """The reason this mode exists: FIFO would sell the cheap lot."""
    txs = [
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ]
    assert fifo(txs)[1][0].cost == pytest.approx(100)  # oldest lot
    assert avg(txs)[1][0].cost == pytest.approx(200)  # averaged


def test_a_purchase_after_a_sale_re_averages_what_is_left():
    _, sales = avg([
        buy("2024-01-10", 100, 1.0),
        sell("2024-03-10", 50, 2.0),  # 50 left at 1.00
        buy("2024-06-10", 100, 4.0),  # 150 at (50 + 400) / 150 = 3.00
        sell("2024-09-10", 150, 5.0),
    ])
    assert sales[0].cost == pytest.approx(50)
    assert sales[1].cost == pytest.approx(450)
    assert sales[1].gain == pytest.approx(300)


def test_selling_out_and_buying_back_starts_a_new_holding():
    """The parcel's date is the new purchase's, not the closed holding's."""
    _, sales = avg([
        buy("2024-01-10", 100, 1.0),
        sell("2024-02-10", 100, 2.0),
        buy("2024-03-10", 100, 10.0),
        sell("2024-04-10", 100, 12.0),
    ])
    assert sales[1].buy_date == "2024-03-10"
    assert sales[1].cost == pytest.approx(1_000)


def test_commissions_land_in_the_average_and_in_the_proceeds():
    _, sales = avg([
        buy("2024-01-10", 100, 1.0, fee=10.0),  # 110 for 100 shares
        sell("2024-06-10", 100, 2.0, fee=5.0),
    ])
    assert sales[0].cost == pytest.approx(110)
    assert sales[0].proceeds == pytest.approx(195)
    assert sales[0].gain == pytest.approx(85)


def test_a_split_multiplies_the_holding_and_keeps_its_cost():
    positions, sales = avg([
        buy("2024-01-10", 100, 10.0),
        Transaction("2024-06-10", "SHOP.TO", "split", 2, 0.0, "CAD", 0.0),
        sell("2024-09-10", 100, 8.0),
    ])
    assert sales[0].cost == pytest.approx(500)  # half of 1,000
    assert positions[0].quantity == pytest.approx(100)
    assert positions[0].cost == pytest.approx(500)


def test_selling_more_than_the_holding_is_refused():
    with pytest.raises(ValueError, match="exceeds held"):
        avg([buy("2024-01-10", 100, 1.0), sell("2024-06-10", 150, 2.0)])


def test_each_ticker_averages_on_its_own():
    _, sales = avg([
        buy("2024-01-10", 100, 1.0, ticker="A"),
        buy("2024-01-10", 100, 9.0, ticker="B"),
        sell("2024-06-10", 100, 5.0, ticker="A"),
        sell("2024-06-10", 100, 5.0, ticker="B"),
    ])
    by_ticker = {s.ticker: s for s in sales}
    assert by_ticker["A"].gain == pytest.approx(400)
    assert by_ticker["B"].gain == pytest.approx(-400)


def test_the_average_is_in_the_reporting_currency_at_each_date():
    """A CAD cost base averages *converted* costs, not native ones."""
    rates = {"2024-01-10": 1.5, "2024-06-10": 2.0, "2025-01-10": 2.5}

    def to_base(amount, currency, day):
        return amount * rates[day]

    _, sales = build(
        [
            Transaction("2024-01-10", "AAPL", "buy", 100, 1.0, "USD", 0.0),
            Transaction("2024-06-10", "AAPL", "buy", 100, 1.0, "USD", 0.0),
            Transaction("2025-01-10", "AAPL", "sell", 100, 1.0, "USD", 0.0),
        ],
        to_base=to_base,
        base="CAD",
        matching="average",
    )
    # (150 + 200) / 200 = 1.75 a share, sold at 2.50.
    assert sales[0].cost == pytest.approx(175)
    assert sales[0].proceeds == pytest.approx(250)


# --- LIFO ---

def test_lifo_sells_the_newest_lot_first():
    positions, sales = lifo([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ])
    assert sales[0].matched == "lifo"
    assert sales[0].buy_date == "2024-06-10"
    assert sales[0].cost == pytest.approx(300)
    assert sales[0].gain == pytest.approx(200)
    # The cheap lot is the one still open — the opposite of FIFO.
    assert positions[0].cost == pytest.approx(100)


def test_lifo_books_less_gain_than_fifo_in_a_rising_market():
    """Why Italy needs its own replay: same trades, smaller taxable gain."""
    txs = [
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 100, 5.0),
    ]
    assert fifo(txs)[1][0].gain == pytest.approx(400)
    assert lifo(txs)[1][0].gain == pytest.approx(200)


def test_lifo_walks_backwards_across_several_lots():
    _, sales = lifo([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        buy("2024-09-10", 50, 4.0),
        sell("2025-01-10", 120, 5.0),
    ])
    assert [(s.buy_date, s.quantity) for s in sales] == [
        ("2024-09-10", 50),
        ("2024-06-10", 70),
    ]
    assert sum(s.cost for s in sales) == pytest.approx(200 + 210)


def test_lifo_leaves_the_partially_sold_lot_open():
    positions, _ = lifo([
        buy("2024-01-10", 100, 1.0),
        buy("2024-06-10", 100, 3.0),
        sell("2025-01-10", 150, 5.0),
    ])
    assert positions[0].quantity == pytest.approx(50)
    assert positions[0].cost == pytest.approx(50)  # the old 1.00 lot


def test_lifo_cannot_reach_a_lot_bought_after_the_sale():
    """"Newest" means newest *so far* — the replay is still chronological."""
    _, sales = lifo([
        buy("2024-01-10", 100, 1.0),
        sell("2024-06-10", 100, 5.0),
        buy("2024-09-10", 100, 9.0),
    ])
    assert sales[0].buy_date == "2024-01-10"
    assert sales[0].cost == pytest.approx(100)


def test_lifo_refuses_to_sell_more_than_is_held():
    with pytest.raises(ValueError, match="exceeds held"):
        lifo([buy("2024-01-10", 100, 1.0), sell("2024-06-10", 150, 2.0)])
