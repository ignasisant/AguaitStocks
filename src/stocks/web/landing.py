"""Marketing landing page — the first thing an anonymous visitor sees.

Ported from the Claude Design canvas file `Aguait Landing.dc.html`. This module
owns only the *markup*: `landing_static` wraps these sections in a standalone
HTML document (head, SEO tags, JSON-LD) that `server.py` serves at `/` and
`/es/` straight from Starlette — no Streamlit script run, no websocket, no
`st.html`. That is what makes the page crawlable and instant; the app itself
starts at the first CTA click.

`consume_params()` is the other half: the CTAs are links carrying query
parameters, and `app.py` calls it once per rerun to act on them.

Two things shape how this is written:

* **The CTAs are links, not buttons.** A designed landing puts its calls to
  action inside flex rows, sticky bars and centred hero blocks; a static page
  has no widget layer to put a button in anyway. Every CTA is an anchor
  carrying a query parameter, and any query parameter on `/` is what tells
  `server.py` to hand the request to Streamlit instead of the landing —
  `?signin=1` then makes `consume_params()` call `st.login()`, `?guest=1`
  drops the visitor straight into the app as a guest.
* **Phones get a fixed CTA bar, not a squeezed header.** The page is very
  long, so the sign-in call to action leaves the top bar on narrow viewports
  and reappears as a fixed bottom bar (`.ag-l-mbar`) that follows the reader
  down. Everything else is width-driven CSS — see `_MOBILE_RULES`.
* **No raw colour literals.** Everything reads `var(--ag-*)` from
  `widgets.ds_vars_css()`, which `landing_static` inlines into the document
  head. Tints the tokens don't ship are derived with `color-mix()` rather than
  hard-coded rgba, so the palette stays single-source.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import streamlit as st

from stocks.portfolio import tax
from stocks.web import auth
from stocks.web.i18n import DEFAULT_LANG, LANGUAGES, has, translate
from stocks.web.markup import esc

# Query parameters the in-page anchors set. Read by consume_params().
PARAM_SIGNIN = "signin"
PARAM_GUEST = "guest"

# Browser paths this module hard-codes: the language pair the sections link
# between, and the asset mount server.py serves the brand mark from. Absolute,
# because /es/ is one segment deeper than / and a relative logo URL would
# resolve against the wrong directory there.
PATH_EN = "/"
PATH_ES = "/es/"
# Language and tax residence are independent: an English-reading filer in Spain
# and a Spanish-reading filer in the US both exist, and the pitch is a
# country's case, so each combination is its own indexable page rather than a
# query parameter (any query parameter on `/` is what hands the request to the
# app). The two defaults keep their short URLs; the cross pairs are explicit.
PATH_EN_ES = "/en-es/"  # English copy, Spanish rules
PATH_ES_US = "/es-us/"  # Spanish copy, US rules
ASSET_BASE = "/lp/"

# The language this render is for. A ContextVar rather than an argument because
# every section below reads it through tr() — threading a code through fifteen
# builders to reach a dict lookup would be all noise. Set by render_language().
_LANG: ContextVar[str] = ContextVar("landing_lang", default=DEFAULT_LANG)

# The tax jurisdiction this render argues from. The page makes a *country's*
# case — the euro basis and the two-month rule, or the USD basis and the wash
# sale — so it follows the reader's language rather than a separate control:
# an anonymous visitor has no profile to read a tax residence from, and a
# fourth landing variant per language pair would be four indexed pages arguing
# past each other. The language toggle switches both, and `render_language`
# takes an explicit override for the day that stops being true.
JURISDICTION_BY_LANG = {"en": "US", "es": "ES"}
# Unset rather than defaulting to one country: outside a render (seo.py called
# directly, a test) the right jurisdiction is the language's own, not Spain's.
_JUR: ContextVar[str | None] = ContextVar("landing_jur", default=None)

# (language, jurisdiction) -> the one URL that serves it. Two pages became four
# when the copy split by country; the pairs a language defaults to keep the
# short paths so nothing already indexed moves.
VARIANTS: dict[tuple[str, str], str] = {
    ("en", "US"): PATH_EN,
    ("es", "ES"): PATH_ES,
    ("en", "ES"): PATH_EN_ES,
    ("es", "US"): PATH_ES_US,
}
LANDING_PATHS: tuple[str, ...] = tuple(VARIANTS.values())


def jurisdiction_for(lang: str) -> str:
    return tax.normalize(JURISDICTION_BY_LANG.get(lang, tax.DEFAULT_CODE))


def variant_path(lang: str, jurisdiction: str | None = None) -> str:
    """The URL serving `lang` under `jurisdiction` (its default if unset)."""
    code = lang if lang in LANGUAGES else DEFAULT_LANG
    jur = tax.normalize(jurisdiction or jurisdiction_for(code))
    return VARIANTS.get((code, jur), PATH_EN)


def variant_for(path: str) -> tuple[str, str] | None:
    """(language, jurisdiction) a landing path serves, or None if it isn't one.

    Accepts the path with or without its trailing slash, so the redirect that
    canonicalizes `/es` can ask about `/es` directly.
    """
    wanted = path if path.endswith("/") else f"{path}/"
    for (lang, jur), p in VARIANTS.items():
        if p == wanted or p.rstrip("/") == path.rstrip("/"):
            return lang, jur
    return None


@contextmanager
def render_language(lang: str, jurisdiction: str | None = None):
    """Render the sections in `lang`. Wraps every landing_static build."""
    code = lang if lang in LANGUAGES else DEFAULT_LANG
    lang_token = _LANG.set(code)
    jur_token = _JUR.set(tax.normalize(jurisdiction or jurisdiction_for(code)))
    try:
        yield
    finally:
        _JUR.reset(jur_token)
        _LANG.reset(lang_token)


def active_language() -> str:
    """The language this render is for — never Streamlit session state.

    The page is built outside a script run (there is no session), so it cannot
    go through `i18n.active_language()`.
    """
    return _LANG.get()


def active_jurisdiction() -> str:
    """The tax jurisdiction this render argues from ("ES", "US")."""
    return active_jurisdiction_for(active_language())


def active_jurisdiction_for(lang: str) -> str:
    """The render's jurisdiction, or the one `lang` defaults to outside one.

    seo.py takes its language as an argument rather than from the context (the
    cron and the tests call it directly), so it has to be able to ask "which
    country goes with *this* language" without a render in progress.
    """
    return _JUR.get() or jurisdiction_for(lang)


def jur_key(key: str, jurisdiction: str | None = None) -> str:
    """`landing.us_hero_title` when that copy exists, else `landing.hero_title`.

    Most of the page is country-neutral, so only the keys a jurisdiction
    actually disputes need an override — the rest fall through to one string
    per language. seo.py resolves through here too, so the title, description
    and FAQ rich result argue the same case as the page.
    """
    prefix = "landing."
    if not key.startswith(prefix):
        return key
    jur = jurisdiction or active_jurisdiction()
    scoped = f"{prefix}{jur.lower()}_{key[len(prefix):]}"
    return scoped if has(scoped) else key


def tr(key: str, /, **kwargs) -> str:
    """`i18n.t()` bound to the render language and jurisdiction."""
    return translate(jur_key(key), _LANG.get(), **kwargs)

# Illustrative figures for the product mocks. Plausible, obviously a sample —
# never presented as anyone's real book.
_BENCHMARK = "SPY"
_FISCAL_YEAR = 2026
_CHART_YEARS = ("2023", "2024", "2025", "2026")

_GITHUB_URL = "https://github.com/ignasi-sant/stocks"

# Q/A pairs in the FAQ section (landing.faq_q1..q8 / faq_a1..a8). `seo.py`
# reads the same range to emit the FAQPage structured data, so the rich result
# and the visible section can never drift apart.
FAQ_COUNT = 8

# Brokers the Import page actually parses (stocks.portfolio.platforms), in the
# order that page lists them. The last entry is the catch-all schema.
_BROKERS = (
    ("Revolut", "CSV · PDF", False),
    ("Revolut Crypto", "CSV", False),
    ("Trading 212", "CSV", False),
    ("DEGIRO", "CSV", False),
    ("Interactive Brokers", "CSV", False),
    ("ClickTrade / Saxo", "XLSX · CSV", False),
    (None, None, True),  # generic CSV — label comes from the catalog
)


# ------------------------------------------------------------------ numbers
# The mocks are the loudest part of the page, so they follow the reader's own
# conventions: "€48,230" and "+47.7%" in English, "48.230 €" and "+47,7 %" in
# Spanish. Kept local and tiny — the app's real formatters work off live frames
# and a display-currency preference neither of which exists on a landing page.


def _is_es() -> bool:
    return active_language() == "es"


def _group(digits: str) -> str:
    sep = "." if _is_es() else ","
    out, count = [], 0
    for ch in reversed(digits):
        if count and count % 3 == 0:
            out.append(sep)
        out.append(ch)
        count += 1
    return "".join(reversed(out))


def _symbol() -> str:
    """The currency the mocks are denominated in, per jurisdiction."""
    return "$" if tax.get(active_jurisdiction()).currency == "USD" else "€"


def _money(amount: int, *, signed: bool = False) -> str:
    sign = ""
    if signed:
        sign = "−" if amount < 0 else "+"
    body = _group(str(abs(amount)))
    sym = _symbol()
    return f"{sign}{body} {sym}" if _is_es() else f"{sign}{sym}{body}"


def _plain(amount: int, *, signed: bool = False) -> str:
    """A bare grouped figure — for table cells that already have a € header."""
    sign = ("−" if amount < 0 else "+") if signed else ""
    return f"{sign}{_group(str(abs(amount)))}"


def _pct(value: float, *, signed: bool = True) -> str:
    sign = ("−" if value < 0 else "+") if signed else ""
    body = f"{abs(value):.1f}"
    if _is_es():
        return f"{sign}{body.replace('.', ',')} %"
    return f"{sign}{body}%"


def _dec(value: float, places: int) -> str:
    body = f"{value:.{places}f}"
    return body.replace(".", ",") if _is_es() else body


def _rows(n: int) -> str:
    return tr("landing.row_n" if n == 1 else "landing.rows_n", n=n)


# ------------------------------------------------------------------- assets

_G_MARK = (
    '<svg class="ag-l-g" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M21.35 11.1H12v3.9h5.4c-.5 2.5-2.7 3.9-5.4 3.9a6 6 0 1 1 0-12'
    "c1.5 0 2.9.55 3.95 1.45l2.9-2.9A9.9 9.9 0 0 0 12 2a10 10 0 1 0 0 20"
    'c5.75 0 9.55-4.05 9.55-9.75 0-.4-.05-.8-.2-1.15z"></path></svg>'
)

_GITHUB_MARK = (
    '<svg class="ag-l-gh" viewBox="0 0 24 24" aria-hidden="true">'
    '<path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79'
    "-.25.79-.55v-1.94c-3.2.7-3.87-1.54-3.87-1.54-.52-1.33-1.28-1.68-1.28-1.68"
    "-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.19 1.76 1.19 1.03 1.76 2.69 1.25 3.35"
    ".96.1-.75.4-1.25.72-1.54-2.55-.29-5.24-1.28-5.24-5.68 0-1.26.45-2.28 1.19"
    "-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0"
    "c2.2-1.49 3.16-1.18 3.16-1.18.63 1.59.24 2.76.12 3.05.74.81 1.19 1.83 1.19"
    " 3.09 0 4.41-2.7 5.38-5.26 5.66.41.36.78 1.06.78 2.14v3.17c0 .3.2.67.8.55"
    'A11.51 11.51 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z"></path></svg>'
)

_WARN_MARK = (
    '<svg class="ag-l-warnicon" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 '
    '2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" '
    'x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>'
    "</svg>"
)


# ---------------------------------------------------------------- stylesheet
# Plain (non-f) string: it is full of CSS braces, and every value it needs is
# already a custom property. Tints the token set doesn't ship are derived with
# color-mix() off the same tokens rather than hard-coded rgba.
#
# NOTE: never introduce a raw "less-than" character anywhere in this block,
# comments included — DOMPurify drops the entire style element when it sees one.
_BASE_CSS = """
/* --- suppress the app chrome; the landing owns the whole viewport --- */
section[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
header[data-testid="stHeader"],
.topstocks-topbar { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container {
  padding: 0 !important; max-width: 100% !important;
}
[data-testid="stMain"] { background: var(--ag-surface-page); }

/* --- page --- */
.ag-l {
  background: var(--ag-surface-page);
  color: var(--ag-text-primary);
  font-family: 'Instrument Sans', sans-serif;
  font-variant-numeric: lining-nums;
  line-height: 1.5;
}
.ag-l * { box-sizing: border-box; }
.ag-l a { color: var(--ag-brand-accent); text-decoration: none; }
.ag-l a:hover { color: var(--ag-purple-400); }
.ag-l h1, .ag-l h2, .ag-l h3 {
  font-family: 'Epilogue', sans-serif; margin: 0; letter-spacing: -0.015em;
}
.ag-l p { margin: 0; }
.ag-l-wrap { max-width: 1140px; margin: 0 auto; padding: 0 24px; }
.ag-l-num { font-family: 'Epilogue', sans-serif; font-weight: 700; }
.ag-l-mono { font-family: 'Martian Mono', monospace; }
.ag-l-up { color: var(--ag-landing-up); }
.ag-l-down { color: var(--ag-landing-down); }
.ag-l-band {
  background: var(--ag-surface-band);
  border-top: 1px solid var(--ag-surface-card);
}
.ag-l-rule { border-top: 1px solid var(--ag-surface-card); }

/* --- top bar --- */
.ag-l-bar {
  position: sticky; top: 0; z-index: 50;
  background: color-mix(in srgb, var(--ag-surface-page) 92%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--ag-border);
}
.ag-l-bar-in { height: 60px; display: flex; align-items: center; gap: 20px; }
.ag-l-brand { display: flex; align-items: center; gap: 10px; }
.ag-l-brand img { width: 28px; height: auto; }
.ag-l-brand span {
  font-family: 'Epilogue', sans-serif; font-weight: 700;
  font-size: var(--ag-fs-xl); letter-spacing: -0.01em;
}
.ag-l-spacer { flex: 1; }
.ag-l-lang {
  display: flex; align-items: center;
  border: 1px solid var(--ag-border); border-radius: var(--ag-radius-sm);
  overflow: hidden;
}
.ag-l-lang a, .ag-l-lang span {
  padding: 5px 10px; font-size: var(--ag-fs-sm); font-weight: 500;
  color: var(--ag-text-muted); transition: all 50ms ease-in-out;
}
.ag-l-lang .on {
  font-weight: 600; background: var(--ag-surface-card);
  color: var(--ag-text-primary);
}
.ag-l-lang a:hover { color: var(--ag-text-primary); }
.ag-l-gh { width: 16px; height: 16px; fill: currentColor; }
.ag-l-ghlink {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--ag-fs-md); font-weight: 500; color: var(--ag-text-secondary);
}
.ag-l-ghlink:hover { color: var(--ag-text-primary); }

/* --- keyboard focus --- */
/* The default UA ring is near-invisible on this dark surface; keyboard users
   get an explicit high-contrast ring instead. :focus-visible only — mouse
   clicks stay ringless, as the UA heuristic intends. */
a:focus-visible, button:focus-visible, summary:focus-visible {
  outline: 2px solid var(--ag-brand-cta); outline-offset: 2px;
}

/* --- buttons --- */
.ag-l-g { width: 17px; height: 17px; fill: var(--ag-brand-google-tile); }
.ag-l-cta {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: var(--ag-fs-lg); font-weight: 600;
  color: var(--ag-on-brand); background: var(--ag-brand-cta);
  border: none; border-radius: var(--ag-radius-sm); padding: 13px 22px;
  cursor: pointer; transition: all 50ms ease-in-out;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--ag-brand-cta) 25%, transparent);
}
.ag-l-cta:hover { background: var(--ag-purple-700); color: var(--ag-on-brand); }
.ag-l-cta-ghost {
  display: inline-flex; align-items: center;
  font-size: var(--ag-fs-md); font-weight: 600; color: var(--ag-text-primary);
  background: transparent; border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-sm); padding: 8px 14px;
  transition: all 50ms ease-in-out; white-space: nowrap;
}
.ag-l-cta-ghost:hover {
  border-color: var(--ag-text-faint); color: var(--ag-text-primary);
}
.ag-l-cta-text {
  font-size: var(--ag-fs-lg); font-weight: 600;
  color: var(--ag-brand-accent); padding: 13px 8px;
}
.ag-l-ctarow {
  display: flex; gap: 12px; flex-wrap: wrap; align-items: center;
}
/* The fixed phone CTA bar. Hidden here, revealed by _MOBILE_RULES. */
.ag-l-mbar { display: none; }

