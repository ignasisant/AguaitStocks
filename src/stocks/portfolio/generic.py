"""Parse a generic ledger-format CSV into Transactions.

Accepts the same format `stocks tx import` does — a header row of
``date,ticker,action,quantity,price,currency,fee,note`` — so any broker not
covered by a dedicated parser can be imported by reshaping its export once
in a spreadsheet. Column matching is case-insensitive; ``currency`` defaults
to USD, ``fee``/``note`` are optional, extra columns are ignored.

Rows that can't become a Transaction (unknown action, non-numeric quantity or
price) are reported in ``skipped`` with the row number and reason — never
silently dropped. Semantic checks (future dates, oversells, unknown tickers)
stay in validate.py, same as the Revolut path.

Nothing here writes to the ledger; the caller previews and then commits.
"""

from __future__ import annotations

import csv
import io

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult

_REQUIRED = ("date", "ticker", "action")
_OPTIONAL = ("quantity", "price", "currency", "fee", "note")


def parse_csv(text: str) -> ParseResult:
    """Parse ledger-format CSV text into a ParseResult (no side effects)."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return ParseResult()

    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    missing = [c for c in _REQUIRED if c not in fields]
    if missing:
        return ParseResult(
            skipped=[{
                "row": 1,
                "type": "header",
                "reason": f"missing required column(s): {', '.join(missing)}",
            }]
        )

    def cell(raw: dict, key: str) -> str:
        return (raw.get(fields[key]) or "").strip() if key in fields else ""

    result = ParseResult()
    for lineno, raw in enumerate(reader, start=2):  # 1 is the header
        if not any(cell(raw, k) for k in _REQUIRED + _OPTIONAL):
            continue  # blank line
        try:
            tx = Transaction(
                date=cell(raw, "date"),
                ticker=cell(raw, "ticker"),
                action=cell(raw, "action"),
                quantity=float(cell(raw, "quantity") or 0),
                price=float(cell(raw, "price") or 0),
                currency=cell(raw, "currency") or "USD",
                fee=float(cell(raw, "fee") or 0),
                note=cell(raw, "note"),
            )
        except ValueError as exc:
            result.skipped.append(
                {"row": lineno, "type": cell(raw, "action") or "?", "reason": str(exc)}
            )
            continue
        result.transactions.append(tx)
    return result
