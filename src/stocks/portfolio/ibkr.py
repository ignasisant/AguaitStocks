"""Parse an Interactive Brokers activity-statement CSV into Transactions.

IBKR's activity statement (Performance & Reports → Statements → Activity →
CSV) is not one table: it concatenates many sections, each row prefixed with
its section name and a row kind (``Trades,Header,…`` / ``Trades,Data,…``).
This parser is deliberately strict: it refuses the whole file unless it finds
a ``Trades`` or ``Dividends`` header row with the exact columns IBKR emits,
so a statement from another broker can never be half-imported by accident.

What imports, and how:

* ``Trades`` section, ``DataDiscriminator == Order``, asset category Stocks —
  buys (positive quantity) and sells (negative). Per-share price is
  ``T. Price`` in the row's currency; ``Comm/Fee`` (always negative in the
  statement) becomes the fee — IBKR charges commission in the trade currency.
  ``SubTotal``/``Total``/``ClosedLot`` rows are derived lines, not events,
  and are dropped without comment.
* Other asset categories (Forex, Options, CFDs…) are skipped with a reason —
  the ledger models stock/ETF positions only.
* ``Dividends`` section — the ticker is parsed from the description prefix
  ("AAPL(US03…) Cash Dividend…"); amount is the gross payment. Per-currency
  ``Total`` summary rows are dropped.
* ``Withholding Tax`` rows are listed as skipped with a pointer to set the
  tax as the fee on the matching dividend row (the Spanish double-tax credit
  convention) — pairing them automatically across sections is not reliable
  when corrections restate an earlier payment.

Nothing here writes to the ledger; the Import page previews and commits.
"""

from __future__ import annotations

import csv
import io
import re

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult, _money, _parse_date

_TRADE_COLS = (
    "DataDiscriminator", "Asset Category", "Currency", "Symbol",
    "Date/Time", "Quantity", "T. Price",
)
_DIVIDEND_COLS = ("Currency", "Date", "Description", "Amount")

# "AAPL(US0378331005) Cash Dividend USD 0.24 per Share" -> AAPL
_DESC_TICKER = re.compile(r"^([A-Z0-9.\- ]+?)\s*\(")


def parse_csv(text: str) -> ParseResult:
    """Parse IBKR activity-statement CSV text into a ParseResult (no writes)."""
    result = ParseResult()
    headers: dict[str, list[str]] = {}  # section name -> current column list
    recognised = False

    for i, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if len(row) < 3:
            continue
        section, kind, rest = row[0], row[1], row[2:]
        if kind == "Header":
            headers[section] = rest
            if section == "Trades" and all(c in rest for c in _TRADE_COLS):
                recognised = True
            if section == "Dividends" and all(c in rest for c in _DIVIDEND_COLS):
                recognised = True
            continue
        if kind != "Data" or section not in headers:
            continue
        cells = dict(zip(headers[section], rest, strict=False))
        if section == "Trades":
            _trade_row(i, cells, result)
        elif section == "Dividends":
            _dividend_row(i, cells, result)
        elif section == "Withholding Tax":
            _withholding_row(i, cells, result)

    if not recognised:
        return ParseResult(
            skipped=[{
                "row": 1,
                "type": "header",
                "reason": (
                    "not an IBKR activity statement — no Trades/Dividends "
                    "section with the expected columns"
                ),
            }]
        )
    return result


def _trade_row(line: int, cells: dict[str, str], result: ParseResult) -> None:
    if cells.get("DataDiscriminator") != "Order":
        return  # ClosedLot / SubTotal / Total — derived lines, not events
    category = (cells.get("Asset Category") or "").strip()
    ticker = (cells.get("Symbol") or "").strip()
    qty = _money(cells.get("Quantity"))
    try:
        if category not in ("Stocks", "ETFs"):
            raise ValueError(f"asset category {category or '?'} — not auto-imported")
        if not ticker:
            raise ValueError("missing symbol")
        date = _parse_date((cells.get("Date/Time") or "").split(",", 1)[0])
        price = _money(cells.get("T. Price"))
        if qty == 0:
            raise ValueError("trade row has no quantity")
        if price <= 0:
            raise ValueError(f"trade row has non-positive price {price:g}")
        result.transactions.append(
            Transaction(
                date=date,
                ticker=ticker,
                action="buy" if qty > 0 else "sell",
                quantity=abs(qty),
                price=price,
                currency=(cells.get("Currency") or "USD").strip(),
                fee=abs(_money(cells.get("Comm/Fee"))),
                note="ibkr",
            )
        )
    except ValueError as exc:
        result.skipped.append(
            _skip(line, category or "trade", str(exc), cells, ticker, qty)
        )


def _dividend_row(line: int, cells: dict[str, str], result: ParseResult) -> None:
    currency = (cells.get("Currency") or "").strip()
    if not currency or currency.startswith("Total"):
        return  # per-currency summary line, not an event
    desc = (cells.get("Description") or "").strip()
    try:
        m = _DESC_TICKER.match(desc)
        if not m:
            raise ValueError(f"cannot read ticker from description {desc!r}")
        amount = _money(cells.get("Amount"))
        if amount <= 0:
            raise ValueError(f"dividend amount {amount:g} is not positive")
        result.transactions.append(
            Transaction(
                date=_parse_date(cells.get("Date")),
                ticker=m.group(1).strip(),
                action="dividend",
                price=amount,
                currency=currency,
                note="ibkr",
            )
        )
    except ValueError as exc:
        result.skipped.append(_skip(line, "dividend", str(exc), cells, "", 0.0))


def _withholding_row(line: int, cells: dict[str, str], result: ParseResult) -> None:
    currency = (cells.get("Currency") or "").strip()
    if not currency or currency.startswith("Total"):
        return
    desc = (cells.get("Description") or "").strip()
    m = _DESC_TICKER.match(desc)
    result.skipped.append({
        "row": line,
        "type": "withholding tax",
        "reason": (
            "withholding tax — set it as the fee on the matching dividend "
            "row for the double-tax credit"
        ),
        "date": (cells.get("Date") or "").strip(),
        "ticker": m.group(1).strip() if m else "",
        "quantity": 0.0,
        "amount": _money(cells.get("Amount")),
        "currency": currency,
    })


def _skip(
    line: int, rtype: str, reason: str, cells: dict[str, str], ticker: str, qty: float
) -> dict:
    return {
        "row": line,
        "type": rtype,
        "reason": reason,
        "date": (cells.get("Date/Time") or cells.get("Date") or "").split(",", 1)[0],
        "ticker": ticker.upper(),
        "quantity": qty,
        "amount": _money(cells.get("Amount") or cells.get("Proceeds")),
        "currency": (cells.get("Currency") or "").strip().upper(),
    }
