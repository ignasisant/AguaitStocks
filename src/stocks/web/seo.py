"""Search and social metadata for the static landing pages.

Everything here exists because the landing is served as a real HTML document
(see `landing_static` and `server`) rather than rendered inside the Streamlit
app: a Streamlit page owns exactly one `<head>` for the whole app, which no
script run can add a description, a canonical URL or Open Graph tags to. A
plain Starlette response can.

Three things are worth knowing about the choices below.

* **Two indexable URLs, not one with a parameter.** English lives at `/`,
  Spanish at `/es/`, and each declares the other with `hreflang` (plus an
  `x-default` pointing at English). Serving both languages from `/` off the
  Accept-Language header would leave one of them with no address to rank.
* **The app is deliberately not indexed.** `/`, `/es/` and the assets are the
  only public documents; every app route is a JavaScript shell over somebody's
  positions, so `robots_txt()` disallows them and `server` stamps an
  `X-Robots-Tag: noindex` header on them. `/` is both the landing and the app
  entry (a query parameter or the returning-visitor cookie switches it), and
  crawlers send neither, so allowing `/` cannot expose the app.
* **The absolute base URL comes from the request.** This app is deployed at
  whatever hostname it happens to get (a `*.run.app` URL today), and canonical
  / Open Graph tags must be absolute. `server.base_url()` derives it per
  request from the forwarded headers, so nothing here hard-codes a host.
"""

from __future__ import annotations

import html
import json
from functools import lru_cache
from pathlib import Path

from stocks.web import landing
from stocks.web.i18n import DEFAULT_LANG, LANGUAGES, translate
from stocks.web.landing import (
    ASSET_BASE,
    FAQ_COUNT,
    LANDING_PATHS,
    PATH_EN,
    jur_key,
)

SITE_NAME = "TopStocks"
REPO_URL = "https://github.com/ignasi-sant/stocks"

# The share card. 1200x630 is the size Open Graph, Twitter/X, LinkedIn,
# WhatsApp and Slack all render at 1.91:1 without re-cropping.
OG_IMAGE = f"{ASSET_BASE}og.png"
OG_IMAGE_W = 1200
OG_IMAGE_H = 630
OG_IMAGE_TYPE = "image/png"

ICON_SVG = f"{ASSET_BASE}topstocks-icon.svg"
APPLE_ICON = f"{ASSET_BASE}apple-touch-icon.png"

# Page background from .streamlit/config.toml — paints the browser chrome on
# mobile and the loading canvas before the CSS lands.
THEME_COLOR = "#18161C"

# The landing's faces. config.toml loads these for the app; a static document
# is outside that, so it links them itself. One stylesheet, one round trip:
# Instrument Sans (body), Epilogue (display figures), Martian Mono (tickers).
FONTS_HREF = (
    "https://fonts.googleapis.com/css2"
    "?family=Instrument+Sans:wght@400;500;600;700"
    "&family=Epilogue:wght@600;700;800"
    "&family=Martian+Mono:wght@400;500"
    "&display=swap"
)

# hreflang pairs the *same* content in two languages, so the set is per
# jurisdiction: the English and Spanish Spain-tax pages are translations of
# each other, while the English Spain-tax and English US-tax pages are two
# different arguments and must not be declared as alternates. x-default is
# where an unmatched locale lands, which is the source language of that pair.
def _alternates(jurisdiction: str) -> dict[str, str]:
    en, es = (
        landing.variant_path("en", jurisdiction),
        landing.variant_path("es", jurisdiction),
    )
    return {"en": en, "es": es, "x-default": en}

# Open Graph wants a locale, not a language code.
_OG_LOCALE = {"en": "en_US", "es": "es_ES"}


_PAGES_DIR = Path(__file__).parent / "app_pages"

# Streamlit's own endpoints, and the two auth routes it installs. Transport and
# machinery, never content — disallowed in robots.txt, and accepted by the
# not-found gate in `server` so a real internal request is never turned away.
APP_PREFIXES = (
    "/_stcore/",
    "/media/",
    "/component/",
    "/static/",
    "/app/static/",
    "/auth/",
)

