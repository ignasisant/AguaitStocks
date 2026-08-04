"""FX via frankfurter.dev (ECB reference rates, no key).

Spot rates for live sizing; historical rates for cost-basis / tax reporting.
Spanish tax law converts every foreign-currency transaction to EUR at the ECB
rate published for the transaction date, so historical lookups are cached and
resolve weekends/holidays to the prior business day (frankfurter's behaviour).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import date as _date
from datetime import timedelta

from stocks.config import DATA_DIR
from stocks.data.http import get_json

LATEST_URL = "https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}"
HISTORICAL_URL = "https://api.frankfurter.dev/v1/{date}?base={base}&symbols={quote}"
RANGE_URL = "https://api.frankfurter.dev/v1/{start}..{end}?base={base}&symbols={quote}"
FX_CACHE = DATA_DIR / "fx_history.json"

# (amount, currency, iso_date) -> EUR. The injectable-converter signature used
# by positions / dividends / portfolio so tests run without network.
ToEur = Callable[[float, str, str], float]


# Spot rates memoized in-process: sizing a book calls spot() once per
# position, and ECB reference rates update once a day anyway.
_SPOT_TTL_S = 900.0
_SPOT_CACHE: dict[tuple[str, str], tuple[float, tuple[float, str]]] = {}


def spot(base: str, quote: str) -> tuple[float, str]:
    """(rate, as_of_date) for one currency pair, e.g. spot('USD', 'EUR').

    Memoized for 15 minutes, so repeated per-position lookups cost one HTTP
    request per pair, not one per call.
    """
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return 1.0, "spot"
    hit = _SPOT_CACHE.get((base, quote))
    if hit and time.monotonic() - hit[0] < _SPOT_TTL_S:
        return hit[1]
    url = LATEST_URL.format(base=base, quote=quote)
    result = _fetch(url, quote)
    _SPOT_CACHE[(base, quote)] = (time.monotonic(), result)
    return result


def usd_eur() -> tuple[float, str]:
    return spot("USD", "EUR")


def _fetch(url: str, quote: str) -> tuple[float, str]:
    data = get_json(url, timeout=15)
    return float(data["rates"][quote]), data["date"]


# Historical rates never change, so the disk cache is loaded once per process
# and kept in memory; ledger replays then cost zero file IO per lookup.
_MEM_CACHE: dict[str, float] | None = None


def _cache() -> dict[str, float]:
    global _MEM_CACHE
    if _MEM_CACHE is None:
        _MEM_CACHE = _load_cache()
    return _MEM_CACHE


def _load_cache() -> dict[str, float]:
    if not FX_CACHE.exists():
        return {}
    try:
        return json.loads(FX_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, float]) -> None:
    FX_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True))


def rate_on(day: str | _date, base: str, quote: str) -> float:
    """ECB rate for `base`->`quote` on a given date (ISO 'YYYY-MM-DD').

    Weekends/holidays resolve to the prior published business day. Results are
    cached in data/fx_history.json keyed by the *requested* date, so historical
    rates (which never change) are fetched at most once.
    """
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return 1.0
    day = day.isoformat() if isinstance(day, _date) else day
    key = f"{day}:{base}:{quote}"
    cache = _cache()
    if key in cache:
        return cache[key]
    url = HISTORICAL_URL.format(date=day, base=base, quote=quote)
    rate, _ = _fetch(url, quote)
    cache[key] = rate
    _save_cache(cache)
    return rate


def rates_range(start: str, end: str, base: str, quote: str) -> dict[str, float]:
    """Daily ECB rates base->quote over [start, end], one request.

    Returns {'YYYY-MM-DD': rate} for ECB business days only — callers
    forward-fill weekends/holidays. Empty dict when base == quote.
    """
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return {}
    url = RANGE_URL.format(start=start, end=end, base=base, quote=quote)
    data = get_json(url, timeout=30)
    return {d: float(v[quote]) for d, v in data["rates"].items()}


def prefetch(pairs: Iterable[tuple[str, str]], quote: str = "EUR") -> None:
    """Warm the historical-rate cache for (iso_date, currency) pairs.

    One range request per currency instead of one request per date, so a full
    ledger replay costs a single HTTP call per non-EUR currency. Best-effort:
    on any fetch failure the affected dates simply fall back to per-date
    `rate_on` lookups.
    """
    quote = quote.upper()
    wanted: dict[str, set[str]] = {}
    for day, ccy in pairs:
        ccy = (ccy or "").upper()
        if not ccy or ccy == quote:
            continue
        day = day.isoformat() if isinstance(day, _date) else str(day)[:10]
        wanted.setdefault(ccy, set()).add(day)

    cache = _cache()
    dirty = False
    for ccy, days in wanted.items():
        missing = sorted(d for d in days if f"{d}:{ccy}:{quote}" not in cache)
        if not missing:
            continue
        # Pad the start so weekend/holiday dates can resolve to the prior
        # published business day inside the fetched window.
        pad = (_date.fromisoformat(missing[0]) - timedelta(days=7)).isoformat()
        try:
            rates = rates_range(pad, missing[-1], ccy, quote)
        except Exception:
            continue
        business_days = sorted(rates)
        for d in missing:
            prior = [bd for bd in business_days if bd <= d]
            if prior:
                cache[f"{d}:{ccy}:{quote}"] = rates[prior[-1]]
                dirty = True
    if dirty:
        _save_cache(cache)


def to_eur(amount: float, currency: str, day: str | _date) -> float:
    """Convert `amount` of `currency` to EUR at the ECB rate for `day`."""
    if currency.upper() == "EUR":
        return amount
    return amount * rate_on(day, currency, "EUR")
