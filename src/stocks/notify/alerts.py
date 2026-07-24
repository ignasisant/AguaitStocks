"""Evaluate watchlist alerts against latest prices."""

from __future__ import annotations

from dataclasses import dataclass

from stocks.config import Holding, load_watchlist
from stocks.data.fetch import latest_price


@dataclass
class AlertHit:
    ticker: str
    type: str
    threshold: float
    current: float

    def __str__(self) -> str:
        return (
            f"{self.ticker}: {self.type} {self.threshold} "
            f"(current {self.current:.2f})"
        )


def check_holding(holding: Holding) -> list[AlertHit]:
    if not holding.alerts:
        return []
    price = latest_price(holding.ticker)
    return [
        AlertHit(holding.ticker, a.type, a.price, price)
        for a in holding.alerts
        if a.triggered(price)
    ]


def check_all() -> list[AlertHit]:
    hits: list[AlertHit] = []
    for holding in load_watchlist():
        hits.extend(check_holding(holding))
    return hits