# Exact paths Streamlit answers on: the OIDC return, and the three files its
# frontend build serves from the root.
APP_PATHS = ("/oauth2callback", "/favicon.png", "/index.html", "/manifest.json")


@lru_cache(maxsize=1)
def app_page_paths() -> tuple[str, ...]:
    """`/<name>` for every page the nav can serve.

    `st.navigation` derives a page's URL from its module filename, so the
    directory is the source of truth. Deriving it here means a page added to
    `app_pages/` is disallowed in robots.txt and accepted by the not-found gate
    at the same time, with no second list to forget.
    """
    return tuple(
        sorted(f"/{f.stem}" for f in _PAGES_DIR.glob("*.py")
               if not f.stem.startswith("_"))
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def path_for(lang: str, jurisdiction: str | None = None) -> str:
    """The landing path serving `lang` (under `jurisdiction`, or its default)."""
    return landing.variant_path(lang, jurisdiction)


def _abs(base_url: str, path: str) -> str:
    """Absolute URL for a root-relative path."""
    return f"{base_url.rstrip('/')}{path}"


def _meta(name: str, content: str) -> str:
    return f'<meta name="{name}" content="{_esc(content)}">'


def _prop(prop: str, content: str) -> str:
    return f'<meta property="{prop}" content="{_esc(content)}">'


def json_ld(lang: str, base_url: str, jurisdiction: str | None = None) -> str:
    """The page's structured data, as one `<script type="application/ld+json">`.

    A single `@graph` rather than three separate blocks — Google reads them the
    same way and one node can then reference another by `@id` (the app is
    `publisher`-less on purpose: this is one person's open-source project, not
    an organisation, and claiming otherwise in structured data is a way to lose
    a rich result).

    `FAQPage` is built from the same catalog keys the visible FAQ renders, so
    the markup never describes questions the page does not show. It is here for
    the consumers that still read it — Bing, and the crawlers that feed
    assistants — not for a Google rich result: those were restricted to
    government and health sites in 2023 and this page will not get one.
    """
    jur = jurisdiction or landing.active_jurisdiction_for(lang)
    page = _abs(base_url, path_for(lang, jur))
    graph = [
        {
            "@type": "WebSite",
            "@id": f"{page}#website",
            "url": page,
            "name": SITE_NAME,
            "inLanguage": lang,
            "description": translate(jur_key("landing.seo_description", jur), lang),
        },
        {
            "@type": "SoftwareApplication",
            "@id": f"{page}#app",
            "name": SITE_NAME,
            "url": page,
            "applicationCategory": "FinanceApplication",
            "operatingSystem": "Web browser",
            "inLanguage": sorted(LANGUAGES),
            "description": translate(jur_key("landing.seo_description", jur), lang),
            "image": _abs(base_url, OG_IMAGE),
            "license": "https://opensource.org/licenses/MIT",
            "isAccessibleForFree": True,
            "codeRepository": REPO_URL,
            # Free, and priced explicitly: an Offer with price 0 is what turns
            # "is it free?" into something a search result can state.
            "offers": {
                "@type": "Offer",
                "price": 0,
                "priceCurrency": "EUR",
            },
        },
        {
            "@type": "FAQPage",
            "@id": f"{page}#faq",
            "inLanguage": lang,
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": translate(jur_key(f"landing.faq_q{i}", jur), lang),
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": translate(jur_key(f"landing.faq_a{i}", jur), lang),
                    },
                }
                for i in range(1, FAQ_COUNT + 1)
            ],
        },
    ]
    body = json.dumps(
        {"@context": "https://schema.org", "@graph": graph},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # A literal "</script>" inside JSON would end the element early; escaping
    # the angle bracket is valid JSON and valid JSON-LD.
    body = body.replace("<", "\\u003c")
    return f'<script type="application/ld+json">{body}</script>'


def _font_links() -> str:
    """The webfont stylesheet, loaded without blocking the first paint.

    A plain `<link rel="stylesheet">` to a second origin sits directly on the
    LCP path: nothing paints until that request resolves, and `preconnect` only
    shortens the handshake, it does not remove the block. Loading it as
    `media="print"` makes it non-render-blocking, and the onload flip applies it
    the moment it lands; `preload` keeps it at the front of the queue so that is
    early. The families already carry `display=swap`, so text paints in the
    fallback face immediately and swaps in place.

    The `<noscript>` copy is the blocking version, for the (rare, and
    crawler-shaped) client that runs no JavaScript.
    """
    href = _esc(FONTS_HREF)
    return (
        f'<link rel="preload" as="style" href="{href}">'
        f'<link rel="stylesheet" href="{href}" media="print" '
        'onload="this.media=\'all\';this.onload=null">'
        f'<noscript><link rel="stylesheet" href="{href}"></noscript>'
    )


def head(
    lang: str,
    base_url: str,
    *,
    extra_styles: str = "",
    jurisdiction: str | None = None,
) -> str:
    """Everything between `<head>` and `</head>` for the landing in `lang`.

    `extra_styles` is appended last (the design tokens and the page stylesheet),
    so the caller keeps control of CSS order while the metadata stays here.
    `jurisdiction` defaults to the render context's, which is how the title,
    description and FAQ rich result end up arguing the same country as the page.
    """
    lang = lang if lang in LANGUAGES else DEFAULT_LANG
    jur = jurisdiction or landing.active_jurisdiction_for(lang)
    title = translate(jur_key("landing.seo_title", jur), lang)
    description = translate(jur_key("landing.seo_description", jur), lang)
    image_alt = translate(jur_key("landing.seo_image_alt", jur), lang)
    canonical = _abs(base_url, path_for(lang, jur))
    image = _abs(base_url, OG_IMAGE)

    alternates = "".join(
        f'<link rel="alternate" hreflang="{code}" '
        f'href="{_esc(_abs(base_url, path))}">'
        for code, path in _alternates(jur).items()
    )
    other_locales = "".join(
        _prop("og:locale:alternate", _OG_LOCALE[code])
        for code in sorted(LANGUAGES)
        if code != lang
    )

    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        + _meta("description", description)
        + f'<link rel="canonical" href="{_esc(canonical)}">'
        + alternates
        # max-image-preview:large is what allows the share card to be used as
        # the thumbnail in search results, not just in social unfurls.
        + _meta("robots", "index, follow, max-image-preview:large")
        + _meta("theme-color", THEME_COLOR)
        + _meta("color-scheme", "dark")
        + _meta("author", SITE_NAME)
        + f'<link rel="icon" href="{_esc(ICON_SVG)}" type="image/svg+xml">'
        + f'<link rel="apple-touch-icon" href="{_esc(APPLE_ICON)}">'
        + _prop("og:type", "website")
        + _prop("og:site_name", SITE_NAME)
        + _prop("og:locale", _OG_LOCALE.get(lang, "en_US"))
        + other_locales
        + _prop("og:title", title)
        + _prop("og:description", description)
        + _prop("og:url", canonical)
        + _prop("og:image", image)
        + _prop("og:image:type", OG_IMAGE_TYPE)
        + _prop("og:image:width", OG_IMAGE_W)
        + _prop("og:image:height", OG_IMAGE_H)
        + _prop("og:image:alt", image_alt)
        + _meta("twitter:card", "summary_large_image")
        + _meta("twitter:title", title)
        + _meta("twitter:description", description)
        + _meta("twitter:image", image)
        + _meta("twitter:image:alt", image_alt)
        # Fonts come from a second origin, so the handshake starts now rather
        # than when the stylesheet is parsed.
        + '<link rel="preconnect" href="https://fonts.googleapis.com">'
        + '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        + _font_links()
        + json_ld(lang, base_url, jur)
        + extra_styles
    )


