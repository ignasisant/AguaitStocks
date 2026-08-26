"""Worldwide symbol search — Yahoo's own lookup, behind the ticker pickers.

The local search tiers only know part of the universe: the watchlist, the coin
table, and the SEC ticker map (US filers only). A foreign listing therefore
matched nothing — typing "mips" or "hermes" fell straight through to the raw
"Analyze <SYMBOL>" button, which then handed yfinance a symbol Yahoo does not
quote (bare MIPS is a dead stub; the real line is MIPS.ST). This tier asks
Yahoo's search endpoint, which is name-aware, typo-tolerant and covers every
venue it quotes, so the picker offers a symbol that actually resolves.

Same failure contract as the other network-backed sources: any error means an
empty list, never an exception into the render path.
"""

from __future__ import annotations

import time
import urllib.parse

from stocks.data.http import get_json
from stocks.fuzzy import MIN_QUERY

SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# Tradable instruments only — Yahoo also returns mutual funds, futures and
# currencies for a plain word query, none of which the app can chart.
QUOTE_TYPES = {"EQUITY", "ETF"}

# This runs per keystroke behind a cache, so it must fail FAST and then stay
# quiet: Yahoo throttles shared Streamlit Cloud egress IPs, and a search field
# that blocks for 30s on every letter is worse than one that finds nothing.
TIMEOUT = 6.0
COOLDOWN = 300.0

# Shortest normalized name that may swallow a longer one as a duplicate. Below
# this, a shared prefix is coincidence ("MP" vs "MPLX"), not the same issuer.
_DEDUP_MIN = 5

_blocked_until = 0.0


def _clean(name: str) -> str:
    """Collapse Yahoo's column-padded names ("Mips AB           N")."""
    return " ".join(name.split())


def _norm(name: str) -> str:
    """Alphanumeric-only uppercase form, for cross-venue issuer matching."""
    return "".join(c for c in name.upper() if c.isalnum())


def _is_dup(keys: set[str], seen: list[str]) -> bool:
    """Whether these names belong to an issuer already listed.

    Yahoo returns every venue it quotes, and each one spells the issuer its own
    way — Mips AB comes back as "Mips AB" (Stockholm), "Mips AB N" (Frankfurt),
    "MIPS AB O.N." (Stuttgart) and "MIPS AB MIPS ORD SHS" (London). Exact-name
    dedup keeps all four and burns the whole dropdown on one company, so both
    of a row's names (short and long) are matched against both of every kept
    row's, and a prefix relation counts as the same issuer. Yahoo's first row
    wins — its ranking puts the primary listing on top, which is the line with
    the deepest history and the native currency.

    The `_DEDUP_MIN` floor keeps a coincidental short prefix from swallowing an
    unrelated company ("ASML" must not absorb "ASML Group").
    """
    for norm in keys:
        for other in seen:
            short, long = sorted((norm, other), key=len)
            if len(short) >= _DEDUP_MIN and long.startswith(short):
                return True
    return False


def search_symbols(query: str, limit: int = 6) -> list[tuple[str, str, str]]:
    """(symbol, name, exchange) matches for `query`, best first.

    Fuzzy by construction — Yahoo's own matcher takes "sandisc" to SNDK and
    "nvidai" to NVDA — so callers get typo tolerance over the whole quotable
    universe, not just the local tables. One row per issuer (see `_is_dup`).

    Empty for a query shorter than MIN_QUERY, and empty for COOLDOWN seconds
    after a failure: once Yahoo starts rejecting this host, retrying on every
    keystroke only deepens the throttle.
    """
    global _blocked_until
    q = query.strip()
    if len(q) < MIN_QUERY or time.monotonic() < _blocked_until:
        return []
    url = (
        f"{SEARCH_URL}?q={urllib.parse.quote(q)}"
        f"&quotesCount={max(limit * 3, 12)}&newsCount=0"
        "&enableFuzzyQuery=true&enableNavLinks=false"
    )
    try:
        payload = get_json(url, timeout=TIMEOUT)
    except Exception:
        _blocked_until = time.monotonic() + COOLDOWN
        return []
    out: list[tuple[str, str, str]] = []
    seen: list[str] = []
    for row in payload.get("quotes", []):
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or row.get("quoteType") not in QUOTE_TYPES:
            continue
        # longname first: it is the full legal name, spelled the same on every
        # venue ("Mips AB (publ)"), while shortname is a 30-char truncation of
        # whatever the local exchange prints ("ASML Holding N.V. - New York Re").
        short = _clean(str(row.get("shortname") or ""))
        long = _clean(str(row.get("longname") or ""))
        name = long or short or symbol
        keys = {k for k in (_norm(long), _norm(short)) if k} or {_norm(symbol)}
        if _is_dup(keys, seen):
            continue
        seen.extend(keys)
        out.append((symbol, name, str(row.get("exchDisp") or "")))
        if len(out) >= limit:
            break
    return out
