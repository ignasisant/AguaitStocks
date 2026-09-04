"""Ledger export — the CSV the Profile page hands back.

The privacy policy promises the data is the account's own to take away, and
"take away" has to mean a file, not a screenshot of a table. The one thing
worth doing here beyond dumping the rows is the conversion: the app reckons in
the account's reference currency, so an export whose amounts are still in five
native currencies is not the book the user sees. Every row carries the ECB
rate for its OWN trade date and the converted amount alongside the original.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from stocks.portfolio.ledger import all_transactions

# The signed amount a row moves, by action. `split` has no cash leg, so it
# exports with an empty amount rather than a zero that would look like a
# free trade.
_GROSS = {
    "buy": lambda t: t.quantity * t.price,
    "sell": lambda t: t.quantity * t.price,
    "dividend": lambda t: t.price,
    "fee": lambda t: t.fee,
}


def ledger_csv(db: Path | str, base: str = "EUR") -> bytes:
    """Every transaction as CSV, each row also converted to `base`.

    Best-effort on the FX: the rates are warmed in one range request per
    currency, and a row whose rate cannot be fetched exports with an empty
    rate and an empty converted amount rather than a wrong one — an offline
    export is still the ledger.
    """
    from stocks.data import fx

    txs = all_transactions(Path(db))
    if not txs:
        return b""
    try:
        fx.prefetch([(t.date, t.currency) for t in txs], base)
    except Exception:  # noqa: BLE001 — the native columns are the export
        pass

    rows = []
    for t in txs:
        gross = _GROSS[t.action](t) if t.action in _GROSS else None
        try:
            rate = fx.rate_on(t.date, t.currency, base) if gross is not None else None
        except Exception:  # noqa: BLE001 — one unreachable date, not the export
            rate = None
        rows.append(
            {
                "date": t.date,
                "ticker": t.ticker,
                "action": t.action,
                "quantity": t.quantity,
                "price": t.price,
                "currency": t.currency,
                "fee": t.fee,
                "note": t.note,
                "fx_rate": rate,
                f"amount_{base.lower()}": (
                    None if (gross is None or rate is None) else gross * rate
                ),
            }
        )
    return pd.DataFrame(rows).to_csv(index=False).encode()