/* --- hero --- */
/* Every auto-fit grid here writes its minimum as min(Npx, 100%). A bare
   minmax(Npx, 1fr) track does not shrink below Npx, so in a container narrower
   than that — a phone, or the main area with the sidebar open — the row
   overflows to the right and Streamlit's main container clips it rather than
   scrolling. min(Npx, 100%) caps the minimum at the container itself. */
.ag-l-hero {
  padding-top: 72px; padding-bottom: 56px;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(400px, 100%), 1fr));
  gap: 48px; align-items: center;
}
.ag-l-hero-copy { display: flex; flex-direction: column; gap: 20px; }
.ag-l-hero h1 {
  font-weight: 800; font-size: 46px; line-height: 1.08;
  letter-spacing: -0.02em; text-wrap: pretty;
}
.ag-l-lede {
  font-size: 17px; line-height: 1.55; color: var(--ag-text-secondary);
  max-width: 52ch; text-wrap: pretty;
}
.ag-l-trust {
  font-size: 12.5px; color: var(--ag-text-muted); line-height: 1.7;
}
.ag-l-card {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-lg); padding: 20px;
  display: flex; flex-direction: column; gap: 16px;
  box-shadow: 0 24px 60px var(--ag-shadow-color-strong);
}
.ag-l-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.ag-l-kpi { display: flex; flex-direction: column; gap: 3px; }
.ag-l-kpi-l {
  font-size: 10.5px; font-weight: 500; color: var(--ag-text-muted);
  letter-spacing: 0.04em; text-transform: uppercase;
}
.ag-l-kpi-v { font-size: 19px; }
.ag-l-tbl {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--ag-border);
}
.ag-l-tr {
  display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 8px;
  padding: 7px 4px; font-size: var(--ag-fs-md);
  border-top: 1px solid var(--ag-surface-card);
}
.ag-l-th {
  display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 8px;
  padding: 8px 4px; font-size: 10.5px; font-weight: 500;
  color: var(--ag-text-faint); letter-spacing: 0.04em; text-transform: uppercase;
}
.ag-l-tr span + span, .ag-l-th span + span { text-align: right; }
.ag-l-tk { font-weight: 600; }
.ag-l-dim { color: var(--ag-text-secondary); }
.ag-l-chart {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px solid var(--ag-border);
  padding-top: 14px;
}
.ag-l-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: var(--ag-fs-xs);
  color: var(--ag-text-muted);
}
.ag-l-legend span { display: flex; align-items: center; gap: 5px; }
.ag-l-sw { width: 14px; height: 2px; border-radius: 1px; }
.ag-l-sw-twr { background: var(--ag-brand-accent); }
.ag-l-sw-bm { background: var(--ag-landing-info); opacity: 0.7; }
.ag-l-chart svg { width: 100%; height: auto; display: block; }
.ag-l-axis {
  display: flex;
  justify-content: space-between;
  font-size: 9px;
  color: var(--ag-text-faint);
}

