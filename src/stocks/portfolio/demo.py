"""Demo transactions — a hardcoded book, so an empty account can see the app.

Everything on the Portfolio page derives from the ledger, so an account that
has not imported yet finds positions, P/L, allocation, risk, dividends, fees
and the tax report all blank at once. The Import page answers that with a real
broker statement (`platforms.Platform.sample`), which is the honest path and
the one to take when the reader is ready to import. This module answers the
other half of it: a reader who has not decided yet, and wants to *look* first.

Three rules keep a fabricated book from becoming a liability in an app that
also files tax reports:

* **Every row is marked.** The note's first word is `demo`, which is exactly
  what `fees.broker_of` reads a row's origin from, so demo rows are one SQL
  `LIKE` away everywhere and show up under their own name in the Fees and
  Custody views rather than posing as a broker.
* **The first real import wipes them.** Both commit paths (the Import page and
  the assistant) call `clear()` before writing, so a real book never has
  invented lots mixed into its cost basis. They are also excluded from the
  duplicate/oversell baseline the incoming batch is validated against, since
  they are about to be gone.
* **The prices are the real ones.** Roughly the actual closes on the dates
  below — a demo whose P/L is +900% teaches nothing about the app and
  everything about the fabrication.

The book is deliberately static: seven well-known US names, bought over two
years, with adds, two sells, dividends and a custody fee, so every tab has
something in it. It is not meant to be anyone's portfolio.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from stocks.portfolio.ledger import (
    DB_PATH,
    Transaction,
    add_many,
    connect,
    delete_many,
)

# The note's first word. `fees.broker_of` reads it as the row's origin, so it
# has to be one lowercase word and must not collide with a real broker key.
BROKER = "demo"

TICKERS = ("AAPL", "MSFT", "NVDA", "GOOGL", "META", "TSLA", "NOW")

# date, ticker, action, quantity, price, fee, name
# Prices are approximately the real closes on those dates; a dividend's total
# amount rides in `price`, which is the ledger's own convention.
_ROWS: tuple[tuple[str, str, str, float, float, float, str], ...] = (
    ("2024-03-12", "AAPL",  "buy",      20,  173.20, 0.99, "Apple"),
    ("2024-04-18", "MSFT",  "buy",       8,  404.30, 0.99, "Microsoft"),
    ("2024-05-21", "TSLA",  "buy",      15,  186.60, 0.99, "Tesla"),
    ("2024-06-11", "GOOGL", "buy",      25,  177.80, 0.99, "Alphabet"),
    ("2024-07-09", "NVDA",  "buy",      40,  131.10, 0.99, "NVIDIA"),
    ("2024-08-06", "META",  "buy",       6,  494.30, 0.99, "Meta"),
    ("2024-08-15", "AAPL",  "dividend",  0,    5.00, 0.00, "Apple dividend"),
    ("2024-09-12", "MSFT",  "dividend",  0,    6.64, 0.00, "Microsoft dividend"),
    ("2024-09-16", "META",  "dividend",  0,    3.00, 0.00, "Meta dividend"),
    ("2024-10-15", "NOW",   "buy",       3,  921.50, 0.99, "ServiceNow"),
    ("2025-01-14", "AAPL",  "buy",      10,  233.30, 0.99, "Apple"),
    ("2025-02-13", "AAPL",  "dividend",  0,    7.50, 0.00, "Apple dividend"),
    ("2025-02-24", "TSLA",  "sell",      7,  330.50, 0.99, "Tesla"),
    ("2025-04-08", "NVDA",  "buy",      25,   97.60, 0.99, "NVIDIA"),
    ("2025-04-21", "GOOGL", "sell",     10,  165.00, 0.99, "Alphabet"),
    ("2025-06-27", "NVDA",  "dividend",  0,    0.65, 0.00, "NVIDIA dividend"),
    ("2025-06-30", "NOW",   "fee",       0,    0.00, 2.50, "ServiceNow custody fee"),
    ("2025-08-14", "AAPL",  "dividend",  0,    7.80, 0.00, "Apple dividend"),
    ("2025-09-16", "MSFT",  "buy",       4,  509.20, 0.99, "Microsoft"),
    ("2025-09-16", "META",  "dividend",  0,    3.15, 0.00, "Meta dividend"),
    ("2025-12-11", "MSFT",  "dividend",  0,    9.96, 0.00, "Microsoft dividend"),
    ("2026-02-12", "AAPL",  "dividend",  0,    8.10, 0.00, "Apple dividend"),
    ("2026-03-12", "MSFT",  "dividend",  0,   10.16, 0.00, "Microsoft dividend"),
)


def transactions() -> list[Transaction]:
    """The demo book, built fresh (rows are mutable dataclasses)."""
    return [
        Transaction(
            date=date,
            ticker=ticker,
            action=action,
            quantity=quantity,
            price=price,
            currency="USD",
            fee=fee,
            note=f"{BROKER} {name}",
        )
        for date, ticker, action, quantity, price, fee, name in _ROWS
    ]


def is_demo(tx: Transaction) -> bool:
    """Whether a ledger row came from here — same reading as `broker_of`."""
    words = tx.note.split()
    return bool(words) and words[0].lower() == BROKER


def without(txs: list[Transaction]) -> list[Transaction]:
    """`txs` minus the demo rows — the baseline an incoming import is checked
    against, since committing it deletes them."""
    return [t for t in txs if not is_demo(t)]


def _demo_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM transactions "
        "WHERE lower(note) = ? OR lower(note) LIKE ?",
        (BROKER, f"{BROKER} %"),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def active(path: Path = DB_PATH) -> bool:
    """Whether this ledger currently holds demo rows."""
    with closing(connect(path)) as conn:
        return conn.execute(
            "SELECT 1 FROM transactions "
            "WHERE lower(note) = ? OR lower(note) LIKE ? LIMIT 1",
            (BROKER, f"{BROKER} %"),
        ).fetchone() is not None


def seed(path: Path = DB_PATH) -> list[int]:
    """Write the demo book. Returns the inserted ids.

    A ledger that already holds anything is left alone: the offer is only ever
    made on the empty path, and a double click on it (or a stale rerun) must
    not stack a second copy on top of the first.
    """
    with closing(connect(path)) as conn:
        if conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
            return []
    return add_many(transactions(), path)


def clear(path: Path = DB_PATH) -> int:
    """Delete every demo row. Returns how many went."""
    with closing(connect(path)) as conn:
        ids = _demo_ids(conn)
    return delete_many(ids, path)
