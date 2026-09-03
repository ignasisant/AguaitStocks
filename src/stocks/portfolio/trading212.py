"""Parse a Trading 212 transaction-export CSV into ledger Transactions.

Trading 212's export (web app → History → Export) is a flat CSV, one row per
event, with a stable English header. This parser is deliberately strict: it
refuses the whole file unless every distinctive Trading 212 column is present
(`Action`, `Time`, `ISIN`, `Ticker`, `No. of shares`, `Price / share`), so a
statement from another broker can never be half-imported by accident.

Shape notes this parser absorbs:

* `Price / share` is quoted in the instrument currency
  (`Currency (Price / share)`) while `Total` is in the account currency.
  Trades are recorded in the *instrument* currency — the ledger stores native
  amounts and converts to EUR at the trade date downstream (stocks.data.fx).
* Trading 212 charges no commission; the currency-conversion fee column is in
  the account currency and is NOT folded into `fee` (wrong currency for the
  transaction). Fees import as 0.
* Dividends: gross = shares × per-share amount in the instrument currency,
  and the withholding-tax column becomes the fee — the convention the Spanish
  double-tax credit reads (see revolut.py) — when it is reported in that same
  currency. If the per-share fields are blank the row falls back to the
  account-currency `Total` with fee 0.
* Deposits, withdrawals, interest, currency conversions and result
  adjustments are skipped with a reason, never silently dropped. Stock-split
  rows keep the shared skipped-entry shape so validate.resolve_splits can
  derive the ratio.

Nothing here writes to the ledger; the Import page previews and commits.
"""

from __future__ import annotations

from stocks.portfolio import statement
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.statement import CsvFormat, ParseResult, Row, parse_date

# Logical key -> exact Trading 212 header, lowercased (their export is stable,
# so each key has a single spelling; matching is case-insensitive and
# whitespace-tolerant only).
_COLS = {
    "action": ("action",),
    "time": ("time",),
    "isin": ("isin",),
    "ticker": ("ticker",),
    "shares": ("no. of shares",),
    "price": ("price / share",),
    "price_ccy": ("currency (price / share)",),
    "total": ("total",),
    "total_ccy": ("currency (total)",),
    "wht": ("withholding tax",),
    "wht_ccy": ("currency (withholding tax)",),
}

# Every one of these must be present or the file is not a Trading 212 export.
_REQUIRED = ("action", "time", "isin", "ticker", "shares", "price")


def _map_action(rtype: str) -> str | None:
    """Trading 212 `Action` string to a ledger action; None means skip."""
    t = rtype.lower()
    if t.endswith("buy"):  # Market buy / Limit buy / Stop buy
        return "buy"
    if t.endswith("sell"):
        return "sell"
    if t.startswith("dividend"):  # Dividend (Ordinary), (…US corporations), …
        return "dividend"
    return None


def _skip_reason(rtype: str) -> str:
    t = rtype.lower()
    if "split" in t:
        return "stock split — ratio derived at validation, or add manually"
    if "deposit" in t or "withdrawal" in t:
        return "cash movement — not position-affecting"
    if "interest" in t:
        return "interest — cash credit, not position-affecting"
    if "currency conversion" in t:
        return "currency conversion — not position-affecting"
    if "adjustment" in t:
        return "result adjustment — review manually"
    return "unrecognised type — not imported"


def _audit(row: Row) -> dict:
    """Same shape as revolut's skipped entries — validate.resolve_splits and
    the preview table read these fields."""
    return {
        "date": row.text("time"),
        "ticker": row.upper("ticker"),
        "quantity": row.money("shares"),
        "amount": row.money("total"),
        "currency": row.upper("total_ccy"),
    }


def _build_tx(row: Row, action: str) -> Transaction:
    date = parse_date(row.text("time"))
    ticker = row.text("ticker")
    if not ticker:
        raise ValueError("missing ticker")

    if action == "dividend":
        return _build_dividend(row, date, ticker)

    qty = row.money("shares")
    price = row.money("price")
    currency = row.text("price_ccy") or row.text("total_ccy") or "USD"
    if qty <= 0:
        raise ValueError(f"{action} row has no quantity")
    if price <= 0:
        raise ValueError(f"{action} row has no per-share price")
    return Transaction(
        date=date,
        ticker=ticker,
        action=action,
        quantity=qty,
        price=price,
        currency=currency,
        note="trading212",
    )


def _build_dividend(row: Row, date: str, ticker: str) -> Transaction:
    qty = row.money("shares")
    per_share = row.money("price")
    inst_ccy = row.upper("price_ccy")
    wht = row.money("wht")
    wht_ccy = row.upper("wht_ccy")

    if qty > 0 and per_share > 0 and inst_ccy:
        # Gross in the instrument currency; withholding as fee when it is
        # reported in that same currency (else it would mix currencies).
        return Transaction(
            date=date,
            ticker=ticker,
            action="dividend",
            price=round(qty * per_share, 4),
            currency=inst_ccy,
            fee=wht if wht > 0 and wht_ccy == inst_ccy else 0.0,
            note="trading212",
        )

    total = row.money("total")
    total_ccy = row.text("total_ccy") or "USD"
    if total <= 0:
        raise ValueError(f"dividend amount {total} is not positive")
    return Transaction(
        date=date,
        ticker=ticker,
        action="dividend",
        price=total,
        currency=total_ccy,
        fee=wht if wht > 0 and wht_ccy == total_ccy.upper() else 0.0,
        note="trading212",
    )


FORMAT = CsvFormat(
    columns=_COLS,
    type_key="action",
    action_of=_map_action,
    skip_reason=_skip_reason,
    build=lambda row, action: _build_tx(row, action),
    audit=_audit,
    required=_REQUIRED,
    refusal="not a Trading 212 export — missing column(s): ",
    blank_when_empty=("action", "time"),
)


def parse_csv(text: str) -> ParseResult:
    """Parse Trading 212 export CSV text into a ParseResult (no side effects)."""
    return statement.parse_csv(text, FORMAT)
