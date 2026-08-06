"""Parse a DEGIRO Transactions.csv into ledger Transactions.

DEGIRO's transaction export (Actividad → Transacciones → Exportar → CSV) has
a positional quirk no DictReader survives: money columns are followed by
*unnamed* currency columns (``…,Price,,Local value,,…``), so rows are parsed
positionally and each money field's currency is read from the cell to its
right. English and Spanish headers are recognised; the parser refuses the
whole file unless the distinctive columns (date, product, ISIN, quantity,
price) are all found — a statement from another broker can never be
half-imported by accident (a ``Ticker`` header is an explicit refusal: DEGIRO
exports never have one).

Shape notes this parser absorbs:

* There is no ticker column — rows import with the **ISIN as the ticker**
  and the product name in the note. Validation flags each unknown ISIN with
  instructions to map it to a Yahoo symbol under ``aliases:`` in
  watchlist.yaml (the established EU-broker-code mechanism); prices won't
  resolve until then.
* Numbers are locale-formatted ("1.234,56" in the Spanish export) and dates
  are DD-MM-YYYY — both normalised here.
* Buy vs sell is the sign of the quantity column.
* Fees ("Transaction costs" / "Costes de transacción") are folded into the
  transaction only when charged in the trade currency; DEGIRO usually charges
  them in the account currency, in which case they import as 0 rather than
  mixing currencies.
* Each row is cross-checked against its own "Local value" column (qty × price
  within 2%) so a mis-detected decimal separator cannot corrupt cost basis.
* Dividends are NOT in Transactions.csv (they live in the Account statement)
  — import them via the generic CSV or add manually. Corporate actions
  (splits, ISIN changes) appear as ordinary buy/sell pairs and import as
  such; review those manually.

Nothing here writes to the ledger; the Import page previews and commits.
"""

from __future__ import annotations

import csv
import io

from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult

# Logical key -> accepted header names (lowercased), English and Spanish.
_HEADERS = {
    "date": ("date", "fecha"),
    "product": ("product", "producto"),
    "isin": ("isin",),
    "quantity": ("quantity", "número", "numero", "cantidad"),
    "price": ("price", "precio"),
    "local_value": ("local value", "valor local"),
    "costs": (),  # matched by prefix, see _find_columns
}
_COST_PREFIXES = ("transaction", "costes", "gastos")
_REQUIRED = ("date", "product", "isin", "quantity", "price")


def parse_csv(text: str) -> ParseResult:
    """Parse DEGIRO Transactions.csv text into a ParseResult (no side effects)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ParseResult()
    header = [h.strip().lower() for h in rows[0]]

    if "ticker" in header:
        return _refuse("has a Ticker column — not a DEGIRO Transactions.csv")
    idx = _find_columns(header)
    missing = [k for k in _REQUIRED if k not in idx]
    if missing:
        return _refuse(
            "not a DEGIRO Transactions.csv — missing column(s): " + ", ".join(missing)
        )

    result = ParseResult()
    for i, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue  # blank line
        try:
            tx = _build_tx(row, idx)
        except (IndexError, ValueError) as exc:
            result.skipped.append({
                "row": i,
                "type": _cell(row, idx.get("product")),
                "reason": str(exc),
                "date": _cell(row, idx.get("date")),
                "ticker": _cell(row, idx.get("isin")).upper(),
                "quantity": _num(_cell(row, idx.get("quantity"))),
                "amount": 0.0,
                "currency": "",
            })
            continue
        result.transactions.append(tx)
    return result


def _refuse(reason: str) -> ParseResult:
    return ParseResult(skipped=[{"row": 1, "type": "header", "reason": reason}])


def _find_columns(header: list[str]) -> dict[str, int]:
    """Positional index of each logical column (currency = named col + 1)."""
    idx: dict[str, int] = {}
    for key, names in _HEADERS.items():
        for pos, name in enumerate(header):
            if name in names or (key == "costs" and name.startswith(_COST_PREFIXES)):
                idx[key] = pos
                break
    return idx


def _cell(row: list[str], pos: int | None) -> str:
    return row[pos].strip() if pos is not None and pos < len(row) else ""


def _ccy(row: list[str], pos: int | None) -> str:
    """Currency of a money column: the unnamed cell immediately to its right."""
    return _cell(row, pos + 1).upper() if pos is not None else ""


def _build_tx(row: list[str], idx: dict[str, int]) -> Transaction:
    isin = _cell(row, idx["isin"]).upper()
    if not isin:
        raise ValueError("missing ISIN")
    date = _parse_date(_cell(row, idx["date"]))
    qty = _num(_cell(row, idx["quantity"]))
    if qty == 0:
        raise ValueError("row has no quantity")
    price = _num(_cell(row, idx["price"]))
    if price <= 0:
        raise ValueError(f"row has non-positive price {price:g}")
    currency = _ccy(row, idx["price"])
    if not currency:
        raise ValueError("price column has no currency")

    # Guard against decimal-separator misreads: qty × price must match the
    # statement's own Local value column (same currency) within 2%.
    local_pos = idx.get("local_value")
    local = abs(_num(_cell(row, local_pos))) if local_pos is not None else 0.0
    if local > 0 and _ccy(row, local_pos) == currency:
        gross = abs(qty) * price
        if abs(gross - local) > local * 0.02:
            raise ValueError(
                f"row inconsistent: {abs(qty):g} × {price:g} = {gross:.2f} "
                f"but local value is {local:.2f}"
            )

    fee = 0.0
    cost_pos = idx.get("costs")
    if cost_pos is not None and _ccy(row, cost_pos) == currency:
        fee = abs(_num(_cell(row, cost_pos)))

    return Transaction(
        date=date,
        ticker=isin,
        action="buy" if qty > 0 else "sell",
        quantity=abs(qty),
        price=price,
        currency=currency,
        fee=fee,
        note=f"degiro {_cell(row, idx['product'])}".strip(),
    )


def _parse_date(value: str) -> str:
    """DD-MM-YYYY (or DD/MM/YYYY, or already-ISO) to ISO YYYY-MM-DD."""
    v = value.strip().split(" ", 1)[0]
    if not v:
        raise ValueError("missing date")
    parts = v.replace("/", "-").split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return "-".join(parts)
        if len(parts[2]) == 4:
            return f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"
    raise ValueError(f"unrecognised date {value!r}")


def _num(value: str) -> float:
    """Parse a locale-formatted number: "1.234,56", "1,234.56", "-2,5", "10".

    When both separators appear the rightmost one is the decimal mark. A lone
    comma is a decimal mark unless it reads as a thousands group (",ddd" with
    a multi-digit head) — the Local-value cross-check catches the rare
    ambiguous case this heuristic gets wrong.
    """
    v = value.strip().replace("\xa0", "").replace(" ", "")
    if not v:
        return 0.0
    if "," in v and "." in v:
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    elif "," in v:
        head, _, tail = v.rpartition(",")
        if v.count(",") > 1 or (len(tail) == 3 and len(head.lstrip("+-")) > 1):
            v = v.replace(",", "")
        else:
            v = head + "." + tail
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"unparseable number {value!r}") from None
