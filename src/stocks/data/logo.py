"""Resolve company logo image URLs for the dashboard.

Preference order, best-looking first:
  1. FMP  — a real transparent-PNG logo keyed straight off the *ticker*
            (the same source TIKR-style dashboards use); no key needed for
            the image path, and no yfinance round-trip to find a domain.
  2. Google favicon — low-res, but always returns *something*, keyed off the
            company website domain (yfinance lookup).

(Clearbit used to sit between the two; HubSpot shut the API down, so it was
dropped — the v3 cache bump re-resolves entries that still point there.)

Each candidate is probed once *from this host*. Only definitive answers — a
working image, or a hard 404 on every source — are cached to data/logos.json;
an inconclusive probe (403/429/timeout: FMP/Yahoo block datacenter IPs, which
is exactly what a cloud deploy runs on) is kept in memory for the process
only, and the best-guess URL is still returned so the *browser* (a
residential IP) gets a chance. The next boot re-probes. The cache is
versioned: bump CACHE_VERSION to force every ticker to re-resolve.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from stocks import obs
from stocks.config import DATA_DIR
from stocks.data.http import get_bytes_and_type, probe_image

LOGO_CACHE = DATA_DIR / "logos.json"
FMP_LOGO_URL = "https://financialmodelingprep.com/image-stock/{ticker}.png"
FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
COINCAP_URL = "https://assets.coincap.io/assets/icons/{coin}@2x.png"

# Bumping this invalidates the whole on-disk cache on next load. v3 dropped
# Clearbit (API shut down) and stopped caching probes blocked by the host.
CACHE_VERSION = 3


def domain_from_website(website: str | None) -> str | None:
    """Bare domain (no scheme, no www.) from a website URL, or None."""
    if not website:
        return None
    # urlparse needs a scheme to populate netloc; add one if missing.
    if "//" not in website:
        website = "//" + website
    host = urlparse(website).netloc.lower().removeprefix("www.")
    return host or None


# The schema stamp shares the file with the URL entries but is an int, so it
# is kept out of the in-memory dict rather than widening every value's type.
_VERSION_KEY = "_v"


def _load_cache() -> dict[str, str]:
    """On-disk cache, discarded wholesale if written by an older schema."""
    if LOGO_CACHE.exists():
        data = json.loads(LOGO_CACHE.read_text())
        if isinstance(data, dict) and data.get(_VERSION_KEY) == CACHE_VERSION:
            return {k: v for k, v in data.items() if isinstance(v, str)}
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    stamped = {_VERSION_KEY: CACHE_VERSION, **cache}
    LOGO_CACHE.write_text(json.dumps(stamped, indent=2, sort_keys=True))


# Skips 404 placeholders and dead FMP paths; "blocked" = can't tell from here.
_probe = probe_image

# Inconclusive resolutions (every remaining source blocked from this host)
# live only for the process: the browser still gets the best-guess URL, and
# the next boot re-probes instead of trusting a verdict this host couldn't
# reach. Definitive answers go to the disk cache instead.
_inconclusive: dict[str, str] = {}


def _first_alive(candidates) -> tuple[str, bool]:
    """(url, definitive) — first live candidate, walked in preference order.

    "ok" wins definitively; "dead" moves on; "blocked" keeps the first such
    candidate as the browser's best guess and, unless a later candidate
    probes "ok", marks the walk inconclusive.
    """
    guess = ""
    for url in candidates:
        verdict = _probe(url)
        if verdict == "ok":
            return url, True
        if verdict == "blocked" and not guess:
            guess = url
    return guess, not guess


def _company_domain(ticker: str) -> str | None:
    """Website domain via yfinance, or None — never raises: Yahoo throttles
    shared cloud IPs hard, and a logo lookup must not take the page down."""
    try:
        from stocks.data.fetch import info as quote_info

        website = quote_info(ticker).get("website")
    except Exception:
        return None
    return domain_from_website(website)


def _candidates(ticker: str):
    """Candidate URLs, best first, yielded lazily so the yfinance domain
    lookup only runs when the FMP probe didn't already settle it."""
    from stocks.data.crypto import split_pair

    # Crypto pairs: coin icon sources — a company-domain lookup can't work.
    if pair := split_pair(ticker):
        coin = pair[0]
        yield COINCAP_URL.format(coin=coin.lower())
        yield FMP_LOGO_URL.format(ticker=f"{coin}USD")  # FMP keys crypto as BTCUSD
        return
    yield FMP_LOGO_URL.format(ticker=ticker)
    if domain := _company_domain(ticker):
        yield FAVICON_URL.format(domain=domain)


