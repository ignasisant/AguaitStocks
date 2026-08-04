"""Resolve company logo image URLs for the dashboard.

Preference order, best-looking first:
  1. FMP  — a real transparent-PNG logo keyed straight off the *ticker*
            (the same source TIKR-style dashboards use); no key needed for
            the image path, and no yfinance round-trip to find a domain.
  2. Clearbit — transparent PNG keyed off the company website domain.
  3. Google favicon — low-res, but always returns *something*.

Each resolved URL is validated once and cached to data/logos.json so we don't
re-hit the network on every render. The cache is versioned: bump CACHE_VERSION
to force every ticker to re-resolve (e.g. after adding a better source), so
old low-res favicon entries upgrade themselves instead of sticking around.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import yfinance as yf

from stocks.config import DATA_DIR
from stocks.data.http import url_is_image

LOGO_CACHE = DATA_DIR / "logos.json"
FMP_LOGO_URL = "https://financialmodelingprep.com/image-stock/{ticker}.png"
CLEARBIT_URL = "https://logo.clearbit.com/{domain}"
FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
COINCAP_URL = "https://assets.coincap.io/assets/icons/{coin}@2x.png"

# Bumping this invalidates the whole on-disk cache on next load. v2 added the
# FMP ticker logo as the preferred source over the old favicon-only entries.
CACHE_VERSION = 2


def domain_from_website(website: str | None) -> str | None:
    """Bare domain (no scheme, no www.) from a website URL, or None."""
    if not website:
        return None
    # urlparse needs a scheme to populate netloc; add one if missing.
    if "//" not in website:
        website = "//" + website
    host = urlparse(website).netloc.lower().removeprefix("www.")
    return host or None


def _load_cache() -> dict[str, str]:
    """On-disk cache, discarded wholesale if written by an older schema."""
    if LOGO_CACHE.exists():
        data = json.loads(LOGO_CACHE.read_text())
        if isinstance(data, dict) and data.get("_v") == CACHE_VERSION:
            return data
    return {"_v": CACHE_VERSION}


def _save_cache(cache: dict[str, str]) -> None:
    cache["_v"] = CACHE_VERSION
    LOGO_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


# Skips Clearbit 404 placeholders and dead FMP paths.
_url_ok = url_is_image


def logo_url(ticker: str) -> str | None:
    """Working logo image URL for a ticker, or None. Result cached to disk.

    Tries the FMP ticker logo first (best quality, no domain lookup), then
    Clearbit off the website domain, then the Google favicon. The cache stores
    the resolved URL ("" means "nothing worked").
    """
    ticker = ticker.upper()
    cache = _load_cache()
    if ticker in cache:
        return cache[ticker] or None

    # Crypto pairs: coin icon sources — a company-domain lookup can't work.
    from stocks.data.crypto import split_pair

    if pair := split_pair(ticker):
        coin = pair[0]
        resolved = ""
        for candidate in (
            COINCAP_URL.format(coin=coin.lower()),
            FMP_LOGO_URL.format(ticker=f"{coin}USD"),  # FMP keys crypto as BTCUSD
        ):
            if _url_ok(candidate):
                resolved = candidate
                break
        cache[ticker] = resolved
        _save_cache(cache)
        return resolved or None

    # 1. FMP: keyed straight off the ticker, so no yfinance call to find a domain.
    fmp = FMP_LOGO_URL.format(ticker=ticker)
    resolved = fmp if _url_ok(fmp) else ""

    # 2/3. Fall back to the company domain (Clearbit, then favicon).
    if not resolved:
        website = (yf.Ticker(ticker).info or {}).get("website")
        domain = domain_from_website(website)
        if domain:
            clearbit = CLEARBIT_URL.format(domain=domain)
            resolved = clearbit if _url_ok(clearbit) else FAVICON_URL.format(domain=domain)

    cache[ticker] = resolved
    _save_cache(cache)
    return resolved or None
