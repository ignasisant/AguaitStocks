"""Logos and display names — the visual identity of a ticker, brand or broker.

Every one of these is a network lookup behind a long cache: a logo that
resolves once should stay resolved for a day, and a name the user set on their
own watchlist must never render for another account (see `company_name`).
Mirroring to ./static is what keeps the hosted app from hot-linking on every
rerun; `stocks.data.logo` owns that side.
"""

from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

from stocks.config import load_watchlist
from stocks.data.logo import brand_logo_url, logo_url, mirror_brand, mirror_logo
from stocks.portfolio import platforms
from stocks.portfolio.custody import UNKNOWN as BROKER_UNKNOWN
from stocks.web import auth
from stocks.web.ds import (
    BORDER,
    FS_XS,
    RADIUS_XS,
    TEXT_MUTED,
    TEXT_PRIMARY,
)
from stocks.web.i18n import t as tr

# Streamlit static serving root: ./static next to the entry point (app.py).
_STATIC_LOGO_DIR = Path(__file__).parent / "static" / "logos"


def _static_logo_src(name: str) -> str:
    """Browser URL for a mirrored logo file — RELATIVE, no leading slash.

    Streamlit serves ./static at <base>/app/static, where <base> is wherever
    the document actually lives: "/" locally, but "/~/+/" behind Streamlit
    Cloud's shell iframe, and "/<prefix>/" under server.baseUrlPath. A
    relative URL resolves against the document URL and lands on the right
    mount in all three; an absolute "/app/static/..." escapes the Cloud
    iframe mount and 404s (this is also the form the Streamlit docs use).
    Page routes (".../portfolio") have no trailing slash, so the last
    segment drops out and "app/static/..." still resolves at the mount root.
    """
    return f"app/static/logos/{name}"


@st.cache_data(ttl=86400, show_spinner=False)
def logo(ticker: str) -> str | None:
    """Same-origin logo URL for a ticker (cached a day — logos rarely change).

    Images are mirrored into static/logos/ and served by this app, so the
    logo hosts never see per-viewer requests revealing which tickers someone
    displays. The external URL is the fallback when this host can't validate
    or download the image (logo CDNs block datacenter IPs — the browser gets
    a chance instead); None when no source knows the ticker.
    """
    if name := mirror_logo(ticker, _STATIC_LOGO_DIR):
        return _static_logo_src(name)
    return logo_url(ticker)


@st.cache_data(ttl=86400, show_spinner=False)
def brand_logo(key: str, domain: str | None) -> str | None:
    """Same-origin logo URL for a brand/platform (broker selector, …).

    Mirrored into static/logos/ like ticker logos — same privacy rationale;
    the external URL is the fallback when this host can't fetch the image.
    None when the platform declares no domain (e.g. the generic CSV)."""
    if not domain:
        return None
    if name := mirror_brand(key, domain, _STATIC_LOGO_DIR):
        return _static_logo_src(name)
    return brand_logo_url(domain)


def broker_name(key: str) -> str:
    """Display name for a ledger broker prefix ("clicktrade" -> "ClickTrade").

    Brand names come from the import registry; the two generic buckets — a
    hand-entered row and a holding no note attributes — are localized.
    """
    if key == "manual":
        return tr("portfolio.broker_manual")
    if key == BROKER_UNKNOWN:
        return tr("portfolio.broker_unknown")
    return platforms.broker_label(key)


def broker_chips_html(
    mix: list[tuple[str, float]], *, size: int = 22, shares: bool = True
) -> str:
    """Custody marks for one holding: a brand logo per broker holding it.

    `mix` is `custody.mix()`'s (broker, share of shares) list. A broker with
    no brand domain of its own (a hand-entered row, a one-off note) shows a
    muted name pill instead of a logo, so the row never renders as a gap. The
    share of the position rides each mark's tooltip — the marks sit next to a
    badge, where a second number would crowd it — and `shares=False` drops it
    for a single-custodian holding.
    """
    marks = []
    for key, share in mix:
        name = broker_name(key)
        title = f"{name} · {share:.0%}" if shares and len(mix) > 1 else name
        title = html.escape(title, quote=True)
        src = brand_logo(key, platforms.broker_domain(key))
        if src:
            marks.append(
                f'<img src="{html.escape(src, quote=True)}" alt="{title}"'
                f' title="{title}" style="width:{size}px;height:{size}px;'
                f"border-radius:{RADIUS_XS};background:{TEXT_PRIMARY};"
                f"border:1px solid {BORDER};box-sizing:border-box;"
                f'padding:2px;object-fit:contain">'
            )
        else:
            marks.append(
                f'<span title="{title}" style="font-size:{FS_XS};'
                f"font-weight:600;color:{TEXT_MUTED};border:1px solid {BORDER};"
                f"border-radius:{RADIUS_XS};padding:2px 6px;"
                f'white-space:nowrap">{html.escape(name)}</span>'
            )
    if not marks:
        return ""
    return (
        '<span style="display:inline-flex;align-items:center;gap:4px">'
        + "".join(marks)
        + "</span>"
    )


@st.cache_data(show_spinner=False)
def asset_logo(name: str) -> str | None:
    """Same-origin URL for a bundled image from web/assets/ (e.g. the TopStocks
    icon), copied into static/logos/ so it is served like the brand logos."""
    src = Path(__file__).parent / "assets" / name
    dest = _STATIC_LOGO_DIR / name
    try:
        if not dest.exists():
            _STATIC_LOGO_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
    except OSError:
        return None
    return _static_logo_src(name)


@st.cache_data(ttl=86400, show_spinner=False)
def _company_name(ticker: str, watchlist: str) -> str | None:
    for h in load_watchlist(Path(watchlist)):
        if h.ticker.upper() == ticker.upper() and h.name:
            return h.name
    # Both fallbacks hit the network on a cold cache (coin list, SEC ticker
    # map) and render pre-page.run, outside the app-level guard — a dead or
    # throttled endpoint must degrade to "no name" (callers show the symbol),
    # not crash the page. The miss isn't cached, so a rerun retries.
    try:
        from stocks.data.crypto import crypto_name

        if name := crypto_name(ticker):
            return name
        from stocks.data.funds import fund_name

        # The fund catalog is local and covers the lines a EUR investor holds;
        # the SEC map below knows US filers, so a UCITS ETF would otherwise
        # render as a bare symbol everywhere a name is shown.
        if name := fund_name(ticker):
            return name
        from stocks.data.edgar import title_for

        return title_for(ticker)
    except Exception:
        return None


def company_name(ticker: str) -> str | None:
    """Human name: the session account's watchlist name first, then the coin
    map for crypto pairs, then the SEC ticker map (offline once cached). None
    for symbols no source knows. The cache keys on the account's watchlist
    path — custom names one user sets must never render for another."""
    return _company_name(ticker, str(auth.watchlist_path()))
