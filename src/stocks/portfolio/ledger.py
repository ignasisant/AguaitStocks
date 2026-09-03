"""Transaction ledger backed by SQLite (data/portfolio.db).

One row per event: buy / sell / dividend / fee / split. This table is the
single source of truth; positions, realized gains and tax all derive from it.
Amounts are stored in the transaction's *native* currency — EUR conversion
happens downstream at the transaction date (see stocks.data.fx).
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from stocks import storage
from stocks.config import DATA_DIR

DB_PATH = DATA_DIR / "portfolio.db"

ACTIONS = {"buy", "sell", "dividend", "fee", "split"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT    NOT NULL,          -- ISO YYYY-MM-DD (trade/settlement date)
    ticker   TEXT    NOT NULL,
    action   TEXT    NOT NULL,          -- buy | sell | dividend | fee | split
    quantity REAL    NOT NULL DEFAULT 0,-- shares (split: ratio, e.g. 4 for 4:1)
    price    REAL    NOT NULL DEFAULT 0,-- per-share native ccy (dividend: total)
    currency TEXT    NOT NULL DEFAULT 'USD',
    fee      REAL    NOT NULL DEFAULT 0,-- commission in native ccy
    note     TEXT    NOT NULL DEFAULT ''
);
"""

# Schema versioning via SQLite's own `PRAGMA user_version` (0 on any db that
# predates this mechanism — identical in shape to version 1, so stamping is
# the only "migration" it needs). To change the schema from here on:
#   1. update SCHEMA above (what a brand-new db gets),
#   2. bump SCHEMA_VERSION,
#   3. add MIGRATIONS[new_version] = [SQL...] turning the previous shape into
#      the new one (ALTER TABLE ... ADD COLUMN etc.).
# connect() applies pending migrations in order inside one transaction per
# step, so every reader/writer — web, CLI, cron — upgrades any db it touches
# and none of them ever sees a half-migrated file. Backup snapshots keep the
# pre-migration copies (see stocks.backup).
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, list[str]] = {}


def _migrate(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v >= SCHEMA_VERSION:
        return
    for target in range(v + 1, SCHEMA_VERSION + 1):
        # Explicit BEGIN/COMMIT, not `with conn`: Python's sqlite3 runs DDL
        # (ALTER TABLE — most of what a migration is) in autocommit mode, so
        # the context manager would commit each statement as it goes and a
        # failed step would leave the db half-migrated and unstamped.
        conn.execute("BEGIN")
        try:
            for statement in MIGRATIONS.get(target, []):
                conn.execute(statement)
            # PRAGMA can't be parameterized; target is an int from range().
            conn.execute(f"PRAGMA user_version = {target:d}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@dataclass
class Transaction:
    date: str
    ticker: str
    action: str
    quantity: float = 0.0
    price: float = 0.0
    currency: str = "USD"
    fee: float = 0.0
    note: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()
        self.action = self.action.lower()
        self.currency = self.currency.upper()
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}; expected one of {ACTIONS}")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    fresh = not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
    ).fetchone()
    conn.execute(SCHEMA)
    if fresh:
        # A brand-new db is created in the current shape; the migrations
        # describe how *old* shapes get here and must not replay over it.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
    else:
        _migrate(conn)
    return conn


def _rowid(cur: sqlite3.Cursor) -> int:
    """The id sqlite just assigned. Typed `int | None` on the driver because a
    cursor that never ran an INSERT has none; after one it always does."""
    if cur.lastrowid is None:  # pragma: no cover — an INSERT always sets it
        raise RuntimeError("INSERT reported no rowid")
    return int(cur.lastrowid)


def add(tx: Transaction, path: Path = DB_PATH) -> int:
    with closing(connect(path)) as conn, conn:
        cur = conn.execute(
            "INSERT INTO transactions "
            "(date, ticker, action, quantity, price, currency, fee, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tx.date, tx.ticker, tx.action, tx.quantity, tx.price,
             tx.currency, tx.fee, tx.note),
        )
        rowid = _rowid(cur)
    storage.persist(path)
    return rowid


def add_many(txs: list[Transaction], path: Path = DB_PATH) -> list[int]:
    """Insert many transactions in one commit. Returns the inserted row ids,
    so callers can record the batch (and later undo exactly these rows)."""
    if not txs:
        return []
    ids: list[int] = []
    with closing(connect(path)) as conn, conn:
        for t in txs:
            cur = conn.execute(
                "INSERT INTO transactions "
                "(date, ticker, action, quantity, price, currency, fee, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (t.date, t.ticker, t.action, t.quantity, t.price,
                 t.currency, t.fee, t.note),
            )
            ids.append(_rowid(cur))
    storage.persist(path)
    return ids


def clear(path: Path = DB_PATH) -> None:
    """Delete every transaction (wipe the book — used before a clean re-import)."""
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM transactions")
    storage.persist(path)


def all_transactions(path: Path = DB_PATH) -> list[Transaction]:
    """Every transaction, ordered by date then id (stable FIFO ordering)."""
    with closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY date, id"
        ).fetchall()
    return [_row_to_tx(r) for r in rows]


def delete(tx_id: int, path: Path = DB_PATH) -> None:
    with closing(connect(path)) as conn, conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    storage.persist(path)


def delete_many(tx_ids: list[int], path: Path = DB_PATH) -> int:
    """Delete the given transaction ids in one commit. Returns rows removed
    (ids already gone — e.g. after a wipe — are silently skipped)."""
    if not tx_ids:
        return 0
    with closing(connect(path)) as conn, conn:
        cur = conn.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(tx_ids))})",
            tx_ids,
        )
        removed = cur.rowcount
    storage.persist(path)
    return removed


def import_csv(csv_path: Path, path: Path = DB_PATH) -> int:
    """Bulk-load transactions from a CSV with a header row matching the fields:
    date,ticker,action,quantity,price,currency,fee,note. Returns rows inserted.
    """
    with open(csv_path, newline="") as fh:
        txs = [
            Transaction(
                date=raw["date"].strip(),
                ticker=raw["ticker"],
                action=raw["action"],
                quantity=float(raw.get("quantity") or 0),
                price=float(raw.get("price") or 0),
                currency=(raw.get("currency") or "USD"),
                fee=float(raw.get("fee") or 0),
                note=(raw.get("note") or "").strip(),
            )
            for raw in csv.DictReader(fh)
        ]
    return len(add_many(txs, path))


def _row_to_tx(r: sqlite3.Row) -> Transaction:
    return Transaction(
        id=r["id"],
        date=r["date"],
        ticker=r["ticker"],
        action=r["action"],
        quantity=r["quantity"],
        price=r["price"],
        currency=r["currency"],
        fee=r["fee"],
        note=r["note"],
    )
