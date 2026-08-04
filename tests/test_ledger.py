"""Ledger + FIFO + dividends tests — pure, no network (FX is stubbed)."""

import pytest

from stocks.portfolio import dividends
from stocks.portfolio.ledger import (
    Transaction,
    add,
    add_many,
    all_transactions,
    delete_many,
    import_csv,
)
from stocks.portfolio.positions import build


def flat_fx(amount: float, currency: str, day: str) -> float:
    """Deterministic stub: USD->EUR at 0.9, EUR unchanged."""
    return amount * (0.9 if currency.upper() == "USD" else 1.0)


# --- ledger ---

def test_ledger_roundtrip(tmp_path):
    db = tmp_path / "p.db"
    add(Transaction("2025-01-02", "aapl", "buy", 10, 100, "USD", 5), path=db)
    add(Transaction("2025-02-01", "AAPL", "sell", 4, 120, "USD"), path=db)
    txs = all_transactions(path=db)
    assert len(txs) == 2
    assert txs[0].ticker == "AAPL"  # normalized upper
    assert txs[0].action == "buy" and txs[0].id == 1


def test_ledger_rejects_bad_action():
    with pytest.raises(ValueError):
        Transaction("2025-01-01", "AAPL", "wibble")


def test_add_many_returns_ids_delete_many_undoes(tmp_path):
    db = tmp_path / "p.db"
    add(Transaction("2025-01-01", "MSFT", "buy", 1, 400, "USD"), path=db)
    ids = add_many(
        [
            Transaction("2025-01-02", "AAPL", "buy", 10, 100, "USD"),
            Transaction("2025-02-01", "AAPL", "sell", 4, 120, "USD"),
        ],
        path=db,
    )
    assert ids == [2, 3]
    assert add_many([], path=db) == []
    # delete only the batch; unrelated row and already-gone ids untouched
    assert delete_many(ids + [999], path=db) == 2
    remaining = all_transactions(path=db)
    assert [t.ticker for t in remaining] == ["MSFT"]
    assert delete_many([], path=db) == 0


def test_import_csv(tmp_path):
    db = tmp_path / "p.db"
    csv = tmp_path / "tx.csv"
    csv.write_text(
        "date,ticker,action,quantity,price,currency,fee,note\n"
        "2025-01-02,AAPL,buy,10,100,USD,5,first\n"
        "2025-03-01,TEP.PA,buy,3,90,EUR,1,paris\n"
    )
    assert import_csv(csv, path=db) == 2
    assert len(all_transactions(path=db)) == 2


# --- FIFO positions ---

def test_fifo_cost_basis_eur():
    txs = [Transaction("2025-01-02", "AAPL", "buy", 10, 100, "USD", 10)]
    positions, realized = build(txs, to_eur=flat_fx)
    assert not realized
    p = positions[0]
    # native cost = 10*100 + 10 fee = 1010; EUR at 0.9 = 909
    assert p.cost_native == pytest.approx(1010)
    assert p.cost_eur == pytest.approx(909)
    assert p.avg_cost_eur == pytest.approx(90.9)


def test_fifo_partial_sell_realized_gain():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 10, 100, "USD", 10),
        Transaction("2025-06-01", "AAPL", "sell", 5, 120, "USD", 0),
    ]
    positions, realized = build(txs, to_eur=flat_fx)
    assert len(realized) == 1
    s = realized[0]
    # cost = 909 * 5/10 = 454.5 ; proceeds = 5 * 120 * 0.9 = 540
    assert s.cost_eur == pytest.approx(454.5)
    assert s.proceeds_eur == pytest.approx(540)
    assert s.gain_eur == pytest.approx(85.5)
    # 5 shares remain, half the basis
    assert positions[0].quantity == pytest.approx(5)
    assert positions[0].cost_eur == pytest.approx(454.5)


def test_fifo_spans_multiple_lots():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 5, 100, "USD", 0),
        Transaction("2025-02-02", "AAPL", "buy", 5, 200, "USD", 0),
        Transaction("2025-06-01", "AAPL", "sell", 8, 300, "USD", 0),
    ]
    positions, realized = build(txs, to_eur=flat_fx)
    # FIFO: 5 @100 then 3 @200 consumed
    assert len(realized) == 2
    assert realized[0].quantity == pytest.approx(5)
    assert realized[1].quantity == pytest.approx(3)
    assert positions[0].quantity == pytest.approx(2)  # 2 left from lot 2


def test_split_scales_quantity_keeps_basis():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 10, 100, "USD", 0),
        Transaction("2025-03-01", "AAPL", "split", 4),  # 4:1
    ]
    positions, _ = build(txs, to_eur=flat_fx)
    assert positions[0].quantity == pytest.approx(40)
    assert positions[0].cost_eur == pytest.approx(900)  # unchanged
    assert positions[0].avg_cost_eur == pytest.approx(22.5)


def test_oversell_raises():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 5, 100, "USD"),
        Transaction("2025-06-01", "AAPL", "sell", 10, 120, "USD"),
    ]
    with pytest.raises(ValueError, match="exceeds held"):
        build(txs, to_eur=flat_fx)


# --- dividends ---

def test_dividends_by_year():
    txs = [
        Transaction("2025-03-10", "AAPL", "dividend", 0, 100, "USD", 15),  # US 15% WHT
        Transaction("2025-09-10", "AAPL", "dividend", 0, 100, "USD", 15),
    ]
    years = dividends.by_year(txs, to_eur=flat_fx)
    d = years[2025]
    assert d.gross_eur == pytest.approx(180)  # 200 * 0.9
    assert d.withheld_eur == pytest.approx(27)  # 30 * 0.9
    assert d.net_eur == pytest.approx(153)
    # withheld exactly at 15% treaty cap -> fully creditable, nothing reclaimable
    assert d.creditable_eur == pytest.approx(27)
    assert d.reclaimable_eur == pytest.approx(0)


def test_dividend_reclaimable_above_treaty_cap():
    # France withholds 25% domestically; treaty cap 15% -> 10% reclaimable
    txs = [Transaction("2025-05-01", "RMS.PA", "dividend", 0, 100, "EUR", 25)]
    d = dividends.by_year(txs, to_eur=flat_fx)[2025]
    assert d.creditable_eur == pytest.approx(15)
    assert d.reclaimable_eur == pytest.approx(10)
