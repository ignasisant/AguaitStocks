"""Parse a Revolut trading account-statement CSV into ledger Transactions.

Revolut's stock statement is a flat CSV, one row per event, so the shared
DictReader pipeline in `stocks.portfolio.statement` runs it — this module
supplies only what is Revolut-specific. Quirks it absorbs:

* Money fields carry a currency symbol and thousands separators ("$1,301.50").
* Dates are ISO timestamps ("2023-01-03T14:30:00.000Z") — we keep the date part.
* Column names and the exact `Type` strings have drifted across app versions, so
  columns are matched case-insensitively and types are mapped by keyword.

Design choices (see module tests):

* buy / sell / dividend rows become Transactions. Cash top-ups, withdrawals and
  transfers are *not* position-affecting and are reported as skipped, never
  silently dropped.
* Stock splits are NOT auto-imported: Revolut reports the resulting share count,
  not the split ratio positions.py needs, and a wrong ratio corrupts every later
  lot. Split rows are surfaced in `skipped` with a note to add them by hand.
* Dividends map to price=gross total, fee=0. Revolut's statement does not break
  out withholding tax, so the Spanish double-tax credit will be understated —
  edit the fee on dividend rows if your statement reports withholding.

Nothing here writes to the ledger. The caller previews `ParseResult.transactions`
and only then commits (see the web Import page / ledger.add_many).
"""

from __future__ import annotations

from pathlib import Path

from stocks.portfolio import statement
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.statement import (
    CsvFormat,
    ParseResult,
    Row,
    check_consistency,
    implied_fee,
    parse_date,
)

# Canonical Revolut column names, lowercased. Matching is case-insensitive and
# tolerant of surrounding whitespace; alternates cover older/newer exports.
_COLS = {
    "date": ("date", "date acquired", "completed date", "started date"),
    "ticker": ("ticker", "symbol"),
    "type": ("type", "action"),
    "quantity": ("quantity", "shares", "no. of shares"),
    "price": ("price per share", "price"),
    "amount": ("total amount", "total", "amount"),
    "currency": ("currency", "ccy"),
}


# ------------------------------------------------------------------ type -> action
def _map_action(rtype: str) -> str | None:
    """Revolut `Type` string to a ledger action; None means 'skip this row'."""
    t = rtype.upper()
    if t.startswith("BUY"):
        return "buy"
    if t.startswith("SELL"):
        return "sell"
    # "DIVIDEND TAX (CORRECTION)" must not import as a dividend: those rows are
    # withholding adjustments that arrive in +/- pairs which cancel out.
    if "DIVIDEND" in t and "TAX" not in t:
        return "dividend"
    # split / cash / fee / transfer / reward are intentionally not auto-imported.
    return None


def _skip_reason(rtype: str) -> str:
    t = rtype.upper()
    if "SPLIT" in t:
        return "stock split — ratio derived at validation, or add manually"
    if "DIVIDEND" in t and "TAX" in t:
        return "dividend tax correction — arrives in +/- pairs, review manually"
    if "RETURN OF CAPITAL" in t:
        return "return of capital — reduces cost basis, adjust manually"
    if "REWARD" in t:
        return "reward — cash credit, not position-affecting"
    if "FEE" in t:
        return "fee row — add manually if you want it in cost basis"
    if any(k in t for k in ("TOP-UP", "TOP UP", "WITHDRAWAL", "TRANSFER", "CASH")):
        return "cash movement — not position-affecting"
    return "unrecognised type — not imported"


def _audit(row: Row) -> dict:
    """Raw fields kept on a skipped row so downstream steps (e.g. split-ratio
    derivation in portfolio.validate) can still use them."""
    return {
        "date": row.text("date"),
        "ticker": row.upper("ticker"),
        "quantity": row.money("quantity"),
        "amount": row.money("amount"),
        "currency": row.upper("currency"),
    }


# ------------------------------------------------------------------ row -> Transaction
def _build_tx(row: Row, action: str) -> Transaction:
    date = parse_date(row.text("date"))
    ticker = row.text("ticker")
    if not ticker:
        raise ValueError("missing ticker")
    currency = row.text("currency") or "USD"

    if action == "dividend":
        total = row.money("amount")
        if total <= 0:
            raise ValueError(f"dividend amount {total} is not positive")
        # price = gross dividend total; quantity/fee left at 0 (see module doc).
        return Transaction(
            date=date,
            ticker=ticker,
            action="dividend",
            price=total,
            currency=currency,
            note="revolut",
        )

    qty = row.money("quantity")
    price = row.money("price")
    amount = row.money("amount")
    if price == 0.0:  # derive from total when per-share price is blank
        price = amount / qty if qty else 0.0
    if qty <= 0:
        raise ValueError(f"{action} row has no quantity")
    if price < 0:
        raise ValueError(f"{action} row has negative price {price}")
    # A sell at price 0 *and* total 0 is how Revolut records a worthless
    # disposal (delisting) — importable, it realizes the full loss. A zero
    # price anywhere else is corrupt data.
    if price == 0 and not (action == "sell" and amount == 0):
        raise ValueError(f"{action} row has zero price")
    if amount < 0:
        raise ValueError(f"{action} row has negative total {amount}")
    check_consistency(action, qty, price, amount)
    return Transaction(
        date=date,
        ticker=ticker,
        action=action,
        quantity=qty,
        price=price,
        currency=currency,
        fee=implied_fee(action, qty, price, amount),
        note="revolut",
    )


FORMAT = CsvFormat(
    columns=_COLS,
    type_key="type",
    action_of=_map_action,
    skip_reason=_skip_reason,
    build=_build_tx,
    audit=_audit,
)


def parse_csv(text: str) -> ParseResult:
    """Parse Revolut statement CSV text into a ParseResult (no side effects)."""
    return statement.parse_csv(text, FORMAT)


def parse_rows(rows: list[dict], col: dict[str, str] | None = None) -> ParseResult:
    """Shared row pipeline for the CSV and PDF front-ends.

    `rows` are raw string dicts; `col` maps logical keys to the dict's actual
    header names (identity mapping when None — used by the PDF extractor,
    which already emits canonical keys).
    """
    return statement.parse_rows(rows, FORMAT, col)


def parse_file(path: str | Path) -> ParseResult:
    return parse_csv(Path(path).read_text())
