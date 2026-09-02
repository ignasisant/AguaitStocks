#!/usr/bin/env python
"""Verify stocks.data.funds.KNOWN_FUNDS against Yahoo. Run after editing it.

    uv run python scripts/check_fund_catalog.py

The catalog seeds fund classification and the picker's offline fund tier, so a
symbol Yahoo doesn't quote turns into a search suggestion that leads nowhere,
and a symbol whose quoteType is not a fund would gate the wrong sections off
the Ticker page. Both are silent failures — this makes them loud.

Network-bound (one quote lookup per symbol) and therefore not a test: pytest
runs offline. Exits non-zero when anything failed to verify.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

from stocks.data.funds import FUND_TYPES, KNOWN_FUNDS


def check(symbol: str) -> tuple[str, str, str]:
    """(symbol, verdict, detail) — verdict is 'ok', 'type' or 'missing'."""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # noqa: BLE001 — report it, don't abort the sweep
        return symbol, "missing", type(exc).__name__
    kind = str(info.get("quoteType") or "").upper()
    name = str(info.get("longName") or info.get("shortName") or "")
    if not kind:
        return symbol, "missing", "no quoteType"
    if kind not in FUND_TYPES:
        return symbol, "type", kind
    return symbol, "ok", name


def main() -> int:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, KNOWN_FUNDS))
    bad = [r for r in results if r[1] != "ok"]
    for symbol, verdict, detail in results:
        mark = "ok  " if verdict == "ok" else f"{verdict.upper():4}"
        print(f"{mark} {symbol:<10} {KNOWN_FUNDS[symbol]:<52} {detail}")
    print(f"\n{len(results) - len(bad)}/{len(results)} verified")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
