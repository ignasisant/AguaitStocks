"""Central config: paths, data model, watchlist loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

# libyaml-backed loader/dumper where the wheel ships it (it does on every
# platform this deploys to) — the C parser is several times faster than the
# pure-Python one on the same bytes, and every watchlist read and Profile-editor
# write goes through these.
try:  # pragma: no cover — availability is a build detail of the wheel
    from yaml import CSafeDumper as SafeDumper
    from yaml import CSafeLoader as SafeLoader
except ImportError:  # pragma: no cover
    from yaml import SafeDumper, SafeLoader  # type: ignore[assignment]


def yaml_load(text: str) -> dict:
    """`yaml.safe_load` on the fast loader; {} for an empty document."""
    return yaml.load(text, Loader=SafeLoader) or {}


def yaml_dump(data) -> str:
    """`yaml.safe_dump` on the fast dumper, in this project's house style:
    key order preserved (these files are read by humans) and real UTF-8."""
    return yaml.dump(data, Dumper=SafeDumper, sort_keys=False,
                     allow_unicode=True)

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


def stat_key(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) signature for `path`, or None when it doesn't exist.

    The cache key for `_yaml`, and for `web.auth`'s prefs memo. Size rides
    along with the timestamp because coarse-granularity filesystems can round
    two writes inside the same second to the same mtime; a rewrite that
    changes the byte count is then still seen.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=64)
def _yaml_cached(path: Path, _key: tuple[int, int]) -> dict:
    """The parse behind `_yaml` — memoized on the caller's stat signature."""
    return yaml_load(path.read_text())


def _yaml(path: Path) -> dict:
    """Parsed YAML for `path` ({} when it doesn't exist), memoized per file.

    `resolve()` reads `aliases` out of this file on *every* yfinance lookup,
    and a parse of a 1.6 KB watchlist costs ~3.4 ms: one 30-ticker bulk
    download was spending ~100 ms re-parsing the same bytes, and a page that
    asks `market_live` per ticker paid it again. Keying the memo on the file's
    (mtime, size) rather than clearing it from every writer means the Profile
    editor's rewrites (stocks.web.auth) are picked up on the next read with no
    invalidation call to forget.

    The returned dict is the shared cached object: callers read from it and
    build their own structures (the maps below, `load_watchlist`'s Holdings);
    none of them may mutate it in place.
    """
    key = stat_key(path)
    return {} if key is None else _yaml_cached(path, key)


def _reference_raw(path: Path) -> dict:
    """Raw watchlist YAML for the global reference maps (aliases/tv).

    The default watchlist.yaml is git-ignored, so fresh checkouts and deploys
    don't have it; fall back to the tracked example there so broker-code
    resolution works before the personal file exists. Explicit paths (tests,
    per-user files) never fall back.
    """
    if not path.exists() and path == WATCHLIST_FILE:
        path = EXAMPLE_WATCHLIST_FILE
    return _yaml(path)


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
    """Parse watchlist.yaml into Holding objects. Empty list if missing.

    Reads through `_yaml`, so the eight-odd calls a single rerun makes (the
    topbar, the page, search, chat) share one parse. The Holdings themselves
    are rebuilt per call on purpose: they are mutable dataclasses carrying
    mutable `tags`/`alerts` lists, and handing every caller the same instances
    would let one page's edit surface in another's.
    """
    raw = _yaml(path)
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