/* --- broker strip --- */
.ag-l-brokers { padding-bottom: 64px; display: flex; flex-direction: column; gap: 16px; }
.ag-l-brokers-line {
  font-size: var(--ag-fs-md);
  font-weight: 500;
  color: var(--ag-text-muted);
  text-align: center;
}
.ag-l-brokergrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(130px, 100%), 1fr));
  gap: 10px;
}
.ag-l-broker {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-md); padding: 14px 10px;
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  text-align: center;
}
.ag-l-broker.generic { border-style: dashed; }
.ag-l-broker b { font-family: 'Epilogue', sans-serif; font-weight: 700; font-size: 14px; }
.ag-l-broker.generic b { color: var(--ag-text-secondary); }
.ag-l-broker i { font-style: normal; font-size: 9px; color: var(--ag-text-muted); }

/* --- section headings --- */
.ag-l-sec {
  padding-top: 72px;
  padding-bottom: 72px;
  display: flex;
  flex-direction: column;
  gap: 32px;
}
.ag-l-sec h2 { font-weight: 800; font-size: var(--ag-fs-3xl); }
.ag-l-head { display: flex; flex-direction: column; gap: 10px; max-width: 60ch; }
.ag-l-head p { font-size: var(--ag-fs-lg); color: var(--ag-text-secondary); }

/* --- the gap --- */
.ag-l-gaps { display: flex; flex-direction: column; gap: 14px; }
.ag-l-gap {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-md); padding: 20px 22px;
}
.ag-l-gap div { display: flex; flex-direction: column; gap: 6px; }
.ag-l-gap b { font-size: var(--ag-fs-lg); font-weight: 600; }
.ag-l-gap p {
  font-size: var(--ag-fs-base);
  line-height: 1.55;
  color: var(--ag-text-secondary);
}
.ag-l-diagrams {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
  gap: 14px;
}
.ag-l-diagram {
  background: var(--ag-surface-page); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-md); padding: 20px;
  display: flex; flex-direction: column; gap: 14px;
}
.ag-l-eyebrow-sm { font-size: 10px; color: var(--ag-text-muted); letter-spacing: 0.06em; }
.ag-l-note { font-size: 12.5px; color: var(--ag-text-muted); }
.ag-l-lots { display: flex; gap: 8px; align-items: stretch; }
.ag-l-lot {
  border-radius: var(--ag-radius-xs); padding: 8px;
  display: flex; flex-direction: column; gap: 2px;
}
.ag-l-lot.sold {
  background: color-mix(in srgb, var(--ag-brand-cta) 18%, transparent);
  border: 1px solid var(--ag-brand-cta);
}
.ag-l-lot.held {
  background: var(--ag-surface-raised);
  border: 1px solid var(--ag-border);
}
.ag-l-lot b { font-size: var(--ag-fs-sm); font-weight: 600; }
.ag-l-lot.sold b { color: var(--ag-purple-400); }
.ag-l-lot.held b { color: var(--ag-text-muted); }
.ag-l-lot i { font-style: normal; font-size: 10px; }
.ag-l-lot.sold i { color: var(--ag-brand-accent); }
.ag-l-lot.held i { color: var(--ag-text-faint); }
.ag-l-lotpair {
  display: flex; border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-xs); overflow: hidden;
}
.ag-l-lotpair .ag-l-lot { border: none; border-radius: 0; }
.ag-l-lotpair .sold { border-right: 1px dashed var(--ag-brand-cta); }
.ag-l-tl { position: relative; height: 64px; }
.ag-l-tl-rail {
  position: absolute; left: 0; right: 0; top: 30px; height: 2px;
  background: var(--ag-border); border-radius: 1px;
}
.ag-l-tl-tick {
  position: absolute;
  top: 22px;
  width: 2px;
  height: 18px;
  background: var(--ag-brand-accent);
}
.ag-l-tl-tick.faint { background: var(--ag-text-faint); }
.ag-l-tl-lab {
  position: absolute; top: 0; transform: translateX(-50%); text-align: center;
  font-size: 9.5px; color: var(--ag-brand-accent); line-height: 1.35;
}
.ag-l-tl-lab.faint { color: var(--ag-text-faint); }
.ag-l-compare { display: flex; gap: 10px; flex-wrap: wrap; }
.ag-l-compare div {
  flex: 1; min-width: 140px; border-radius: var(--ag-radius-sm);
  padding: 10px 12px; display: flex; flex-direction: column; gap: 2px;
}
.ag-l-compare .broker {
  background: var(--ag-surface-raised);
  border: 1px solid var(--ag-border);
}
.ag-l-compare .ours {
  background: color-mix(in srgb, var(--ag-brand-cta) 12%, transparent);
  border: 1px solid var(--ag-brand-cta);
}
.ag-l-compare small { font-size: var(--ag-fs-xs); color: var(--ag-text-muted); }
.ag-l-compare .ours small { color: var(--ag-purple-400); }
.ag-l-compare b { font-size: 17px; }
.ag-l-compare .broker b { color: var(--ag-text-secondary); }

/* --- how it works --- */
.ag-l-steps {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  gap: 16px;
}
.ag-l-step {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-lg); padding: 24px;
  display: flex; flex-direction: column; gap: 16px;
}
.ag-l-step h3 {
  font-weight: 800; font-size: var(--ag-fs-lg); color: var(--ag-brand-accent);
}
.ag-l-step p {
  font-size: var(--ag-fs-base);
  line-height: 1.55;
  color: var(--ag-text-secondary);
}
.ag-l-gbtn {
  display: flex; align-items: center; justify-content: center; padding: 22px 0;
}
.ag-l-gbtn span {
  display: flex; align-items: center; gap: 10px;
  background: var(--ag-brand-google-tile); color: var(--ag-surface-card);
  font-size: var(--ag-fs-base); font-weight: 600;
  border-radius: var(--ag-radius-sm); padding: 11px 20px;
}
.ag-l-gbtn .ag-l-g { fill: var(--ag-info-deep); }
.ag-l-buckets { display: flex; flex-direction: column; gap: 6px; }
.ag-l-bucket {
  display: flex; justify-content: space-between; align-items: center;
  border-radius: 7px; padding: 7px 12px; font-size: 12.5px;
}
.ag-l-bucket b { font-weight: 600; }
.ag-l-bucket i { font-style: normal; font-size: var(--ag-fs-xs); }
.ag-l-bucket.ok {
  background: color-mix(in srgb, var(--ag-landing-up) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-up) 35%, transparent);
  color: var(--ag-landing-up);
}
.ag-l-bucket.warn {
  background: color-mix(in srgb, var(--ag-landing-warn) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-warn) 35%, transparent);
  color: var(--ag-landing-warn);
}
.ag-l-bucket.bad {
  background: color-mix(in srgb, var(--ag-landing-down) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-down) 35%, transparent);
  color: var(--ag-landing-down);
}
.ag-l-bucket.skip {
  background: var(--ag-surface-page); border: 1px solid var(--ag-border);
  color: var(--ag-text-muted);
}
.ag-l-mini {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-sm);
  overflow: hidden;
}
.ag-l-mini-h {
  display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 6px;
  padding: 7px 12px; font-size: 10px; font-weight: 500;
  color: var(--ag-text-faint); text-transform: uppercase;
  letter-spacing: 0.04em; background: var(--ag-surface-page);
}
.ag-l-mini-r {
  display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 6px;
  padding: 6px 12px; font-size: 12.5px; border-top: 1px solid var(--ag-surface-card);
}
.ag-l-mini-h span + span, .ag-l-mini-r span + span { text-align: right; }

/* --- provenance band --- */
.ag-l-prov {
  background: var(--ag-surface-brand-band);
  border-top: 1px solid var(--ag-border-brand-band);
  border-bottom: 1px solid var(--ag-border-brand-band);
}
.ag-l-prov-in {
  padding-top: 64px;
  padding-bottom: 64px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.ag-l-prov h2 { font-weight: 800; font-size: var(--ag-fs-3xl); max-width: 24ch; }
.ag-l-provrow { display: flex; gap: 12px; flex-wrap: wrap; }
.ag-l-provcard {
  background: var(--ag-surface-page); border: 1px solid var(--ag-border);
  border-radius: 10px; padding: 14px 18px;
  display: flex; flex-direction: column; gap: 6px; min-width: 150px;
}
.ag-l-provcard-h { display: flex; align-items: center; gap: 8px; }
.ag-l-provcard-h span { font-size: var(--ag-fs-xs); color: var(--ag-text-muted); }
.ag-l-provcard b { font-size: 20px; }
.ag-l-provcard small { font-size: 10.5px; color: var(--ag-text-faint); }
.ag-l-tag {
  font-family: 'Martian Mono', monospace; font-size: 8.5px; font-weight: 500;
  border-radius: var(--ag-radius-xs); padding: 2px 6px; letter-spacing: 0.05em;
}
.ag-l-tag.fact {
  color: var(--ag-landing-up);
  background: color-mix(in srgb, var(--ag-landing-up) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-up) 35%, transparent);
}
.ag-l-tag.consensus {
  color: var(--ag-landing-info);
  background: color-mix(in srgb, var(--ag-landing-info) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-info) 35%, transparent);
}
.ag-l-tag.derived {
  color: var(--ag-purple-400);
  background: color-mix(in srgb, var(--ag-brand-accent) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-brand-accent) 40%, transparent);
}
.ag-l-na { color: var(--ag-text-faint); }
.ag-l-prov p {
  font-size: var(--ag-fs-lg); line-height: 1.6; color: var(--ag-text-secondary);
  max-width: 78ch; text-wrap: pretty;
}
.ag-l-prov p b { font-weight: 600; }
.ag-l-prov p b.fact { color: var(--ag-landing-up); }
.ag-l-prov p b.consensus { color: var(--ag-landing-info); }
.ag-l-prov p b.derived { color: var(--ag-purple-400); }

