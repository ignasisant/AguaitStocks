"""Central config: paths, data model, watchlist loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
WATCHLIST_FILE = PROJECT_ROOT / "watchlist.yaml"

DATA_DIR.mkdir(exist_ok=True)


@dataclass
class Alert:
    """A price threshold. type is 'above' or 'below'."""

    type: str
    price: float

    def triggered(self, current: float) -> bool:
        if self.type == "above":
            return current >= self.price
        if self.type == "below":
            return current <= self.price
        raise ValueError(f"unknown alert type: {self.type!r}")


@dataclass
class Holding:
    """One ticker in the watchlist, with optional alerts."""

    ticker: str
    name: str = ""
    alerts: list[Alert] = field(default_factory=list)


def load_watchlist(path: Path = WATCHLIST_FILE) -> list[Holding]:
    """Parse watchlist.yaml into Holding objects. Empty list if missing."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    holdings: list[Holding] = []
    for item in raw.get("watchlist", []):
        alerts = [Alert(**a) for a in item.get("alerts", [])]
        holdings.append(
            Holding(ticker=item["ticker"], name=item.get("name", ""), alerts=alerts)
        )
    return holdings


def tickers(path: Path = WATCHLIST_FILE) -> list[str]:
    return [h.ticker for h in load_watchlist(path)]
