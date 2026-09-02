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
# watchlist.yaml is git-ignored (personal positions); the tracked example
# carries the global reference maps (aliases/tv) for checkouts without it.
EXAMPLE_WATCHLIST_FILE = PROJECT_ROOT / "watchlist.example.yaml"

DATA_DIR.mkdir(exist_ok=True)


# Alert types that only need the latest price (evaluated by Alert.triggered).
# Reporting currencies the app can reckon in, and how their amounts are
# prefixed. Here rather than in web/auth.py because the headless senders (the
# Telegram digest, the CLI) format money too and must not import Streamlit.
# Frankfurter serves any ECB-quoted base, so adding one is a line here. The
# Nordic currencies all write "kr", so they take the code-prefix form rather
# than a symbol nobody could tell apart in a table.
CURRENCIES = (
    "EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "CAD", "AUD",
)
CURRENCY_SYMBOL = {
    "EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF ",
    "SEK": "SEK ", "NOK": "NOK ", "DKK": "DKK ", "PLN": "zł",
    "CZK": "Kč", "CAD": "CA$", "AUD": "A$",
}


def currency_symbol(ccy: str | None) -> str:
    return CURRENCY_SYMBOL.get(str(ccy or "").upper(), "")


PRICE_THRESHOLD_TYPES = frozenset({"above", "below"})
# Alert types evaluated against price history in stocks.notify.alerts.
HISTORY_ALERT_TYPES = frozenset(
    {"pct_move", "drawdown", "rsi_below", "rsi_above", "sma_cross", "high_52w", "low_52w"}
)
ALERT_TYPES = PRICE_THRESHOLD_TYPES | HISTORY_ALERT_TYPES


@dataclass
class Alert:
    """A watchlist alert rule.

    Simple price thresholds ('above'/'below') use `price` and are evaluated by
    `triggered`. History-based rules ('pct_move', 'drawdown', 'rsi_below',
    'rsi_above', 'sma_cross', 'high_52w', 'low_52w') use `pct`/`level`/`window`
    and are evaluated by stocks.notify.alerts against price history.
    """

    type: str
    price: float | None = None
    pct: float | None = None  # percent magnitude, e.g. 5 == 5%
    level: float | None = None  # absolute level, e.g. RSI 30
    window: int | None = None  # lookback in trading days (SMA span, drawdown window)

    def __post_init__(self) -> None:
        if self.type not in ALERT_TYPES:
            raise ValueError(f"unknown alert type: {self.type!r}")

    def triggered(self, current: float) -> bool:
        """Evaluate a price-threshold alert against the latest price."""
        if self.type == "above":
            return self.price is not None and current >= self.price
        if self.type == "below":
            return self.price is not None and current <= self.price
        raise ValueError(f"{self.type!r} is history-based; evaluate in notify.alerts")


@dataclass
class Holding:
    """One ticker in the watchlist.

    `shares` (>0) turns a watchlist entry into a real position: portfolio
    analytics then weights by market value instead of equal-weighting. `cost`
    is the average purchase price per share, for unrealised P/L.
    """

    ticker: str
    name: str = ""
    favorite: bool = False
    shares: float = 0.0
    cost: float | None = None
    tags: list[str] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    @property
    def is_position(self) -> bool:
        return self.shares > 0


def _reference_raw(path: Path) -> dict:
    """Raw watchlist YAML for the global reference maps (aliases/tv).

    The default watchlist.yaml is git-ignored, so fresh checkouts and deploys
    don't have it; fall back to the tracked example there so broker-code
    resolution works before the personal file exists. Explicit paths (tests,
    per-user files) never fall back.
    """
    if not path.exists() and path == WATCHLIST_FILE:
        path = EXAMPLE_WATCHLIST_FILE
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def ticker_aliases(path: Path = WATCHLIST_FILE) -> dict[str, str]:
    """Broker ticker code -> Yahoo Finance symbol, from watchlist.yaml `aliases`.

    Revolut lists EU stocks under bare local codes (RCF, HMI…) that Yahoo
    doesn't know; the ledger and watchlist keep the broker code and every
    yfinance lookup resolves through this map (stocks.data.fetch.resolve).
    """
    raw = _reference_raw(path)
    return {
        str(code).upper(): str(symbol)
        for code, symbol in (raw.get("aliases") or {}).items()
    }


def tv_symbols(path: Path = WATCHLIST_FILE) -> dict[str, str]:
    """Broker ticker code -> TradingView symbol spec, from watchlist.yaml `tv`.

    TradingView needs an explicit venue (exchange + regional screener) that
    Yahoo doesn't, and its symbols differ from `aliases` (which target Yahoo).
    Spec form is ``EXCHANGE:SYMBOL`` with an optional ``@screener`` suffix, e.g.
    ``BME:RCF@spain`` or ``NASDAQ:NVDA`` (screener defaults to ``america``).
    Codes without a mapping fall back to probing the common US venues; see
    stocks.data.tradingview.candidates. Empty dict when the file or key is
    absent, so TradingView support is entirely opt-in.
    """
    raw = _reference_raw(path)
    return {
        str(code).upper(): str(spec)
        for code, spec in (raw.get("tv") or {}).items()
    }


def load_watchlist(path: Path = WATCHLIST_FILE) -> list[Holding]:
    """Parse watchlist.yaml into Holding objects. Empty list if missing."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    holdings: list[Holding] = []
    for item in raw.get("watchlist", []):
        alerts = [Alert(**a) for a in item.get("alerts", [])]
        holdings.append(
            Holding(
                ticker=item["ticker"],
                name=item.get("name", ""),
                favorite=bool(item.get("favorite", False)),
                shares=float(item.get("shares", 0) or 0),
                cost=item.get("cost"),
                tags=[str(t) for t in (item.get("tags") or [])],
                alerts=alerts,
            )
        )
    return holdings


def favorites(path: Path = WATCHLIST_FILE) -> list[Holding]:
    """Holdings flagged `favorite: true` in the watchlist."""
    return [h for h in load_watchlist(path) if h.favorite]


def positions(path: Path = WATCHLIST_FILE) -> list[Holding]:
    """Holdings with a non-zero share count (the real book)."""
    return [h for h in load_watchlist(path) if h.is_position]


def tickers(path: Path = WATCHLIST_FILE) -> list[str]:
    return [h.ticker for h in load_watchlist(path)]
