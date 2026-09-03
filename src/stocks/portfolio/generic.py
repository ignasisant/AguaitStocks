"""Parse a generic ledger-format CSV into Transactions.

Accepts the same format `stocks tx import` does — a header row of
``date,ticker,action,quantity,price,currency,fee,note`` — so any broker not
covered by a dedicated parser can be imported by reshaping its export once
in a spreadsheet. Column matching is case-insensitive; ``currency`` defaults
to USD, ``fee``/``note`` are optional, extra columns are ignored.

The action is a column here rather than a broker-specific type string, so
every row is handed straight to `Transaction`, which is what rejects an
unknown action. Rows that can't become one (unknown action, non-numeric
quantity or price) are reported in ``skipped`` with the row number and
reason — never silently dropped. Note that numbers are parsed strictly, not
through `statement.money`: this format is written by hand, and "1,5" as a
typo for 1.5 should be flagged rather than read as 15.

Semantic checks (future dates, oversells, unknown tickers) stay in
validate.py, same as the Revolut path.

Nothing here writes to the ledger; the caller previews and then commits.
"""

from __future__ import annotations

from stocks.portfolio import statement
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.statement import CsvFormat, ParseResult, Row

_COLS = {
    "date": ("date",),
    "ticker": ("ticker",),
    "action": ("action",),
    "quantity": ("quantity",),
    "price": ("price",),
    "currency": ("currency",),
    "fee": ("fee",),
    "note": ("note",),
}
_REQUIRED = ("date", "ticker", "action")


def _build_tx(row: Row, action: str) -> Transaction:
    return Transaction(
        date=row.text("date"),
        ticker=row.text("ticker"),
        action=action,
        quantity=float(row.text("quantity") or 0),
        price=float(row.text("price") or 0),
        currency=row.text("currency") or "USD",
        fee=float(row.text("fee") or 0),
        note=row.text("note"),
    )


FORMAT = CsvFormat(
    columns=_COLS,
    type_key="action",
    # Never None: an empty or unknown action is not "skip this kind of row",
    # it's a broken row, and Transaction says so in words worth showing.
    action_of=lambda rtype: rtype,
    skip_reason=lambda rtype: "unrecognised action — not imported",
    build=_build_tx,
    audit=lambda row: {},
    required=_REQUIRED,
    refusal="missing required column(s): ",
    blank_when_empty=tuple(_COLS),
)


def parse_csv(text: str) -> ParseResult:
    """Parse ledger-format CSV text into a ParseResult (no side effects)."""
    return statement.parse_csv(text, FORMAT)
