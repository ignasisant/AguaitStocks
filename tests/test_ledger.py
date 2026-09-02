"""Ledger + FIFO + dividends tests — pure, no network (FX is stubbed)."""

import pytest

from stocks.portfolio import dividends, ledger
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
    positions, realized = build(txs, to_base=flat_fx)
    assert not realized
    p = positions[0]
    # native cost = 10*100 + 10 fee = 1010; EUR at 0.9 = 909
    assert p.cost_native == pytest.approx(1010)
    assert p.cost == pytest.approx(909)
    assert p.avg_cost == pytest.approx(90.9)


def test_fifo_partial_sell_realized_gain():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 10, 100, "USD", 10),
        Transaction("2025-06-01", "AAPL", "sell", 5, 120, "USD", 0),
    ]
    positions, realized = build(txs, to_base=flat_fx)
    assert len(realized) == 1
    s = realized[0]
    # cost = 909 * 5/10 = 454.5 ; proceeds = 5 * 120 * 0.9 = 540
    assert s.cost == pytest.approx(454.5)
    assert s.proceeds == pytest.approx(540)
    assert s.gain == pytest.approx(85.5)
    # 5 shares remain, half the basis
    assert positions[0].quantity == pytest.approx(5)
    assert positions[0].cost == pytest.approx(454.5)


def test_fifo_spans_multiple_lots():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 5, 100, "USD", 0),
        Transaction("2025-02-02", "AAPL", "buy", 5, 200, "USD", 0),
        Transaction("2025-06-01", "AAPL", "sell", 8, 300, "USD", 0),
    ]
    positions, realized = build(txs, to_base=flat_fx)
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
    positions, _ = build(txs, to_base=flat_fx)
    assert positions[0].quantity == pytest.approx(40)
    assert positions[0].cost == pytest.approx(900)  # unchanged
    assert positions[0].avg_cost == pytest.approx(22.5)


def test_oversell_raises():
    txs = [
        Transaction("2025-01-02", "AAPL", "buy", 5, 100, "USD"),
        Transaction("2025-06-01", "AAPL", "sell", 10, 120, "USD"),
    ]
    with pytest.raises(ValueError, match="exceeds held"):
        build(txs, to_base=flat_fx)


# --- dividends ---

def test_dividends_by_year():
    txs = [
        Transaction("2025-03-10", "AAPL", "dividend", 0, 100, "USD", 15),  # US 15% WHT
        Transaction("2025-09-10", "AAPL", "dividend", 0, 100, "USD", 15),
    ]
    years = dividends.by_year(txs, to_base=flat_fx)
    d = years[2025]
    assert d.gross == pytest.approx(180)  # 200 * 0.9
    assert d.withheld == pytest.approx(27)  # 30 * 0.9
    assert d.net == pytest.approx(153)
    # withheld exactly at 15% treaty cap -> fully creditable, nothing reclaimable
    assert d.creditable == pytest.approx(27)
    assert d.reclaimable == pytest.approx(0)


def test_dividend_reclaimable_above_treaty_cap():
    # France withholds 25% domestically; treaty cap 15% -> 10% reclaimable
    txs = [Transaction("2025-05-01", "RMS.PA", "dividend", 0, 100, "EUR", 25)]
    d = dividends.by_year(txs, to_base=flat_fx)[2025]
    assert d.creditable == pytest.approx(15)
    assert d.reclaimable == pytest.approx(10)


def test_new_db_is_stamped_with_the_current_schema_version(tmp_path):
    import sqlite3

    db = tmp_path / "p.db"
    with ledger.connect(db) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == ledger.SCHEMA_VERSION
        )
    # A pre-versioning db (same shape, user_version 0) is stamped on connect.
    legacy = tmp_path / "old.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute(ledger.SCHEMA)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    with ledger.connect(legacy) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == ledger.SCHEMA_VERSION
        )


def test_pending_migrations_run_in_order_and_stamp(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "p.db"
    with sqlite3.connect(db) as conn:  # a db of today's shape, version 0
        conn.execute(ledger.SCHEMA)

    monkeypatch.setattr(ledger, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(ledger, "MIGRATIONS", {
        2: ["ALTER TABLE transactions ADD COLUMN venue TEXT NOT NULL DEFAULT ''"],
        3: ["ALTER TABLE transactions ADD COLUMN lot TEXT NOT NULL DEFAULT ''"],
    })
    with ledger.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        assert {"venue", "lot"} <= cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    # Idempotent: a second connect sees the stamp and re-runs nothing.
    with ledger.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_a_failing_migration_rolls_back_its_whole_step(tmp_path, monkeypatch):
    import sqlite3

    import pytest as _pytest

    db = tmp_path / "p.db"
    with sqlite3.connect(db) as conn:
        conn.execute(ledger.SCHEMA)

    monkeypatch.setattr(ledger, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(ledger, "MIGRATIONS", {
        2: [
            "ALTER TABLE transactions ADD COLUMN venue TEXT NOT NULL DEFAULT ''",
            "THIS IS NOT SQL",
        ],
    })
    with _pytest.raises(sqlite3.OperationalError):
        ledger.connect(db).close()
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        assert "venue" not in cols  # the half-applied step rolled back
        # Steps before the failing one keep their stamps (step 1 is empty and
        # committed); the failed step 2 left no trace, so a fixed migration
        # resumes exactly there.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