/* --- feature bento --- */
.ag-l-bento-lg {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr));
  gap: 16px;
}
.ag-l-bento-sm {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
  gap: 16px;
}
.ag-l-tile {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-lg); display: flex; flex-direction: column;
}
.ag-l-bento-lg .ag-l-tile { padding: 26px; gap: 12px; }
.ag-l-bento-sm .ag-l-tile { padding: 22px; gap: 8px; }
.ag-l-bento-lg h3 { font-weight: 700; font-size: 19px; }
.ag-l-bento-sm h3 { font-weight: 700; font-size: var(--ag-fs-lg); }
.ag-l-bento-lg p {
  font-size: var(--ag-fs-base);
  line-height: 1.6;
  color: var(--ag-text-secondary);
  text-wrap: pretty;
}
.ag-l-bento-sm p {
  font-size: 13.5px;
  line-height: 1.55;
  color: var(--ag-text-secondary);
  text-wrap: pretty;
}
.ag-l-pills { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
.ag-l-pill {
  font-family: 'Martian Mono', monospace; font-size: 10px;
  color: var(--ag-brand-accent);
  background: color-mix(in srgb, var(--ag-brand-cta) 12%, transparent);
  border-radius: var(--ag-radius-xs); padding: 5px 9px;
}

/* --- depth blocks --- */
.ag-l-depth {
  padding-top: 72px; padding-bottom: 72px;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr));
  gap: 48px; align-items: center;
}
.ag-l-depth-copy { display: flex; flex-direction: column; gap: 14px; }
.ag-l-eyebrow {
  font-family: 'Martian Mono', monospace; font-size: var(--ag-fs-xs);
  color: var(--ag-brand-accent); letter-spacing: 0.1em;
}
.ag-l-depth h2 { font-weight: 800; font-size: 30px; text-wrap: pretty; }
.ag-l-depth-copy p {
  font-size: var(--ag-fs-lg);
  line-height: 1.6;
  color: var(--ag-text-secondary);
  text-wrap: pretty;
}
.ag-l-flat {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: var(--ag-radius-lg); padding: 24px;
  display: flex; flex-direction: column; gap: 18px;
}
.ag-l-flat.sunken { background: var(--ag-surface-page); gap: 14px; }
.ag-l-legs { display: flex; flex-direction: column; gap: 10px; }
.ag-l-leg { display: flex; align-items: center; gap: 12px; font-size: var(--ag-fs-xs); }
.ag-l-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ag-brand-accent);
}
.ag-l-leg span { color: var(--ag-text-secondary); flex: 1; }
/* The dot is a span too, so the rule above claimed it: flex-basis 0 plus grow
   stretched the 8px dot into a 185px bar. Needs the extra specificity —
   source order does not decide this one. */
.ag-l-leg .ag-l-dot { flex: none; }
.ag-l-leg b { color: var(--ag-brand-accent); font-weight: 400; }
.ag-l-legjoin {
  margin-left: 3.5px;
  border-left: 1px dashed var(--ag-border);
  height: 14px;
}
.ag-l-split {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  border-top: 1px solid var(--ag-border);
  padding-top: 16px;
}
.ag-l-split div {
  flex: 1;
  min-width: 150px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ag-l-split small { font-size: var(--ag-fs-xs); color: var(--ag-text-muted); }
.ag-l-split .ours small { color: var(--ag-purple-400); }
.ag-l-split b { font-size: 20px; }
.ag-l-split .broker b { color: var(--ag-text-secondary); }
.ag-l-irpf { display: flex; flex-direction: column; }
.ag-l-irpf-r {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  padding: 9px 0; border-bottom: 1px solid var(--ag-surface-card);
  font-size: 13.5px;
}
.ag-l-irpf-r:last-child { border-bottom: none; }
.ag-l-irpf-r span { color: var(--ag-text-secondary); }
.ag-l-irpf-r.total span { color: var(--ag-text-primary); font-weight: 600; }
.ag-l-irpf-r b { font-family: 'Epilogue', sans-serif; font-weight: 600; }
.ag-l-irpf-r.total b { font-weight: 700; }
.ag-l-warnrow span {
  color: var(--ag-landing-warn); display: flex; align-items: center; gap: 7px;
}
.ag-l-warnicon { width: 13px; height: 13px; flex-shrink: 0; }
.ag-l-struck { color: var(--ag-text-muted); text-decoration: line-through; }
.ag-l-disclaimer {
  background: color-mix(in srgb, var(--ag-landing-warn) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--ag-landing-warn) 30%, transparent);
  border-radius: 10px; padding: 12px 16px;
  font-size: var(--ag-fs-md); line-height: 1.55; color: var(--ag-text-secondary);
}
/* The "filing in the other country?" link under the tax panel — the reader
   who needs it is the one the page is arguing the wrong rules at. */
.ag-l-jurswitch {
  align-self: start; font-size: var(--ag-fs-sm); font-weight: 600;
  color: var(--ag-landing-info); text-decoration: none;
  border-bottom: 1px solid currentColor;
}
.ag-l-jurswitch:hover { color: var(--ag-text-primary); }

/* --- trust --- */
.ag-l-trustgrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
  gap: 16px;
}
.ag-l-trustitem {
  display: flex; flex-direction: column; gap: 6px;
  border-top: 2px solid var(--ag-brand-cta); padding-top: 14px;
}
.ag-l-trustitem b { font-size: var(--ag-fs-lg); font-weight: 600; }
.ag-l-trustitem span {
  font-size: var(--ag-fs-base);
  line-height: 1.55;
  color: var(--ag-text-secondary);
}

/* --- FAQ --- */
.ag-l-faq {
  max-width: 800px;
  margin: 0 auto;
  padding: 72px 24px;
  display: flex;
  flex-direction: column;
  gap: 28px;
}
.ag-l-faq h2 { font-weight: 800; font-size: var(--ag-fs-2xl); }
.ag-l-qs { display: flex; flex-direction: column; gap: 8px; }
.ag-l-q {
  background: var(--ag-surface-raised); border: 1px solid var(--ag-border);
  border-radius: 10px; padding: 14px 18px;
}
.ag-l-q summary { font-size: var(--ag-fs-lg); font-weight: 600; cursor: pointer; }
.ag-l-q p {
  margin: 10px 0 2px;
  font-size: var(--ag-fs-base);
  line-height: 1.55;
  color: var(--ag-text-secondary);
}

/* --- final CTA + footer --- */
.ag-l-final {
  padding-top: 88px; padding-bottom: 88px;
  display: flex; flex-direction: column; gap: 24px;
  align-items: center; text-align: center;
}
.ag-l-final h2 {
  font-weight: 800;
  font-size: 38px;
  letter-spacing: -0.02em;
  max-width: 24ch;
  text-wrap: pretty;
}
.ag-l-foot {
  padding-top: 32px;
  padding-bottom: 40px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.ag-l-foot-row { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.ag-l-foot-row img { width: 22px; height: auto; }
.ag-l-foot-row .ag-l-brand span { font-size: var(--ag-fs-lg); }
.ag-l-foot a { font-size: var(--ag-fs-md); color: var(--ag-text-muted); }
.ag-l-foot a:hover { color: var(--ag-text-secondary); }
.ag-l-foot-lang {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--ag-fs-md);
  color: var(--ag-text-muted);
}
.ag-l-foot-lang .on { font-weight: 600; color: var(--ag-text-secondary); }
.ag-l-fine {
  font-size: var(--ag-fs-sm); line-height: 1.6; color: var(--ag-text-faint);
  max-width: 90ch;
}

/* --- tablet / narrow desktop: the two-column blocks stack --- */
/* The wide layouts are auto-fit grids, which collapse on their own — but they
   collapse at whatever width their minmax() implies, and .ag-l-depth is built
   panel-first in section B. Pin the single column at one breakpoint so the
   stacking width and the reading order are both explicit. */
@media (max-width: 900px) {
  .ag-l-hero {
    grid-template-columns: 1fr;
    padding-top: 56px; padding-bottom: 44px; gap: 32px;
  }
  .ag-l-hero h1 { font-size: 38px; }
  .ag-l-depth {
    grid-template-columns: 1fr;
    padding-top: 56px; padding-bottom: 56px; gap: 32px;
  }
  .ag-l-depth .ag-l-depth-copy { order: -1; }  /* heading above its mock */
  .ag-l-sec { padding-top: 56px; padding-bottom: 56px; }
  .ag-l-prov-in { padding-top: 52px; padding-bottom: 52px; }
  .ag-l-diagrams, .ag-l-bento-lg { grid-template-columns: 1fr; }
  .ag-l-final { padding-top: 64px; padding-bottom: 64px; }
  .ag-l-final h2 { font-size: 32px; }
}
"""


# --------------------------------------------------------------- mobile rules
# Phone layout. Kept as bare rules rather than a media block because it is
# applied two ways (see _mobile_css()): by viewport width, and again by the
# User-Agent check for phones whose CSS viewport is wider than the breakpoint.
# Same DOMPurify rule as above — no "less-than" character anywhere in here.
#
# What actually changes on a ~360px screen, beyond narrower gutters:
#   * the header CTA moves to a fixed bottom bar, so it is one thumb-tap away
#     from anywhere on a very long page;
#   * the positions mock drops its € value column — four numeric columns wrap
#     inside their own cells at this width, and the P/L pair is the point;
#   * the FIFO lot strip stacks, and the FX timeline stops being a timeline
#     (its absolutely-placed labels overlap and overrun the right edge);
#   * every two-up flex pair (compare, split) becomes one column.
_MOBILE_RULES = """
.ag-l { padding-bottom: 84px; }  /* clears the fixed CTA bar */
.ag-l-wrap { padding: 0 16px; }
.ag-l h1, .ag-l h2, .ag-l h3 { overflow-wrap: break-word; }

/* --- top bar: brand and language only --- */
.ag-l-bar-in { height: 54px; gap: 12px; }
.ag-l-brand img { width: 24px; }
.ag-l-brand span { font-size: var(--ag-fs-lg); }
.ag-l-ghlink, .ag-l-bar .ag-l-cta-ghost { display: none; }
.ag-l-lang a, .ag-l-lang span { padding: 8px 12px; }  /* 36px tap target */

/* --- fixed CTA bar --- */
.ag-l-mbar {
  display: block; position: fixed; left: 0; right: 0; bottom: 0; z-index: 60;
  padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px));
  background: color-mix(in srgb, var(--ag-surface-page) 94%, transparent);
  backdrop-filter: blur(10px);
  border-top: 1px solid var(--ag-border);
}
.ag-l-mbar .ag-l-cta {
  width: 100%; justify-content: center; box-shadow: none; padding: 12px 18px;
}
/* Out of the way while a real CTA is on screen — the hero's and the closing
   one are both full-width buttons with the same label, so an unconditional
   bar would sit directly under its own twin at the top and the bottom of the
   page. The class is JS-applied (_BAR_JS); with no JS the bar just stays up. */