def _resolve(key: str, candidates) -> str | None:
    """Cache-then-probe walk shared by logo_url / brand_logo_url."""
    cache = _load_cache()
    if key in cache:
        return cache[key] or None
    if key in _inconclusive:
        return _inconclusive[key]
    url, definitive = _first_alive(candidates)
    if definitive:
        cache[key] = url
        _save_cache(cache)
    else:
        _inconclusive[key] = url
    return url or None


def logo_url(ticker: str) -> str | None:
    """Working logo image URL for a ticker, or None.

    FMP ticker logo first (best quality, no domain lookup), then the Google
    favicon off the yfinance website domain. Definitive results are cached
    to disk ("" means "nothing exists"); blocked probes only in memory.
    """
    ticker = ticker.upper()
    return _resolve(ticker, _candidates(ticker))


def brand_logo_url(domain: str) -> str | None:
    """Working logo URL for a brand *domain* (broker/platform, no ticker).

    Google favicon — Clearbit, the old quality source, was shut down. Cached
    under a `brand:` prefix so brand keys can never collide with tickers.
    """
    return _resolve(f"brand:{domain.lower()}", [FAVICON_URL.format(domain=domain)])


_EXT_BY_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


def _restore_dir_quiet(static_dir: Path) -> None:
    """First touch per process pulls previously mirrored logos back from the
    storage bucket (ephemeral hosts boot with an empty static dir). Never
    fatal: a failed pull only means logos re-resolve over the network."""
    with obs.swallow("logo.restore_dir"):
        from stocks import storage

        storage.restore_dir(static_dir)


def _persist_quiet(path: Path) -> None:
    """Push one mirrored logo to the bucket. Unlike user data, a lost logo
    re-mirrors itself, so a storage hiccup must never break a render."""
    with obs.swallow("logo.mirror", path=str(path)):
        from stocks import storage

        storage.persist(path)


def _mirror(stem: str, resolve_url, static_dir: Path) -> str | None:
    """Mirror one image into `static_dir` as `stem.<ext>`; returns the file
    name. An already-mirrored file (local, or pulled back from the storage
    bucket on an ephemeral host's first touch) short-circuits before any
    network call; `resolve_url` is only invoked on a cache miss."""
    _restore_dir_quiet(static_dir)
    if static_dir.is_dir():
        for existing in static_dir.glob(f"{stem}.*"):
            return existing.name
    url = resolve_url()
    if not url:
        return None
    try:
        data, ctype = get_bytes_and_type(url)
    except Exception:
        return None  # hiccup / blocked host — caller falls back to external URL
    ctype = ctype.partition(";")[0].strip().lower()
    if not ctype.startswith("image"):
        return None  # a CDN challenge page with a 200 — don't store HTML as a .png
    ext = _EXT_BY_TYPE.get(ctype, "png")
    static_dir.mkdir(parents=True, exist_ok=True)
    out = static_dir / f"{stem}.{ext}"
    out.write_bytes(data)
    _persist_quiet(out)
    return out.name


def mirror_logo(ticker: str, static_dir: Path) -> str | None:
    """Mirror a ticker's logo into `static_dir`; returns the file name.

    The dashboard serves logos same-origin (Streamlit static serving) so the
    logo hosts (FMP, Clearbit, Google) never learn which tickers a viewer
    looks at — only the server fetches each image, once per ticker. Returns
    e.g. "AAPL.png", or None when no source resolved or the download failed.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", ticker.upper())
    return _mirror(safe, lambda: logo_url(ticker), static_dir)


def mirror_brand(key: str, domain: str, static_dir: Path) -> str | None:
    """Mirror a brand-domain logo as `brand-<key>.<ext>`; same contract as
    mirror_logo. The `brand-` stem keeps platform keys clear of tickers."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key.lower())
    return _mirror(f"brand-{safe}", lambda: brand_logo_url(domain), static_dir)
