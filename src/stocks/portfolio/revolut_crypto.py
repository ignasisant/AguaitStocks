"""Parse a Revolut crypto account-statement CSV into ledger Transactions.

Revolut's crypto statement is a flat CSV like the stock one (one row per
event) but differs in three ways this parser absorbs:

* The symbol is a bare coin code (BTC), not a market symbol. Rows are
  normalized to the Yahoo pair for the statement's fiat currency (BTC + EUR
  money fields -> ticker BTC-EUR), so the transaction currency and the quote
  currency of every later price lookup are the same one — FIFO, FX and tax
  work unchanged, and no per-user alias is needed. Bare codes must NOT be
  kept: they collide with real stock tickers (SOL, LINK, …).
* There is usually no currency column: the fiat currency is sniffed from the
  money fields' symbol/code (€, $, £, "1.234,56 EUR", …), defaulting to USD.
* Fees come in an explicit column instead of being implied from the total.

Buy / sell rows become Transactions. Everything else — send/receive,
exchanges (coin-to-coin), staking/learn rewards — is reported as skipped
with a reason, never silently dropped: transfers carry no cost basis, and
rewards are acquisitions at market value the user must add manually (they
are taxable income in Spain).

Nothing here writes to the ledger; the Import page previews and commits.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from stocks.data.crypto import to_pair
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult, _money, _parse_date

# Logical key -> accepted header names (lowercased); exports drift across
# app versions, so matching is case-insensitive like the stock parser's.
_COLS = {
    "symbol": ("symbol", "ticker", "asset", "cryptocurrency"),
    "type": ("type", "transaction type", "action"),
    "quantity": ("quantity", "amount"),
    "price": ("price", "price per coin", "price per unit"),
    "value": ("value", "total", "total amount", "fiat amount"),
    "fee": ("fees", "fee"),
    "date": ("date", "completed date", "started date", "timestamp"),
    "currency": ("currency", "fiat currency", "base currency"),
}

# Money-field symbol/code -> ISO currency, for statements without a currency
# column. Checked against the raw price/value strings.
_CCY_MARKS = (
    ("€", "EUR"),
    ("EUR", "EUR"),
    ("£", "GBP"),
    ("GBP", "GBP"),
    ("US$", "USD"),
    ("$", "USD"),
    ("USD", "USD"),
)


def parse_csv(text: str) -> ParseResult:
    """Parse Revolut crypto-statement CSV text (no side effects)."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return ParseResult()
    lower = {name.strip().lower(): name for name in reader.fieldnames}
    col: dict[str, str] = {}
    for key, aliases in _COLS.items():
        for alias in aliases:
            if alias in lower:
                col[key] = lower[alias]
                break

    result = ParseResult()
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
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


def _get(raw: dict, col: dict[str, str], key: str) -> str | None:
    name = col.get(key)
    return raw.get(name) if name else None


def _map_action(rtype: str) -> str | None:
    t = rtype.upper()
    if t.startswith("BUY"):
        return "buy"
    if t.startswith("SELL"):
        return "sell"
    return None  # send/receive/exchange/rewards — skipped with a reason


def _skip_reason(rtype: str) -> str:
    t = rtype.upper()
    if any(k in t for k in ("REWARD", "STAKING", "LEARN")):
        return (
            "reward — an acquisition at market value (taxable income in "
            "Spain); add manually as a buy at the reward-day price"
        )
    if "EXCHANGE" in t or "CONVERT" in t:
        return (
            "coin-to-coin exchange — fiscally a sell plus a buy; add both "
            "legs manually at the exchange-day prices"
        )
    if any(k in t for k in ("SEND", "RECEIVE", "TRANSFER", "WITHDRAW", "DEPOSIT")):
        return "transfer — moves coins without a price; adjust manually if it was a disposal"
    return "unrecognised type — not imported"


def _skip_entry(row: int, rtype: str, raw: dict, col: dict[str, str]) -> dict:
    return {
        "row": row,
        "type": rtype,
        "reason": _skip_reason(rtype),
        "date": (_get(raw, col, "date") or "").strip(),
        "ticker": (_get(raw, col, "symbol") or "").strip().upper(),
        "quantity": _money(_get(raw, col, "quantity")),
        "amount": _money(_get(raw, col, "value")),
        "currency": _currency(raw, col),
    }


def _currency(raw: dict, col: dict[str, str]) -> str:
    """Fiat currency: explicit column first, else sniffed from money fields."""
    explicit = (_get(raw, col, "currency") or "").strip().upper()
    if explicit:
        return explicit
    for key in ("price", "value", "fee"):
        v = (_get(raw, col, key) or "").upper()
        for mark, ccy in _CCY_MARKS:
            if mark in v:
                return ccy
    return "USD"


def _parse_any_date(value: str | None) -> str:
    """ISO date from an ISO timestamp, or from the prose formats some crypto
    exports use ("Jan 5, 2025, 2:31:41 PM")."""
    try:
        return _parse_date(value)
    except ValueError:
        import pandas as pd

        ts = pd.to_datetime((value or "").strip(), errors="coerce")
        if pd.isna(ts):
            raise ValueError(f"unrecognised date {value!r}") from None
        return ts.date().isoformat()


def _build_tx(raw: dict, col: dict[str, str], action: str) -> Transaction:
    date = _parse_any_date(_get(raw, col, "date"))
    coin = (_get(raw, col, "symbol") or "").strip().upper()
    if not coin:
        raise ValueError("missing symbol")
    currency = _currency(raw, col)

    qty = _money(_get(raw, col, "quantity"))
    price = _money(_get(raw, col, "price"))
    value = _money(_get(raw, col, "value"))
    fee = _money(_get(raw, col, "fee"))
    if qty <= 0:
        raise ValueError(f"{action} row has no quantity")
    if price == 0.0 and value and qty:  # derive when per-coin price is blank
        price = value / qty
    if price <= 0:
        raise ValueError(f"{action} row has no price")
    if fee < 0:
        raise ValueError(f"{action} row has negative fee {fee}")
    # Value should be qty×price give or take the fee and price rounding;
    # beyond that the row is corrupt (wrong column, truncated number).
    if value > 0:
        gross = qty * price
        if abs(gross - value) > max(value * 0.02, fee + qty * 0.005 + 0.01):
            raise ValueError(
                f"{action} row inconsistent: {qty:g} × {price:g} = {gross:.2f} "
                f"but value is {value:.2f}"
            )
    return Transaction(
        date=date,
        # Full Yahoo pair, in the statement's fiat — see module doc.
        ticker=to_pair(coin, currency),
        action=action,
        quantity=qty,
        price=price,
        currency=currency,
        fee=fee,
        note=f"revolut crypto {coin}",
    )