.ag-l.cta-visible .ag-l-mbar {
  transform: translateY(105%); pointer-events: none;
}
.ag-l-mbar { transition: transform 160ms ease-out; }
@media (prefers-reduced-motion: reduce) {
  .ag-l-mbar { transition: none; }
}

/* --- hero --- */
.ag-l-hero { padding-top: 32px; padding-bottom: 32px; gap: 28px; }
.ag-l-hero h1 { font-size: 31px; }
.ag-l-lede { font-size: 15.5px; }
.ag-l-ctarow { flex-direction: column; align-items: stretch; gap: 6px; }
.ag-l-ctarow .ag-l-cta { width: 100%; justify-content: center; }
.ag-l-cta-text { text-align: center; padding: 10px 8px; }
.ag-l-trust { font-size: 12px; }
.ag-l-card {
  padding: 16px; gap: 14px;
  box-shadow: 0 12px 30px var(--ag-shadow-color-strong);
}
.ag-l-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 10px; }
.ag-l-kpi-v { font-size: 17px; }
.ag-l-th, .ag-l-tr { grid-template-columns: 1.2fr 1fr 0.9fr; }
.ag-l-th span:nth-child(2), .ag-l-tr span:nth-child(2) { display: none; }
.ag-l-tr { font-size: 12.5px; }

/* --- broker strip --- */
.ag-l-brokers { padding-bottom: 44px; }
.ag-l-brokergrid { grid-template-columns: repeat(2, minmax(0, 1fr)); }

/* --- sections --- */
.ag-l-sec { padding-top: 44px; padding-bottom: 44px; gap: 24px; }
.ag-l-sec h2 { font-size: var(--ag-fs-2xl); }
.ag-l-head p { font-size: var(--ag-fs-base); }
.ag-l-gap { grid-template-columns: 1fr; padding: 16px 18px; gap: 12px; }
.ag-l-diagrams, .ag-l-steps, .ag-l-bento-lg, .ag-l-bento-sm,
.ag-l-trustgrid { grid-template-columns: 1fr; }
.ag-l-step { padding: 20px; }
.ag-l-diagram { padding: 16px; }

/* --- FIFO lots stack --- */
.ag-l-lots { flex-direction: column; }
.ag-l-lots > * { flex: none !important; }  /* beats the inline flex weights */

/* --- FX timeline becomes a list --- */
.ag-l-tl { height: auto; display: flex; flex-direction: column; gap: 8px; }
.ag-l-tl-rail, .ag-l-tl-tick { display: none; }
.ag-l-tl-lab {
  position: static; transform: none; text-align: left; font-size: 10.5px;
  padding-left: 10px; border-left: 2px solid var(--ag-brand-accent);
}
.ag-l-tl-lab.faint { border-left-color: var(--ag-text-faint); }

/* --- two-up pairs --- */
.ag-l-compare, .ag-l-split { flex-direction: column; }
.ag-l-compare div, .ag-l-split div { min-width: 0; }

