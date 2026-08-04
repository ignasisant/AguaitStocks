"""Parse a Revolut trading account-statement CSV into ledger Transactions.

Revolut's stock statement is a flat CSV, one row per event. Quirks this parser
absorbs:

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

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from stocks.portfolio.ledger import Transaction

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


@dataclass
class ParseResult:
    """Outcome of parsing one CSV: importable rows plus a human-readable audit."""

    transactions: list[Transaction] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {row, type, reason}

    @property
    def summary(self) -> str:
        n = len(self.transactions)
        return f"{n} importable, {len(self.skipped)} skipped"


def parse_csv(text: str) -> ParseResult:
    """Parse Revolut statement CSV text into a ParseResult (no side effects)."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return ParseResult()
    col = _resolve_columns(reader.fieldnames)
    return parse_rows(list(reader), col)


def parse_rows(rows: list[dict], col: dict[str, str] | None = None) -> ParseResult:
    """Shared row pipeline for the CSV and PDF front-ends.

    `rows` are raw string dicts; `col` maps logical keys to the dict's actual
    header names (identity mapping when None — used by the PDF extractor,
    which already emits canonical keys).
    """
    if col is None:
        col = {k: k for k in _COLS}
    result = ParseResult()
    for i, raw in enumerate(rows, start=2):  # row 1 is the header
        rtype = (_get(raw, col, "type") or "").strip()
        action = _map_action(rtype)
        if action is None:
            result.skipped.append(_skip_entry(i, rtype, raw, col))
            continue
        try:
            tx = _build_tx(raw, col, action)
        except (KeyError, ValueError) as exc:
            entry = _skip_entry(i, rtype, raw, col)
            entry["reason"] = str(exc)
            result.skipped.append(entry)
            continue
        result.transactions.append(tx)
    return result


def parse_file(path: str | Path) -> ParseResult:
    return parse_csv(Path(path).read_text())


def _skip_entry(row: int, rtype: str, raw: dict, col: dict[str, str]) -> dict:
    """Skipped-row audit record. Raw fields are kept so downstream steps
    (e.g. split-ratio derivation in portfolio.validate) can still use them."""
    return {
        "row": row,
        "type": rtype,
        "reason": _skip_reason(rtype),
        "date": (_get(raw, col, "date") or "").strip(),
        "ticker": (_get(raw, col, "ticker") or "").strip().upper(),
        "quantity": _money(_get(raw, col, "quantity")),
        "amount": _money(_get(raw, col, "amount")),
        "currency": (_get(raw, col, "currency") or "").strip().upper(),
    }


# --------------------------------------------------------------- column mapping
def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our logical keys to the export's actual header names (or absent)."""
    lower = {name.strip().lower(): name for name in fieldnames}
    resolved: dict[str, str] = {}
    for key, aliases in _COLS.items():
        for alias in aliases:
            if alias in lower:
                resolved[key] = lower[alias]
                break
    return resolved


def _get(raw: dict, col: dict[str, str], key: str) -> str | None:
    name = col.get(key)
    return raw.get(name) if name else None


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


# ------------------------------------------------------------------ row -> Transaction
def _build_tx(raw: dict, col: dict[str, str], action: str) -> Transaction:
    date = _parse_date(_get(raw, col, "date"))
    ticker = (_get(raw, col, "ticker") or "").strip()
    if not ticker:
        raise ValueError("missing ticker")
    currency = (_get(raw, col, "currency") or "").strip() or "USD"

    if action == "dividend":
        total = _money(_get(raw, col, "amount"))
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

    qty = _money(_get(raw, col, "quantity"))
    price = _money(_get(raw, col, "price"))
    amount = _money(_get(raw, col, "amount"))
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
    _check_consistency(action, qty, price, amount)
    return Transaction(
        date=date,
        ticker=ticker,
        action=action,
        quantity=qty,
        price=price,
        currency=currency,
        fee=_implied_fee(action, qty, price, amount),
        note="revolut",
    )


def _check_consistency(action: str, qty: float, price: float, amount: float) -> None:
    """Reject rows whose qty×price strays >2% from Total Amount.

    Within 2% the gap is cent-rounding of the per-share price plus commission
    (absorbed by `_implied_fee`); beyond it the row is corrupt — wrong column,
    truncated number — and importing it would silently distort cost basis.
    """
    if amount <= 0:  # blank total: price was derived from it or row has none
        return
    gross = qty * price
    if abs(gross - amount) > max(amount * 0.02, qty * 0.005 + 0.01):
        raise ValueError(
            f"{action} row inconsistent: {qty:g} × {price:g} = {gross:.2f} "
            f"but total is {amount:.2f} ({abs(gross - amount) / amount:.1%} off)"
        )


def _implied_fee(action: str, qty: float, price: float, amount: float) -> float:
    """Commission implied by Total Amount vs qty×price.

    Revolut's Total Amount is cash actually moved: buys cost qty×price + fee,
    sells credit qty×price − fee. The per-share price is rounded to cents, so
    only a difference beyond rounding noise yet within 2% is trusted as a fee;
    anything larger is left at 0 for the validation layer to flag.
    """
    if amount <= 0:
        return 0.0
    gross = qty * price
    fee = (amount - gross) if action == "buy" else (gross - amount)
    # qty*0.005: worst-case cent rounding of the per-share price.
    if fee <= qty * 0.005 + 0.01 or fee > amount * 0.02:
        return 0.0
    return round(fee, 2)


def _parse_date(value: str | None) -> str:
    """Return an ISO YYYY-MM-DD date from a Revolut date/timestamp field."""
    v = (value or "").strip()
    if not v:
        raise ValueError("missing date")
    # ISO timestamp "2023-01-03T14:30:00.000Z" -> "2023-01-03"; plain date passes.
    head = v.split("T", 1)[0].split(" ", 1)[0]
    parts = head.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return head
    raise ValueError(f"unrecognised date {v!r}")


def _money(value: str | None) -> float:
    """Parse a money/number field: strip currency symbols and thousands commas."""
    v = (value or "").strip()
    if not v:
        return 0.0
    kept = [c for c in v if c.isdigit() or c in ".-"]
    cleaned = "".join(kept)
    if cleaned in ("", "-", ".", "-."):
        return 0.0
    return float(cleaned)
