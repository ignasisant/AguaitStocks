"""Related tickers via Yahoo's "people also watch" endpoint.

Powers the quick-pick peer suggestions in the Comparables section: one GET,
no API key. Symbols come back Yahoo-native (post-alias), which is what every
downstream fetch expects anyway.
"""

from __future__ import annotations

from stocks.data.fetch import resolve
from stocks.data.http import get_json

_URL = "https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{symbol}?count={count}"


def parse_related(payload: dict) -> list[str]:
    """Recommended symbols out of the endpoint's response envelope."""
    try:
        results = payload["finance"]["result"] or []
        recs = results[0].get("recommendedSymbols") or []
    except (KeyError, IndexError, TypeError):
        return []
    return [r["symbol"] for r in recs if r.get("symbol")]


def related_tickers(ticker: str, count: int = 8) -> list[str]:
    """Up to `count` related Yahoo symbols; empty list on any failure."""
    symbol = resolve(ticker)
    try:
        payload = get_json(_URL.format(symbol=symbol, count=count))
    except Exception:
        return []
    return [s for s in parse_related(payload) if s.upper() != symbol.upper()][:count]