/* --- provenance --- */
.ag-l-prov-in { padding-top: 44px; padding-bottom: 44px; gap: 20px; }
.ag-l-prov h2 { font-size: var(--ag-fs-2xl); }
.ag-l-prov p { font-size: var(--ag-fs-base); }
.ag-l-provrow { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ag-l-provcard { min-width: 0; padding: 12px 14px; }
.ag-l-provcard-h { flex-wrap: wrap; }  /* the tag drops below a long label */
.ag-l-provcard b { font-size: 17px; }

/* --- depth blocks --- */
.ag-l-depth { padding-top: 44px; padding-bottom: 44px; gap: 24px; }
.ag-l-depth h2 { font-size: 25px; }
.ag-l-depth-copy p { font-size: var(--ag-fs-base); }
.ag-l-flat { padding: 16px; gap: 14px; }
.ag-l-leg { gap: 8px; flex-wrap: wrap; }
.ag-l-leg b { width: 100%; padding-left: 20px; }  /* rate drops to its own line */
.ag-l-irpf-r { font-size: 12.5px; }

/* --- bento --- */
.ag-l-bento-lg .ag-l-tile { padding: 20px; }
.ag-l-bento-sm .ag-l-tile { padding: 18px; }
.ag-l-bento-lg h3 { font-size: 17px; }

/* --- FAQ --- */
.ag-l-faq { padding: 44px 16px; gap: 20px; }
.ag-l-faq h2 { font-size: var(--ag-fs-2xl); }
.ag-l-q { padding: 12px 14px; }
.ag-l-q summary { font-size: var(--ag-fs-base); }

/* --- final CTA and footer --- */
.ag-l-final { padding-top: 56px; padding-bottom: 56px; gap: 18px; }
.ag-l-final h2 { font-size: 27px; }
.ag-l-final .ag-l-ctarow { width: 100%; }
.ag-l-foot { padding-top: 24px; padding-bottom: 28px; gap: 14px; }
.ag-l-foot-row { gap: 14px; }
.ag-l-fine { font-size: var(--ag-fs-xs); }
"""

# Small phones (iPhone SE and the like) — type only, the layout above holds.
# Emitted after the mobile block every time, so it wins on source order.
_TINY_CSS = """
@media (max-width: 380px) {
  .ag-l-hero h1 { font-size: 28px; }
  .ag-l-depth h2 { font-size: 23px; }
  .ag-l-final h2 { font-size: 24px; }
  .ag-l-kpi-v { font-size: 16px; }
  .ag-l-tr { font-size: 12px; }
  .ag-l-provcard b { font-size: 16px; }
}
"""

_CSS = (
    "<style>"
    + _BASE_CSS
    + "@media (max-width: 640px) {"
    + _MOBILE_RULES
    + "}"
    + _TINY_CSS
    + "</style>"
)


# ------------------------------------------------------------------ sections


def _mark(width_class: str = "") -> str:
    src = esc(f"{ASSET_BASE}topstocks-icon.svg")
    cls = f' class="{width_class}"' if width_class else ""
    return f'<img src="{src}" alt="TopStocks"{cls}>'


def _lang_toggle(*, footer: bool = False) -> str:
    """EN / ES switch — one indexable URL each, paired by hreflang in the head.

    The switch keeps the jurisdiction: a reader on the Spanish-tax page who
    wants English wants the English *Spanish-tax* page, not a different
    country's argument.
    """
    es = _is_es()
    jur = active_jurisdiction()
    en_href, es_href = variant_path("en", jur), variant_path("es", jur)
    en_part = '<span class="on">EN</span>' if not es else f'<a href="{en_href}">EN</a>'
    es_part = '<span class="on">ES</span>' if es else f'<a href="{es_href}">ES</a>'
    if footer:
        return f'<div class="ag-l-foot-lang">{en_part}<span>·</span>{es_part}</div>'
    return f'<div class="ag-l-lang">{en_part}{es_part}</div>'


def _jur_toggle(*, footer: bool = False) -> str:
    """Spain-tax / US-tax switch, keeping the language.

    Footer-only in the chrome (the header has no room on a phone) plus one
    contextual link in the tax section, which is where a reader discovers the
    page is arguing the wrong country at them.
    """
    lang = active_language()
    here = active_jurisdiction()
    parts = []
    for code in ("ES", "US"):
        label = esc(tr(f"landing.jur_{code.lower()}"))
        parts.append(
            f'<span class="on">{label}</span>' if code == here
            else f'<a href="{variant_path(lang, code)}">{label}</a>'
        )
    if footer:
        return (
            f'<div class="ag-l-foot-lang">{parts[0]}<span>·</span>{parts[1]}</div>'
        )
    return f'<div class="ag-l-lang">{parts[0]}{parts[1]}</div>'


def _jur_switch_link() -> str:
    """"Filing in the US instead?" — the other country's page, same language."""
    other = "US" if active_jurisdiction() == "ES" else "ES"
    href = variant_path(active_language(), other)
    return (
        f'<a class="ag-l-jurswitch" href="{href}">'
        f'{esc(tr(f"landing.jur_switch_{other.lower()}"))}</a>'
    )


def _legal_lang_q() -> str:
    """Query string that keeps the legal pages in this page's language."""
    return "?lang=es" if _is_es() else ""


def _app_href(param: str) -> str:
    """The app entry URL for a CTA.

    Always root-absolute with the query parameter: `server.py` hands any
    parameterised request for `/` to Streamlit, so this one link both leaves
    the landing and tells the app what the visitor asked for. The Spanish page
    carries its language along, since the app resolves that per session.
    """
    lang = "" if not _is_es() else "&lang=es"
    return f"{PATH_EN}?{param}=1{lang}"


def _signin_link(cls: str, label: str, *, with_mark: bool = True) -> str:
    mark = _G_MARK if with_mark else ""
    href = esc(_app_href(PARAM_SIGNIN))
    return f'<a class="{cls}" href="{href}">{mark}{esc(label)}</a>'


def _guest_link(label: str) -> str:
    href = esc(_app_href(PARAM_GUEST))
    return f'<a class="ag-l-cta-text" href="{href}">{esc(label)} →</a>'


def _top_bar() -> str:
    return f"""
<header class="ag-l-bar"><div class="ag-l-wrap ag-l-bar-in">
  <div class="ag-l-brand">{_mark()}<span>TopStocks</span></div>
  <div class="ag-l-spacer"></div>
  {_lang_toggle()}
  <a class="ag-l-ghlink" href="{esc(_GITHUB_URL)}" target="_blank"
     rel="noopener noreferrer">{_GITHUB_MARK}{esc(tr("landing.nav_github"))}</a>
  {_signin_link("ag-l-cta-ghost", tr("common.sign_in_google"), with_mark=False)}
</div></header>"""


def _hero() -> str:
    kpis = (
        (tr("landing.kpi_cost"), _money(48230), ""),
        (tr("landing.kpi_value"), _money(61484), ""),
        (tr("landing.kpi_unrealised"), _money(13254, signed=True), "ag-l-up"),
        (
            tr("landing.kpi_realised", year=_FISCAL_YEAR),
            _money(2141, signed=True),
            "ag-l-up",
        ),
    )
    kpi_html = "".join(
        f'<div class="ag-l-kpi"><span class="ag-l-kpi-l">{esc(label)}</span>'
        f'<span class="ag-l-num ag-l-kpi-v {cls}">{esc(value)}</span></div>'
        for label, value, cls in kpis
    )

    positions = (
        ("MSFT", 18940, 6120, 47.7),
        ("ASML", 12306, 3485, 39.5),
        ("ITX.MC", 9872, 2410, 32.3),
        ("PYPL", 4517, -1893, -29.5),
    )
    rows = "".join(
        f'<div class="ag-l-tr"><span class="ag-l-tk">{esc(tk)}</span>'
        f'<span class="ag-l-dim">{esc(_plain(val))}</span>'
        f'<span class="ag-l-tk {"ag-l-up" if pl >= 0 else "ag-l-down"}">'
        f"{esc(_plain(pl, signed=True))}</span>"
        f'<span class="{"ag-l-up" if pl >= 0 else "ag-l-down"}">'
        f"{esc(_pct(pct))}</span></div>"
        for tk, val, pl, pct in positions
    )

    axis = "".join(f"<span>{esc(y)}</span>" for y in _CHART_YEARS)

    return f"""
<section class="ag-l-wrap ag-l-hero">
  <div class="ag-l-hero-copy">
    <h1>{esc(tr("landing.hero_title"))}</h1>
    <p class="ag-l-lede">{esc(tr("landing.hero_sub"))}</p>
    <div class="ag-l-ctarow">
      {_signin_link("ag-l-cta", tr("common.sign_in_google"))}
      {_guest_link(tr("landing.cta_guest"))}
    </div>
    <div class="ag-l-trust">{esc(tr("landing.trust_strip"))}</div>
  </div>
  <div class="ag-l-card">
    <div class="ag-l-kpis">{kpi_html}</div>
    <div class="ag-l-tbl">
      <div class="ag-l-th">
        <span>{esc(tr("landing.col_position"))}</span>
        <span>{esc(tr("landing.col_value_eur"))}</span>
        <span>{esc(tr("landing.col_pl_eur"))}</span>
        <span>{esc(tr("landing.col_pl_pct"))}</span>
      </div>{rows}
    </div>
    <div class="ag-l-chart">
      <div class="ag-l-legend">
        <span><span class="ag-l-sw ag-l-sw-twr"></span>
          {esc(tr("landing.legend_twr"))}</span>
        <span><span class="ag-l-sw ag-l-sw-bm"></span>{esc(_BENCHMARK)}</span>
      </div>
      <svg viewBox="0 0 560 130" preserveAspectRatio="none" aria-hidden="true">
        <line x1="0" y1="110" x2="560" y2="110" stroke="var(--ag-surface-card)"
              stroke-width="1"></line>
        <line x1="0" y1="70" x2="560" y2="70" stroke="var(--ag-surface-card)"
              stroke-width="1"></line>
        <line x1="0" y1="30" x2="560" y2="30" stroke="var(--ag-surface-card)"
              stroke-width="1"></line>
        <path d="M0,108 C40,104 60,96 90,98 C120,100 140,84 180,80 C220,76 240,88
                 280,78 C320,68 340,52 390,48 C440,44 460,56 500,40 C530,28 545,26
                 560,22" fill="none" stroke="var(--ag-landing-info)"
              stroke-width="1.6" opacity="0.65"></path>
        <path d="M0,110 C40,108 60,100 90,102 C120,104 150,78 190,72 C230,66 250,84
                 290,70 C330,56 350,44 400,38 C450,32 470,48 510,28 C535,18 548,16
                 560,12" fill="none" stroke="var(--ag-brand-accent)"
              stroke-width="2.2"></path>
      </svg>
      <div class="ag-l-axis ag-l-mono">{axis}</div>
    </div>
  </div>
</section>"""


def _brokers() -> str:
    tiles = []
    for name, formats, generic in _BROKERS:
        if generic:
            name = tr("landing.broker_generic")
            formats = tr("landing.broker_generic_note")
        cls = "ag-l-broker generic" if generic else "ag-l-broker"
        tiles.append(
            f'<div class="{cls}"><b>{esc(name)}</b>'
            f'<i class="ag-l-mono">{esc(formats)}</i></div>'
        )
    return f"""
<section class="ag-l-wrap ag-l-brokers">
  <div class="ag-l-brokers-line">{esc(tr("landing.brokers_line"))}</div>
  <div class="ag-l-brokergrid">{"".join(tiles)}</div>
</section>"""


def _gap() -> str:
    pairs = (
        ("landing.gap_p1", "landing.gap_b1", "landing.gap_s1"),
        ("landing.gap_p2", "landing.gap_b2", "landing.gap_s2"),
        ("landing.gap_p3", "landing.gap_b3", "landing.gap_s3"),
    )
    blocks = "".join(
        f'<div class="ag-l-gap">'
        f'<div><b class="ag-l-down">{esc(tr(head))}</b>'
        f"<p>{esc(tr(body))}</p></div>"
        f'<div><b class="ag-l-up">{esc(tr("landing.gap_does"))}</b>'
        f"<p>{esc(tr(fix))}</p></div></div>"
        for head, body, fix in pairs
    )

    fifo = f"""
<div class="ag-l-diagram">
  <span class="ag-l-mono ag-l-eyebrow-sm">{esc(tr("landing.fifo_label"))}</span>
  <div class="ag-l-lots">
    <div class="ag-l-lot sold" style="flex:10">
      <b>{esc(tr("landing.fifo_lot", n=1, year=2022))}</b>
      <i class="ag-l-mono">{esc(tr("landing.fifo_sold", n=10))}</i></div>
    <div class="ag-l-lotpair" style="flex:12">
      <div class="ag-l-lot sold" style="flex:5">
        <b>{esc(tr("landing.fifo_lot", n=2, year=2023))}</b>
        <i class="ag-l-mono">{esc(tr("landing.fifo_sold", n=5))}</i></div>
      <div class="ag-l-lot held" style="flex:7"><b>&nbsp;</b>
        <i class="ag-l-mono">{esc(tr("landing.fifo_held", n=7))}</i></div>
    </div>
    <div class="ag-l-lot held" style="flex:8">
      <b>{esc(tr("landing.fifo_lot", n=3, year=2025))}</b>
      <i class="ag-l-mono">{esc(tr("landing.fifo_held", n=8))}</i></div>
  </div>
  <span class="ag-l-note">{esc(tr("landing.fifo_note"))}</span>
</div>"""

    buy = esc(tr("landing.fx_buy"))
    sell = esc(tr("landing.fx_sell"))
    rate_lo = esc(_dec(1.098, 3))
    rate_hi = esc(_dec(1.132, 3))
    fx = f"""
<div class="ag-l-diagram">
  <span class="ag-l-mono ag-l-eyebrow-sm">{esc(tr("landing.fx_label"))}</span>
  <div class="ag-l-tl">
    <div class="ag-l-tl-rail"></div>
    <div class="ag-l-tl-tick" style="left:8%"></div>
    <div class="ag-l-tl-lab ag-l-mono" style="left:8%">
      {buy} 03·2022<br>{rate_lo} $/€</div>
    <div class="ag-l-tl-tick" style="left:72%"></div>
    <div class="ag-l-tl-lab ag-l-mono" style="left:72%">
      {sell} 05·2025<br>{rate_hi} $/€</div>
    <div class="ag-l-tl-tick faint" style="left:96%"></div>
    <div class="ag-l-tl-lab ag-l-mono faint" style="left:96%">
      {esc(tr("landing.fx_today"))}<br>{esc(tr("landing.fx_spot"))}</div>
  </div>
  <div class="ag-l-compare">
    <div class="broker"><small>{esc(tr("landing.fx_broker"))}</small>
      <b class="ag-l-num">{esc(_money(1412, signed=True))}</b></div>
    <div class="ours"><small>{esc(tr("landing.fx_topstocks"))}</small>
      <b class="ag-l-num">{esc(_money(1168, signed=True))}</b></div>
  </div>
  <span class="ag-l-note">{esc(tr("landing.fx_note"))}</span>
</div>"""

    return f"""
<section class="ag-l-band"><div class="ag-l-wrap ag-l-sec">
  <div class="ag-l-head">
    <h2>{esc(tr("landing.gap_title"))}</h2>
    <p>{esc(tr("landing.gap_sub"))}</p>
  </div>
  <div class="ag-l-gaps">{blocks}</div>
  <div class="ag-l-diagrams">{fifo}{fx}</div>
</div></section>"""


def _how() -> str:
    buckets = (
        ("ok", tr("landing.imp_clean"), 214),
        ("warn", tr("landing.imp_warn"), 3),
        ("bad", tr("landing.imp_reject"), 1),
        ("skip", tr("landing.imp_skip"), 6),
    )
    bucket_html = "".join(
        f'<div class="ag-l-bucket {cls}"><b>{esc(label)}</b>'
        f'<i class="ag-l-mono">{esc(_rows(n))}</i></div>'
        for cls, label, n in buckets
    )

    mini_rows = "".join(
        f'<div class="ag-l-mini-r"><span class="ag-l-tk">{esc(tk)}</span>'
        f'<span class="ag-l-up">{esc(_plain(pl, signed=True))}</span>'
        f'<span class="ag-l-dim">{esc(_pct(w, signed=False))}</span></div>'
        for tk, pl, w in (
            ("MSFT", 6120, 30.8),
            ("ASML", 3485, 20.0),
            ("ITX.MC", 2410, 16.1),
        )
    )

    return f"""
<section class="ag-l-wrap ag-l-sec">
  <h2>{esc(tr("landing.how_title"))}</h2>
  <div class="ag-l-steps">
    <div class="ag-l-step">
      <h3>{esc(tr("landing.step1_title"))}</h3>
      <div class="ag-l-gbtn"><span>{_G_MARK}
        {esc(tr("common.sign_in_google"))}</span></div>
      <p>{esc(tr("landing.step1_body"))}</p>
    </div>
    <div class="ag-l-step">
      <h3>{esc(tr("landing.step2_title"))}</h3>
      <div class="ag-l-buckets">{bucket_html}</div>
      <p>{esc(tr("landing.step2_body"))}</p>
    </div>
    <div class="ag-l-step">
      <h3>{esc(tr("landing.step3_title"))}</h3>
      <div class="ag-l-mini">
        <div class="ag-l-mini-h">
          <span>{esc(tr("landing.col_position"))}</span>
          <span>{esc(tr("landing.col_eur_pl"))}</span>
          <span>{esc(tr("landing.col_weight"))}</span>
        </div>{mini_rows}
      </div>
      <p>{esc(tr("landing.step3_body"))}</p>
    </div>
  </div>
</section>"""


def _provenance() -> str:
    cards = (
        ("landing.prov_k1_label", {}, "$391.0B", "fact", "landing.tag_fact",
         "landing.prov_k1_src", {}),
        ("landing.prov_k2_label", {"year": 2027}, "$8.42", "consensus",
         "landing.tag_consensus", "landing.prov_k2_src", {"n": 31}),
        ("landing.prov_k3_label", {}, _pct(3.1, signed=False), "derived",
         "landing.tag_derived", "landing.prov_k3_src", {}),
    )
    card_html = "".join(
        f'<div class="ag-l-provcard"><div class="ag-l-provcard-h">'
        f"<span>{esc(tr(label, **largs))}</span>"
        f'<span class="ag-l-tag {cls}">{esc(tr(tag))}</span></div>'
        f'<b class="ag-l-num">{esc(value)}</b>'
        f"<small>{esc(tr(src, **sargs))}</small></div>"
        for label, largs, value, cls, tag, src, sargs in cards
    )
    card_html += (
        '<div class="ag-l-provcard"><div class="ag-l-provcard-h">'
        f'<span>{esc(tr("landing.prov_k4_label"))}</span></div>'
        '<b class="ag-l-num ag-l-na">n/a</b>'
        f'<small>{esc(tr("landing.prov_k4_src"))}</small></div>'
    )

    # The body names its three tags inline and colours each one, so the template
    # is escaped first and the marked-up words are substituted afterwards.
    body = esc(tr("landing.prov_body")).format(
        fact=f'<b class="fact">{esc(tr("landing.prov_word_fact"))}</b>',
        consensus=f'<b class="consensus">{esc(tr("landing.prov_word_consensus"))}</b>',
        derived=f'<b class="derived">{esc(tr("landing.prov_word_derived"))}</b>',
    )

    return f"""
<section class="ag-l-prov"><div class="ag-l-wrap ag-l-prov-in">
  <h2>{esc(tr("landing.prov_title"))}</h2>
  <div class="ag-l-provrow">{card_html}</div>
  <p>{body}</p>
</div></section>"""


def _bento() -> str:
    # Pills are symbols and statute references — language-neutral, so they stay
    # literal data rather than catalog entries. The tax ones are still
    # jurisdiction-specific: a statute cite is the one thing that cannot be
    # translated across borders.
    risk_pills = (
        f"TWR {_pct(18.4)}",
        f"1/HHI {_dec(6.8, 1)}",
        f"β {_dec(1.12, 2)} vs {_BENCHMARK}",
        f"max DD {_pct(-22.6)}",
    )
    tax_pills = _TAX_PILLS.get(active_jurisdiction(), _TAX_PILLS["ES"])

    def pills(items: tuple[str, ...]) -> str:
        inner = "".join(f'<span class="ag-l-pill">{esc(p)}</span>' for p in items)
        return f'<div class="ag-l-pills">{inner}</div>'

    small = (
        ("landing.f_ai_title", "landing.f_ai_body"),
        ("landing.f_deep_title", "landing.f_deep_body"),
        ("landing.f_earnings_title", "landing.f_earnings_body"),
        ("landing.f_alerts_title", "landing.f_alerts_body"),
        ("landing.f_telegram_title", "landing.f_telegram_body"),
        ("landing.f_crypto_title", "landing.f_crypto_body"),
    )
    small_html = "".join(
        f'<div class="ag-l-tile"><h3>{esc(tr(title))}</h3>'
        f"<p>{esc(tr(body))}</p></div>"
        for title, body in small
    )

    return f"""
<section class="ag-l-wrap ag-l-sec">
  <h2>{esc(tr("landing.bento_title"))}</h2>
  <div class="ag-l-bento-lg">
    <div class="ag-l-tile"><h3>{esc(tr("landing.f_portfolio_title"))}</h3>
      <p>{esc(tr("landing.f_portfolio_body"))}</p>{pills(risk_pills)}</div>
    <div class="ag-l-tile"><h3>{esc(tr("landing.f_tax_title"))}</h3>
      <p>{esc(tr("landing.f_tax_body"))}</p>{pills(tax_pills)}</div>
  </div>
  <div class="ag-l-bento-sm">{small_html}</div>
</section>"""


def _depth_a() -> str:
    legs = (
        ("2022-03-14", tr("landing.fx_buy"), 10, 280.50, 1.0982),
        ("2025-05-09", tr("landing.fx_sell"), 10, 438.20, 1.1324),
    )
    leg_html = []
    for i, (date, verb, qty, price, rate) in enumerate(legs):
        if i:
            leg_html.append('<div class="ag-l-legjoin"></div>')
        leg_html.append(
            f'<div class="ag-l-leg"><span class="ag-l-dot"></span>'
            f'<span class="ag-l-mono">{esc(date)} · {esc(verb)} {qty} @ '
            f'${esc(_dec(price, 2))}</span>'
            f'<b class="ag-l-mono">ECB {esc(_dec(rate, 4))}</b></div>'
        )

    return f"""
<section class="ag-l-rule"><div class="ag-l-wrap ag-l-depth">
  <div class="ag-l-depth-copy">
    <span class="ag-l-eyebrow">{esc(tr("landing.depthA_eyebrow"))}</span>
    <h2>{esc(tr("landing.depthA_title"))}</h2>
    <p>{esc(tr("landing.depthA_body"))}</p>
  </div>
  <div class="ag-l-flat">
    <span class="ag-l-mono ag-l-eyebrow-sm">MSFT ·
      {esc(tr("landing.depthA_label"))}</span>
    <div class="ag-l-legs">{"".join(leg_html)}</div>
    <div class="ag-l-split">
      <div class="broker"><small>{esc(tr("landing.depthA_broker"))}</small>
        <b class="ag-l-num">{esc(_money(1412, signed=True))}</b></div>
      <div class="ours"><small>{esc(tr("landing.depthA_topstocks"))}</small>
        <b class="ag-l-num">{esc(_money(1168, signed=True))}</b></div>
    </div>
  </div>
</div></section>"""


# Statute pills on the tax feature card, per jurisdiction.
_TAX_PILLS = {
    "ES": ("art. 37 LIRPF", "art. 33.5.f", "Modelo 720", "19–28%"),
    "US": ("FIFO", "IRC 1091", "Form 8938", "0/15/20%"),
}

# Headline rate on the panel's sample base: Spain's first savings-base bracket,
# the US long-term middle rate. Illustrative, like every figure in the mocks.
_PANEL_RATE = {"ES": 19.0, "US": 15.0}


def _depth_b() -> str:
    rate = _pct(_PANEL_RATE.get(active_jurisdiction(), 19.0), signed=False)
    rows = f"""
<div class="ag-l-irpf-r"><span>{esc(tr("landing.irpf_gains"))}</span>
  <b class="ag-l-up">{esc(_money(2521, signed=True))}</b></div>
<div class="ag-l-irpf-r"><span>{esc(tr("landing.irpf_losses"))}</span>
  <b class="ag-l-down">{esc(_money(-380, signed=True))}</b></div>
<div class="ag-l-irpf-r ag-l-warnrow">
  <span>{_WARN_MARK}{esc(tr("landing.irpf_deferred"))}</span>
  <b class="ag-l-struck">{esc(_money(-612, signed=True))}</b></div>
<div class="ag-l-irpf-r total"><span>{esc(tr("landing.irpf_net"))}</span>
  <b>{esc(_money(2141))}</b></div>
<div class="ag-l-irpf-r total">
  <span>{esc(tr("landing.irpf_tax", rate=rate))}</span>
  <b>{esc(_money(407))}</b></div>
<div class="ag-l-irpf-r"><span>{esc(tr("landing.irpf_carry"))}</span>
  <b class="ag-l-down">{esc(_money(-612, signed=True))}</b></div>"""

    return f"""
<section class="ag-l-band"><div class="ag-l-wrap ag-l-depth">
  <div class="ag-l-flat sunken">
    <span class="ag-l-mono ag-l-eyebrow-sm">
      {esc(tr("landing.irpf_label", year=_FISCAL_YEAR))}</span>
    <div class="ag-l-irpf">{rows}</div>
  </div>
  <div class="ag-l-depth-copy">
    <span class="ag-l-eyebrow">{esc(tr("landing.depthB_eyebrow"))}</span>
    <h2>{esc(tr("landing.depthB_title"))}</h2>
    <p>{esc(tr("landing.depthB_body"))}</p>
    <div class="ag-l-disclaimer">{esc(tr("landing.depthB_disclaimer"))}</div>
    {_jur_switch_link()}
  </div>
</div></section>"""


def _trust() -> str:
    items = "".join(
        f'<div class="ag-l-trustitem"><b>{esc(tr(f"landing.t{i}_h"))}</b>'
        f'<span>{esc(tr(f"landing.t{i}_b"))}</span></div>'
        for i in range(1, 6)
    )
    return f"""
<section class="ag-l-wrap ag-l-sec">
  <h2>{esc(tr("landing.trust_title"))}</h2>
  <div class="ag-l-trustgrid">{items}</div>
</section>"""


def _faq() -> str:
    qs = "".join(
        f'<details class="ag-l-q"><summary>{esc(tr(f"landing.faq_q{i}"))}</summary>'
        f'<p>{esc(tr(f"landing.faq_a{i}"))}</p></details>'
        for i in range(1, FAQ_COUNT + 1)
    )
    return f"""
<section class="ag-l-band"><div class="ag-l-faq">
  <h2>{esc(tr("landing.faq_title"))}</h2>
  <div class="ag-l-qs">{qs}</div>
</div></section>"""


def _final() -> str:
    return f"""
<section class="ag-l-rule"><div class="ag-l-wrap ag-l-final">
  <h2>{esc(tr("landing.final_title"))}</h2>
  <div class="ag-l-ctarow">
    {_signin_link("ag-l-cta", tr("common.sign_in_google"))}
    {_guest_link(tr("landing.cta_guest_short"))}
  </div>
</div></section>"""


def _footer() -> str:
    return f"""
<footer class="ag-l-rule"><div class="ag-l-wrap ag-l-foot">
  <div class="ag-l-foot-row">
    <div class="ag-l-brand">{_mark()}<span>TopStocks</span></div>
    <a href="{esc(_GITHUB_URL)}" target="_blank"
       rel="noopener noreferrer">{esc(tr("landing.nav_github"))}</a>
    <a href="/legal/privacy{_legal_lang_q()}">{esc(tr("landing.footer_privacy"))}</a>
    <a href="/legal/terms{_legal_lang_q()}">{esc(tr("landing.footer_terms"))}</a>
    {_lang_toggle(footer=True)}
    {_jur_toggle(footer=True)}
  </div>
  <p class="ag-l-fine">{esc(tr("landing.footer_disclaimer"))}</p>
</div></footer>"""


def _mobile_bar() -> str:
    """The fixed bottom CTA, phones only.

    Rendered on every viewport and hidden by default — `_MOBILE_RULES` is what
    reveals it, so the switch stays in one place with the rest of the phone
    layout. The guest link is not duplicated here: it sits in the hero, one
    screen up, and a second line would cost another 30px of a 640px-tall
    viewport for the whole scroll.
    """
    label = tr("common.sign_in_google")
    return (
        '<div class="ag-l-mbar">'
        + _signin_link("ag-l-cta", label)
        + "</div>"
    )


# The bar's reveal. An IntersectionObserver on the two in-page CTA rows,
# toggling one class on the page root — no scroll handler, no layout reads.
# Wired once per session (Streamlit re-runs script elements on every rerun) and
# retried through a MutationObserver, because this block can reach the DOM
# before the markup it observes. Its failure mode is the bar staying visible,
# which is the state the stylesheet already gives it.
#
# No "less-than" character in here either: same sanitiser, same rule.
_BAR_JS = """
<script>
(function () {
  if (window.__topstocksLandingBar) return;  /* survive reruns — wire once */
  window.__topstocksLandingBar = true;
  const wire = () => {
    const root = document.querySelector(".ag-l");
    const ctas = document.querySelectorAll(
      ".ag-l-hero .ag-l-ctarow, .ag-l-final .ag-l-ctarow"
    );
    if (!root || ctas.length !== 2) return false;
    root.classList.add("cta-visible");  /* no flash of the bar at the top */
    const onScreen = new Set();
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) onScreen.add(e.target);
        else onScreen.delete(e.target);
      });
      root.classList.toggle("cta-visible", onScreen.size !== 0);
    }, {threshold: 0.35});
    ctas.forEach((el) => io.observe(el));
    return true;
  };
  if (!wire()) {
    const mo = new MutationObserver(() => { if (wire()) mo.disconnect(); });
    mo.observe(document.body, {subtree: true, childList: true});
  }
})();
</script>
"""


def _mobile_css_body() -> str:
    """The phone rules again, at a wider breakpoint, for User-Agent gating.

    `_CSS` already applies them under `max-width: 640px`, which is the right
    trigger — layout should follow width. But a phone can report a CSS
    viewport wider than that (large handsets, landscape, a stale zoom). Where
    the two disagree below 900px, believe the User-Agent: a touch device wants
    the stacked layout and the thumb-reachable CTA either way.

    `landing_static` ships this in a `<style media="not all">` element and a
    one-line script flips the attribute to `all` for phone User-Agents, which
    is how a static document does a server-side check it no longer has. Placed
    after `_CSS`, so it wins on source order; `_TINY_CSS` rides along to stay
    last.
    """
    return "@media (max-width: 900px) {" + _MOBILE_RULES + "}" + _TINY_CSS


def _mobile_css() -> str:
    """`_mobile_css_body()` in its own style element."""
    return "<style>" + _mobile_css_body() + "</style>"


# --------------------------------------------------------------------- entry


def stylesheet() -> str:
    """The page stylesheet: base rules, the 640px phone block, small-phone type."""
    return _CSS


def ua_mobile_rules() -> str:
    """The phone rules at 900px, bare CSS — see `_mobile_css_body`."""
    return _mobile_css_body()


def bar_script() -> str:
    """The mobile CTA bar's reveal script, in its `<script>` element."""
    return _BAR_JS


def consume_params() -> None:
    """Act on the landing CTAs' query parameters. Called once per app rerun.

    The CTAs are links, so arriving in the app *is* the click: `?signin=1`
    starts the OIDC round-trip, `?lang=` pins the copy to the language the
    visitor was reading, and `?guest=1` means "in as a guest" — nothing to do
    beyond clearing it out of the URL. Left alone otherwise, so the app's own
    `?ticker=` deep links pass straight through.

    `?lang=` stays in the URL on purpose: `app.py` re-resolves the language
    from prefs on every rerun, and the parameter is what keeps re-applying the
    visitor's choice for the rest of a signed-out session.
    """
    params = st.query_params

    lang = (params.get("lang") or "").strip().lower()
    if lang in LANGUAGES:
        st.session_state["active_lang"] = lang

    if params.get(PARAM_GUEST):
        del params[PARAM_GUEST]  # in-session rerun; nothing else to keep

    # Guarded on the session, not just the parameter: st.login() redirects, and
    # the parameter can survive the round trip — an unguarded call would then
    # bounce an already-signed-in visitor back to Google on every rerun.
    if params.get(PARAM_SIGNIN) and "auth" in st.secrets and not auth.is_logged_in():
        st.login()
        # st.login() only enqueues the redirect; without the stop, the rest of
        # the run keeps rendering into a page the browser is abandoning, and
        # the aborted chunk loads flash module-import errors until Google
        # takes over.
        st.stop()


def page_body() -> str:
    """The whole page as one element — every section, in order.

    Wrap the call in `render_language()`; `landing_static` does.
    """
    return (
        '<div class="ag-l">'
        + _top_bar()
        + _hero()
        + _brokers()
        + _gap()
        + _how()
        + _provenance()
        + _bento()
        + _depth_a()
        + _depth_b()
        + _trust()
        + _faq()
        + _final()
        + _footer()
        + _mobile_bar()
        + "</div>"
    )
