#!/usr/bin/env python
"""Refresh cached price history for every watchlist ticker.

Run:      uv run python scripts/update_prices.py
Schedule: add to cron / launchd for daily updates.
"""

from __future__ import annotations

from stocks.config import load_watchlist
from stocks.data.fetch import fetch_history, save_history


def main() -> None:
    for h in load_watchlist():
        df = fetch_history(h.ticker, period="1y")
        path = save_history(h.ticker, df)
        print(f"{h.ticker}: {len(df)} rows -> {path}")


if __name__ == "__main__":
    main()