# Crawlers that collect text to train or ground models rather than to send
# readers back. Blocking them is a request, not a control — robots.txt is
# voluntary and only the well-behaved ones honour it — but the well-behaved
# ones are most of the volume, and the landing copy is the only thing here
# they could take (the app is disallowed below and noindex besides).
AI_CRAWLERS = (
    "GPTBot",             # OpenAI, training
    "OAI-SearchBot",      # OpenAI, search index
    "ChatGPT-User",       # OpenAI, user-initiated fetch
    "ClaudeBot",          # Anthropic, training
    "Claude-User",        # Anthropic, user-initiated fetch
    "Claude-SearchBot",   # Anthropic, search index
    "anthropic-ai",       # Anthropic, legacy token
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",    # Gemini training; does NOT affect Google Search
    "Applebot-Extended",  # Apple Intelligence training; Applebot still indexes
    "Bytespider",
    "CCBot",              # Common Crawl, the corpus most others are built from
    "Meta-ExternalAgent",
    "Amazonbot",
    "cohere-ai",
    "Diffbot",
    "Omgilibot",
)


def robots_txt(base_url: str) -> str:
    """`/robots.txt` — the two landing pages are public, the app is not.

    App routes are listed explicitly rather than blanket-disallowed: `/` has to
    stay crawlable (it is the landing for anyone without the app cookie), so
    "Disallow: /" is not available. Streamlit's own machinery gets the same
    treatment — those URLs are transport, never content.

    AI crawlers get their own blanket block first. Order matters: a robots.txt
    parser applies the *most specific* matching group, so a named agent reads
    its own group and never the `*` one — which is why the disallow list below
    has to be repeated there rather than inherited.
    """
    lines = []
    for agent in AI_CRAWLERS:
        lines += [f"User-agent: {agent}", "Disallow: /", ""]
    lines += ["User-agent: *", "Allow: /$"]
    lines += [f"Allow: {p}" for p in LANDING_PATHS if p != PATH_EN]
    lines += [f"Allow: {ASSET_BASE}"]
    # The OIDC return is the only exact path worth naming; Streamlit's root
    # files (favicon, manifest) are left crawlable on purpose — they are not
    # content, they carry `noindex` anyway, and blocking a favicon is how a
    # search result loses its icon.
    disallow = app_page_paths() + ("/oauth2callback",) + APP_PREFIXES
    lines += [f"Disallow: {p}" for p in disallow]
    lines += ["", f"Sitemap: {_abs(base_url, '/sitemap.xml')}", ""]
    return "\n".join(lines)


def sitemap_xml(base_url: str, *, lastmod: str | None = None) -> str:
    """`/sitemap.xml` — every landing variant, cross-linked within its pair.

    Each URL carries its jurisdiction's full `xhtml:link` alternate set
    (including itself, as the spec requires), which is the machine-readable
    half of the hreflang pairing in the head. Priority ranks the two default
    pairs above the cross ones — same content, less likely to be what a
    searcher meant.
    """
    mod = f"<lastmod>{_esc(lastmod)}</lastmod>" if lastmod else ""
    entries = []
    for (code, jur), loc_path in landing.VARIANTS.items():
        loc = _abs(base_url, loc_path)
        links = "".join(
            f'<xhtml:link rel="alternate" hreflang="{alt}" '
            f'href="{_esc(_abs(base_url, path))}"/>'
            for alt, path in _alternates(jur).items()
        )
        if jur != landing.jurisdiction_for(code):
            priority = "0.7"
        else:
            priority = "1.0" if code == DEFAULT_LANG else "0.9"
        entries.append(
            f"<url><loc>{_esc(loc)}</loc>{mod}"
            f"<changefreq>weekly</changefreq>"
            f"<priority>{priority}</priority>"
            f"{links}</url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        + "".join(entries)
        + "</urlset>"
    )
