"""Shared Streamlit widgets — chiefly the ticker picker used across pages.

Every place that selects a ticker uses `ticker_picker`: a searchbar on top of a
fixed-height, scrollable, logo-tagged list of the watchlist (favorites first),
with an "Analyze <SYMBOL>" escape hatch for symbols not on the list. Keeping it
in one place means the main dashboard and the valuation page get the identical
picker instead of a bespoke `text_input` each.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote

import pandas as pd
import streamlit as st
from yfinance.exceptions import YFRateLimitError

from stocks.config import Alert, load_watchlist
from stocks.data.logo import brand_logo_url, logo_url, mirror_brand, mirror_logo
from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio
from stocks.web import auth, notices, skeletons
from stocks.web.i18n import t as tr

# ─────────────────────────────────────────────────── TopStocks design tokens
# Single source of truth for every color, radius, elevation and type step the
# Streamlit theme (.streamlit/config.toml) can't reach — our own HTML, CCv2
# component CSS and Plotly figures. Values are Amphora Web DS tokens and MUST
# stay in lockstep with config.toml, which paints Streamlit's own chrome from
# the same ramp. Nothing outside this block may write a raw hex: Python code
# imports these names, CSS reads the `--ag-*` custom properties DS_VARS_CSS
# emits from them (see ds_vars_css), so a token change lands everywhere at once.

# Semantic — market direction. Reserved for price change only; the light
# success/critical fills are the variants that read on a dark surface.
UP_COLOR = "#DBFFD2"    # alza — market gain (DS success fill)
DOWN_COLOR = "#FFD2CB"  # baja — market loss (DS critical fill)
SUCCESS_FILL = "#2A8200"    # DS success highlight — gain pill/badge background
DOWN_FILL = "#8C1F00"       # DS market-loss pill background (pairs with DOWN_COLOR)
CRITICAL_FILL = "#CC402F"   # DS critical stroke — error states, loss chart marks
WARN_COLOR = "#F4C600"      # aviso — caution (DS caution highlight)
WARN_ORANGE = "#EF752E"     # warning (DS orange) — secondary chart accent
INFO_COLOR = "#7290F0"      # info — chart lines, informational (DS blue 500)
INFO_DEEP = "#4667D0"       # DS chart blue 600 — second info step

# Brand purple ramp. BRAND_CTA/BRAND_ACCENT keep their historic names; the
# numbered steps match the DS scale the config.toml comments already cite.
PURPLE_900 = "#301263"  # active nav row / active chip fill
PURPLE_800 = "#4E2092"  # brand badge fill, active chip border
PURPLE_700 = "#6A2EBF"  # hover state on purple fills
BRAND_CTA = "#7F3FE8"   # purple 600 — CTAs, primary buttons, focus rings
BRAND_ACCENT = "#A98EF7"  # purple 500 — accents, active iconography, links
PURPLE_400 = "#C6B7FB"  # accent text on purple fills
PURPLE_300 = "#DED7FD"  # primary text on purple fills, mono/code

# Neutral ramp — surfaces, borders, text. Mirrors config.toml's
# backgroundColor / secondaryBackgroundColor / borderColor / textColor.
SURFACE_PAGE = "#18161C"    # neutral-950 — page and sidebar background
SURFACE_CARD = "#28262D"    # neutral-900 — elevated surface (cards, inputs)
SURFACE_HOVER = "#333139"   # DS hover step — nav rows, table rows, list buttons
BORDER = "#3B3942"          # neutral-800 — borders, dividers, table rules
BORDER_FOCUS = "#48454F"    # DS focus/hover border — outlined controls on hover
TEXT_PRIMARY = "#F9F9FA"    # neutral-50 — primary text (also the logo plate)
TEXT_SECONDARY = "#B3AFBD"  # neutral-400 — widget labels, secondary body
TEXT_MUTED = "#827F8C"      # neutral-500 — captions, chart axes, company names
TEXT_FAINT = "#696673"      # neutral-600 — section headers, separators, rules

# Landing page. The public marketing surface needs two steps the app chrome
# never asked for: a card fill between SURFACE_PAGE and SURFACE_CARD, and a
# mid-tone green/red pair for figures. The DS success/critical pairs are a dark
# fill plus a light tint, which reads as a badge rather than as a number, so
# these follow the candle hues instead. Used only by landing.py.
SURFACE_RAISED = "#1F1D24"      # landing card fill — between page and card
SURFACE_BAND = "#1B1920"        # alternating full-width section band
SURFACE_BRAND_BAND = "#221B31"  # provenance band — purple-tinted page
BORDER_BRAND_BAND = "#3B3157"   # provenance band edge
LANDING_UP = "#2AC77E"          # positive figures, "fact" provenance tag
LANDING_DOWN = "#F0526A"        # negative figures, rejected import rows
LANDING_INFO = "#4C8DFF"        # benchmark series, "consensus" provenance tag
LANDING_WARN = "#F5B940"        # import warnings, deferred loss, disclaimers
ON_BRAND = "#FEFEFF"            # text on a BRAND_CTA fill

# Back-compat aliases — every green/red profit-loss cue routes through these.
PROFIT_COLOR, LOSS_COLOR = UP_COLOR, DOWN_COLOR

# Price-chart series hues, straight from the Aguait DS chart spec (section 07):
# mid-tone candle green/red that carry on a dark surface without stealing
# UP/DOWN (reserved for text and badges), plus the SMA overlay amber/blue the
# spec fixes at 1.5px line weight.
CANDLE_UP = "#7ED28C"    # bullish candles — DS success, mid step
CANDLE_DOWN = "#F0897E"  # bearish candles — DS critical, mid step
SMA_FAST = "#F2A33C"     # SMA20 overlay — DS chart amber (softer than WARN_ORANGE)
SMA_SLOW = "#6E8FF0"     # SMA50 overlay + results markers — DS chart blue
EVENT_LINE = TEXT_FAINT  # dashed corporate-event verticals + crosshair — neutral-600

# Alpha variants. Written as rgba() because Plotly's SVG attributes predate
# 8-digit hex; the base hue is always the token named in the comment.
PROFIT_COLOR_MUTED = "rgba(219,255,210,0.45)"  # UP_COLOR @ 45%
LOSS_COLOR_MUTED = "rgba(255,210,203,0.45)"    # DOWN_COLOR @ 45%
PROFIT_BAND = "rgba(42,130,0,0.35)"      # SUCCESS_FILL @ 35% — area fills
LOSS_BAND = "rgba(204,64,47,0.25)"       # CRITICAL_FILL @ 25% — area fills
ACCENT_BAND = "rgba(169,142,247,0.15)"   # BRAND_ACCENT @ 15% — forecast bands
ACCENT_AREA = "rgba(169,142,247,0.22)"   # BRAND_ACCENT @ 22% — price-line area top
WARN_BAND = "rgba(244,198,0,0.18)"       # WARN_COLOR @ 18% — caution chip fill
SURFACE_SUNKEN = "rgba(59,57,66,0.25)"   # BORDER @ 25% — out-of-range cells
RULE_SOFT = "rgba(59,57,66,0.5)"         # BORDER @ 50% — dense row dividers
SURFACE_PAGE_HAZE = "rgba(24,22,28,0.92)"  # SURFACE_PAGE @ 92% — sticky topbar
SURFACE_PAGE_VEIL = "rgba(24,22,28,0.85)"  # SURFACE_PAGE @ 85% — touch chart readout
CTA_GLOW = "rgba(127,63,232,0.25)"       # BRAND_CTA @ 25% — launcher shadow
CTA_HALO = "rgba(127,63,232,0.4)"        # BRAND_CTA @ 40% — avatar shadow
CTA_TINT = "rgba(127,63,232,0.16)"       # BRAND_CTA @ 16% — user bubble fill
CTA_TINT_EDGE = "rgba(127,63,232,0.35)"  # BRAND_CTA @ 35% — user bubble border
SKELETON_BASE = "rgba(105,102,115,0.25)"  # TEXT_FAINT @ 25% — shimmer trough
SKELETON_HI = "rgba(105,102,115,0.45)"    # TEXT_FAINT @ 45% — shimmer crest
TRANSPARENT = "rgba(0,0,0,0)"            # Plotly canvas — inherit the surface

# Elevation. The DS has no shadow token, so the app defines three steps and
# uses nothing else; all three are neutral black over the purple-tinted
# surfaces, matching the design's soft-dark cards. The two bare colors exist
# for the side-anchored panels (the sidebar rail, the chat drawer), whose
# shadow must throw sideways — they compose their own offsets and take only
# the tint from here, so every elevation in the app still shares one palette.
SHADOW_COLOR = "rgba(0,0,0,0.35)"                # card-level tint
SHADOW_COLOR_STRONG = "rgba(0,0,0,0.5)"          # overlay-level tint
SHADOW_CARD = f"0px 2px 4px {SHADOW_COLOR}"      # section cards
SHADOW_HOVER = "0px 8px 15px rgba(0,0,0,0.45)"   # Plotly hover box — DS dialog step
SHADOW_OVERLAY = "0px 8px 15px rgba(0,0,0,0.45)"  # dropdowns, popovers — DS dialog step

# Radius scale — config.toml's baseRadius (8px) is the middle step.
RADIUS_XS = "4px"       # logo chips, calendar chips, small badges
RADIUS_NAV = "6px"      # sidebar nav rows, segmented-selector chips (DS "6 · nav")
RADIUS_SM = "8px"       # inputs, buttons, table cells
RADIUS_MD = "12px"      # inset tiles, chat bubbles, Plotly hover box
RADIUS_LG = "16px"      # section cards
RADIUS_PILL = "9999px"  # delta pills

# Type scale — config.toml's headingFontSizes (28/22/18/16/14/12) extended
# down with the three chrome steps the app needs. px, like the DS scale, so a
# baseFontSize change never silently rescales our own HTML.
FS_2XS = "10px"   # nav-section caps, dense calendar chips — DS floor, never below
FS_XS = "11px"    # captions, tile labels, small pills
FS_SM = "12px"    # metric labels, muted secondary lines
FS_MD = "13px"    # nav rows, selector chips, table cells
FS_BASE = "14px"  # body (config.toml baseFontSize)
FS_LG = "16px"    # tile values
FS_XL = "18px"    # h3 / KPI figures
FS_2XL = "22px"   # h2
FS_3XL = "28px"   # h1
FS_DISPLAY = "32px"  # hero price figure (Epilogue), one step above h1

# Icon sizing is its own dimension, not a type step: a Material glyph takes
# its size from font-size, and the wrapper spans' width/height must match it
# exactly or the row height shifts. One token keeps all three in step.
ICON_NAV = "1.6rem"  # sidebar nav glyphs, identical in every rail state

# Diverging ramp for correlation heatmaps. config.toml ships a sequential
# purple ramp (chartSequentialColors) but no diverging one, so this is built
# from the DS semantic pair: brand blue for inverse, neutral-800 for
# uncorrelated, critical for tightly correlated. Plotly colorscale form.
DIVERGING_SCALE = [
    [0.0, INFO_DEEP],      # strongly inverse
    [0.25, INFO_COLOR],
    [0.5, BORDER],         # uncorrelated
    [0.75, CRITICAL_FILL],
    [1.0, DOWN_COLOR],     # moves together
]

# Sequential ramp for magnitude fills (choropleths). The DS brand purple ramp
# (config.toml chartSequentialColors), dark→light. Starts at PURPLE_800, not
# 900: the bottom step must still separate from the SURFACE_PAGE land fill a
# globe paints under countries with no holdings. Plotly colorscale form.
SEQUENTIAL_SCALE = [
    [0.0, PURPLE_800],
    [0.25, PURPLE_700],
    [0.5, BRAND_CTA],
    [0.75, BRAND_ACCENT],
    [1.0, PURPLE_300],
]

# DS chart magenta — categorical slot 3. Exists only in config.toml's
# chartCategoricalColors ramp (no other DS role), named here so the mirror
# below stays hex-free like everything else outside this block.
CHART_MAGENTA = "#C54EA4"

# Categorical series palette — mirrors config.toml chartCategoricalColors
# (which themes the Vega charts) so Plotly traces painted from here match.
# Fixed order, never cycled: a chart with more series than this folds the
# tail into one muted "Others" bucket instead of inventing a 9th hue.
CATEGORICAL_COLORS = [
    BRAND_ACCENT,   # purple 500
    INFO_COLOR,     # blue 500
    CHART_MAGENTA,
    WARN_COLOR,     # yellow
    WARN_ORANGE,
    INFO_DEEP,      # blue 600
    PURPLE_300,
    TEXT_MUTED,
]

# Brand exception, deliberately NOT a DS neutral: Google's sign-in guidelines
# require the "G" mark on pure white, so auth.py's button tile opts out of the
# ramp. It is declared here so an audit finds it named instead of as a stray
# literal, and so it stays the only such exception.
BRAND_GOOGLE_TILE = "#FFFFFF"


def ds_vars_css() -> str:
    """`:root` custom properties for every token above, as a `<style>` block.

    Our CSS lives in string literals scattered across pages, most of them plain
    (non-f) triple-quoted blocks full of CSS braces — threading Python values
    through them would mean escaping every `{`. Emitting the tokens once as
    `--ag-*` custom properties instead lets that CSS read `var(--ag-border)`
    and stay literal, while Python keeps a single source of truth. Custom
    properties inherit into CCv2 shadow roots, so component `css=` blocks
    resolve them too. app.py injects this before its own stylesheet.
    """
    tokens = {
        # color — semantic
        "up": UP_COLOR, "down": DOWN_COLOR,
        "success-fill": SUCCESS_FILL, "down-fill": DOWN_FILL,
        "critical-fill": CRITICAL_FILL,
        "warn": WARN_COLOR, "warn-orange": WARN_ORANGE,
        "info": INFO_COLOR, "info-deep": INFO_DEEP,
        # color — brand
        "purple-900": PURPLE_900, "purple-800": PURPLE_800,
        "purple-700": PURPLE_700, "brand-cta": BRAND_CTA,
        "brand-accent": BRAND_ACCENT, "purple-400": PURPLE_400,
        "purple-300": PURPLE_300,
        # color — neutral
        "surface-page": SURFACE_PAGE, "surface-card": SURFACE_CARD,
        "surface-hover": SURFACE_HOVER,
        "border": BORDER, "border-focus": BORDER_FOCUS,
        "text-primary": TEXT_PRIMARY,
        "text-secondary": TEXT_SECONDARY, "text-muted": TEXT_MUTED,
        "text-faint": TEXT_FAINT,
        # color — landing surface
        "surface-raised": SURFACE_RAISED, "surface-band": SURFACE_BAND,
        "surface-brand-band": SURFACE_BRAND_BAND,
        "border-brand-band": BORDER_BRAND_BAND,
        "landing-up": LANDING_UP, "landing-down": LANDING_DOWN,
        "landing-info": LANDING_INFO, "landing-warn": LANDING_WARN,
        "on-brand": ON_BRAND,
        # color — alpha variants
        "profit-band": PROFIT_BAND, "loss-band": LOSS_BAND,
        "surface-sunken": SURFACE_SUNKEN, "rule-soft": RULE_SOFT,
        "surface-page-haze": SURFACE_PAGE_HAZE,
        "surface-page-veil": SURFACE_PAGE_VEIL,
        "cta-glow": CTA_GLOW, "cta-halo": CTA_HALO,
        "cta-tint": CTA_TINT, "cta-tint-edge": CTA_TINT_EDGE,
        "skeleton-base": SKELETON_BASE, "skeleton-hi": SKELETON_HI,
        "brand-google-tile": BRAND_GOOGLE_TILE,
        # elevation
        "shadow-card": SHADOW_CARD, "shadow-hover": SHADOW_HOVER,
        "shadow-overlay": SHADOW_OVERLAY,
        "shadow-color": SHADOW_COLOR,
        "shadow-color-strong": SHADOW_COLOR_STRONG,
        # radius
        "radius-xs": RADIUS_XS, "radius-nav": RADIUS_NAV,
        "radius-sm": RADIUS_SM,
        "radius-md": RADIUS_MD, "radius-lg": RADIUS_LG,
        "radius-pill": RADIUS_PILL,
        # type scale
        "fs-2xs": FS_2XS, "fs-xs": FS_XS, "fs-sm": FS_SM,
        "fs-md": FS_MD, "fs-base": FS_BASE, "fs-lg": FS_LG, "fs-xl": FS_XL,
        "fs-2xl": FS_2XL, "fs-3xl": FS_3XL, "fs-display": FS_DISPLAY,
        # icon
        "icon-nav": ICON_NAV,
    }
    body = "".join(f"--ag-{k}:{v};" for k, v in tokens.items())
    return f"<style>:root{{{body}}}</style>"



# One hover-label look for every chart — the DS chart-tooltip spec, echoed onto
# Plotly's SVG tooltip: SURFACE_PAGE (the tooltip sits page-toned, darker than
# the card it floats over), BORDER, Instrument Sans face, TEXT_PRIMARY text.
# namelength=-1 so trace names never truncate to 15 chars. Radius + elevation
# (which Plotly's hoverlabel can't set) come from CSS in app.py. Font size
# drops on mobile (see show_chart) so a multi-row box fits a ~390px screen.
HOVER_FONT_DESKTOP = 15
HOVER_FONT_MOBILE = 11
# Axis tick labels shrink too: at Plotly's default 12px the y-axis gutter eats
# ~40px of a ~390px screen; the DS mobile chart spec reads axes at 9px.
TICK_FONT_MOBILE = 9
HOVERLABEL = dict(
    bgcolor=SURFACE_PAGE,           # neutral-950 — DS chart-tooltip surface
    bordercolor=BORDER,             # neutral-800 — DS border
    font=dict(
        family="'Instrument Sans', sans-serif",
        size=HOVER_FONT_DESKTOP,
        color=TEXT_PRIMARY,         # neutral-50 — primary text
    ),
    namelength=-1,
    align="left",
)


def hover_wrap(template: str) -> str:
    """Phone-narrow a hover string by breaking ` · ` separators onto new lines.

    A composite unified-hover line ("Ingresos $416G · YoY +6% · reported") is
    wider than a ~390px phone box, so Plotly clips it off the left viewport edge.
    Stacking the segments keeps the box inside the screen; desktop keeps the
    compact single line. Only the literal ` · ` is touched, so Plotly `%{...}`
    format tokens are left intact.
    """
    return template.replace(" · ", "<br>") if is_mobile() else template

# Trimmed Plotly modebar: keep box zoom + reset, drop the rest.
PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": [
        "toImage",
        "pan2d",
        "zoomIn2d",
        "zoomOut2d",
        "autoScale2d",
        "select2d",
        "lasso2d",
    ],
}


def is_mobile() -> bool:
    """True when the request comes from a phone browser.

    Server-side detect — Streamlit exposes the request headers but no
    viewport API, and every mainstream phone browser sends "Mobi" in its
    User-Agent. Drives the layout switches: sidebar controls move into the
    main area, metric rows wrap instead of stacking, chart axes get pinned
    so touch-drag scrolls the page.
    """
    try:
        return "Mobi" in (st.context.headers.get("User-Agent") or "")
    except Exception:
        return False


def show_chart(fig, *, key: str | None = None, container=None) -> None:
    """st.plotly_chart with a touch-safe config on phones.

    Mobile: both axes pinned (fixedrange) so a drag scrolls the page instead
    of zooming the chart, scroll-zoom off, modebar hidden — taps still raise
    hover tooltips. Desktop: the shared trimmed-modebar config unchanged.
    """
    box = container or st
    mobile = is_mobile()
    # Phone screens are ~390px wide: the 15px desktop tooltip font makes a
    # multi-row hover box (EPS / net income / revenue) overflow the viewport.
    # Shrink the type on mobile so the box fits; desktop keeps the roomier 15px.
    # (Composite lines are also stacked via hover_wrap at the trace level.)
    hoverlabel = (
        {**HOVERLABEL, "font": {**HOVERLABEL["font"], "size": HOVER_FONT_MOBILE}}
        if mobile
        else HOVERLABEL
    )
    # Transparent canvas so the chart takes on the surface behind it (the
    # .topstocks-card SURFACE_CARD) instead of Streamlit's opaque
    # page-background paper — otherwise the plot reads as a darker box inset
    # in the card. Card-less contexts inherit the page bg, still correct.
    fig.update_layout(
        hoverlabel=hoverlabel,
        paper_bgcolor=TRANSPARENT,
        plot_bgcolor=TRANSPARENT,
        modebar={"bgcolor": TRANSPARENT},
    )
    if mobile:
        fig.update_xaxes(fixedrange=True, tickfont=dict(size=TICK_FONT_MOBILE))
        fig.update_yaxes(fixedrange=True, tickfont=dict(size=TICK_FONT_MOBILE))
        config = {**PLOTLY_CONFIG, "scrollZoom": False, "displayModeBar": False}
    else:
        config = PLOTLY_CONFIG
    box.plotly_chart(fig, config=config, key=key)


def metric_cells(n: int, *, width: int = 110) -> list:
    """`n` side-by-side metric slots that survive phone widths.

    Desktop: plain st.columns(n). Mobile: st.columns stack full-width below
    640px (nine metrics become nine screens of scrolling), so instead return
    fixed-width children of a wrapping horizontal container — tiles flow
    2-3 per row and the content stays above the fold.
    """
    if not is_mobile():
        return list(st.columns(n))
    row = st.container(horizontal=True, gap="small")
    return [row.container(width=width) for _ in range(n)]


def chart_layout(
    *,
    title: str | None = None,
    top_legend: bool = False,
    height: int = 260,
) -> dict:
    """Plotly layout kwargs with title and horizontal legend in separate bands.

    A layout `title` and an `orientation="h"` legend both render inside the
    top margin; with a tight margin they print on top of each other. This
    pins the title to the canvas top, anchors the legend directly above the
    plot area, and sizes the top margin so each gets its own band.

    Splat into update_layout before chart-specific keys:
        fig.update_layout(**chart_layout(title=..., top_legend=True), ...)
    """
    # On phones the chart is ~390px wide: a long title wraps to two lines and a
    # horizontal legend with long entries wraps to three rows. Both render in the
    # top margin, so the single-line bands below would let them collide (title
    # over legend). Reserve taller bands on mobile so each clears the other.
    mobile = is_mobile()
    top = 8
    layout: dict = {"height": height}
    if title:
        top += 52 if mobile else 34
        layout["title"] = dict(
            text=title, x=0, xanchor="left", y=1, yanchor="top", pad=dict(t=8)
        )
    if top_legend:
        top += 60 if mobile else 26
        # DS chart spec: legend entries read 12px in secondary text.
        layout["legend"] = dict(
            orientation="h", yanchor="bottom", y=1.0, x=0,
            font=dict(size=12, color=TEXT_SECONDARY),
        )
    layout["margin"] = dict(l=0, r=0, t=top, b=0)
    if mobile:
        # DS mobile chart spec: a finger drag drives the crosshair and the
        # reading row (app.py's touch bridge), so nothing may pan under it.
        layout["dragmode"] = False
    return layout


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


# Earnings-calendar grid, shared by the Home mini-grid and the full Earnings
# page. Both render the same component — logo chips in day cells, today
# highlighted, out-of-range days sunken, past prints as beat/miss chips — and
# each page previously carried its own copy of the stylesheet under a different
# class prefix. The copies had already drifted: only one of them ordered
# `.past:hover` after `.beat`/`.miss`, so a clickable result chip took the
# purple hover on one page and ignored it on the other. One builder, two
# density presets.
CAL_DENSITIES = {
    # Home's 4-week mini grid: five weekday columns in a narrow card column.
    "compact": {
        "head_size": FS_2XS, "head_pad": "0.2rem 0.4rem",
        "daynum_size": FS_2XS, "daynum_gap": "0.15rem",
        "chip_display": "inline-flex", "chip_margin": "0 0.15rem 0.15rem 0",
        "chip_gap": "0.25rem", "chip_pad": "0.06rem 0.28rem",
        "chip_size": FS_2XS, "chip_leading": "1.4", "logo_px": "13px",
    },
    # The Earnings page's full month grid: seven columns, page width.
    "regular": {
        "head_size": FS_2XS, "head_pad": "0.3rem 0.4rem",
        "daynum_size": FS_XS, "daynum_gap": "0.2rem",
        "chip_display": "flex", "chip_margin": "0.12rem 0",
        "chip_gap": "0.3rem", "chip_pad": "0.1rem 0.28rem",
        "chip_size": FS_2XS, "chip_leading": "1.3", "logo_px": "16px",
    },
}


def calendar_css(
    prefix: str, *, density: str, cell_height: str, cell_width: str
) -> str:
    """Stylesheet for one earnings-calendar grid, class-prefixed.

    `prefix` namespaces every class (`mini` -> `.mini-cal`, `.mini-chip`), so
    two grids can coexist on one page. `density` picks a preset from
    CAL_DENSITIES; only the cell box differs per page beyond that, since the
    column count drives it. Colors come from the tokens above — the grid is
    rendered inside a CCv2 shadow root, which is why the font-family falls back
    through Streamlit's own `--st-font` rather than inheriting.
    """
    d = CAL_DENSITIES[density]
    return f"""
  .{prefix}-cal {{
    width:100%; border-collapse:separate; border-spacing:4px;
    table-layout:fixed;
  }}
  .{prefix}-cal th {{
    font-size:{d["head_size"]}; text-transform:uppercase; letter-spacing:.06em;
    color:{TEXT_MUTED}; font-weight:600; padding:{d["head_pad"]};
    text-align:left;
  }}
  .{prefix}-cal td {{
    border:1px solid {BORDER}; border-radius:{RADIUS_SM}; vertical-align:top;
    width:{cell_width}; height:{cell_height}; padding:0.3rem 0.35rem;
  }}
  .{prefix}-cal td.dim {{ background:{SURFACE_SUNKEN}; }}
  .{prefix}-cal td.today {{
    background:{PURPLE_900}; border-color:{BRAND_ACCENT};
  }}
  .{prefix}-daynum {{
    font-size:{d["daynum_size"]}; color:{TEXT_FAINT}; font-weight:600;
    margin-bottom:{d["daynum_gap"]};
  }}
  .{prefix}-cal td.today .{prefix}-daynum {{ color:{PURPLE_400}; }}
  .{prefix}-chip {{
    display:{d["chip_display"]}; align-items:center; gap:{d["chip_gap"]};
    margin:{d["chip_margin"]}; padding:{d["chip_pad"]};
    border-radius:{RADIUS_XS}; background:{PURPLE_800};
    font-size:{d["chip_size"]}; font-weight:600;
    line-height:{d["chip_leading"]}; max-width:100%; color:{PURPLE_300};
    font-family:var(--st-font, inherit);
  }}
  .{prefix}-chip.soon {{ background:{LOSS_BAND}; color:{DOWN_COLOR}; }}
  .{prefix}-chip.beat {{ background:{PROFIT_BAND}; color:{UP_COLOR}; }}
  .{prefix}-chip.miss {{ background:{LOSS_BAND}; color:{DOWN_COLOR}; }}
  .{prefix}-chip img {{
    width:{d["logo_px"]}; height:{d["logo_px"]};
    border-radius:{RADIUS_XS}; object-fit:contain;
  }}
  .{prefix}-chip span {{
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }}
  /* Last, so the hover state wins on a clickable chip whichever verdict
     class it also carries. */
  .{prefix}-chip.past {{ cursor:pointer; }}
  .{prefix}-chip.past:hover {{
    background:{PURPLE_700}; color:{TEXT_PRIMARY};
  }}
"""


# One look for every HTML-rendered ticker table (Positions, Realized & tax,
# earnings lists, screener, import previews) — keep them identical.
_TABLE_STYLES = [
    {"selector": "", "props": [
        ("width", "100%"), ("border-collapse", "collapse"),
        ("font-size", FS_MD),
    ]},
    {"selector": "th", "props": [
        ("text-align", "left"), ("padding", "8px 12px"),
        ("border-bottom", f"1px solid {BORDER}"),
        ("font-weight", "500"), ("font-size", FS_SM),
        ("color", TEXT_MUTED),
    ]},
    {"selector": "td", "props": [
        ("padding", "7px 12px"), ("white-space", "nowrap"),
        ("border-bottom", f"1px solid {RULE_SOFT}"),
    ]},
    # DS row hover: the whole row washes SURFACE_HOVER at 100ms — no text or
    # shadow change (spec section 08).
    {"selector": "tbody tr", "props": [
        ("transition", "background 100ms ease-in-out"),
    ]},
    {"selector": "tbody tr:hover", "props": [
        ("background", SURFACE_HOVER),
    ]},
    # Touch: same wash while the row is pressed (hover never fires there).
    {"selector": "tbody tr:active", "props": [
        ("background", SURFACE_HOVER),
    ]},
    {"selector": "td a:hover b", "props": [
        ("text-decoration", "underline"),
    ]},
]


# Extra look for click-to-sort tables: headers read as controls and the active
# column carries its direction arrow (set by app.py's sorter as data-ag-dir).
_SORT_STYLES = [
    {"selector": "th.col_heading", "props": [
        ("cursor", "pointer"), ("user-select", "none"),
        ("white-space", "nowrap"),
    ]},
    {"selector": "th.col_heading:hover", "props": [("color", TEXT_PRIMARY)]},
    # The active column brightens; its arrow is a real span the sorter adds,
    # not a ::after — DOMPurify scrubs the style block st.html renders and a
    # dropped `content` declaration would leave the sort direction invisible.
    {"selector": "th[data-ag-dir]", "props": [("color", TEXT_PRIMARY)]},
    {"selector": "th .ag-arrow", "props": [
        ("color", BRAND_ACCENT), ("font-size", FS_SM),
    ]},
]


def signed_color(v, *, muted: bool = False) -> str:
    """CSS for a signed number: profit green above 0, loss red below, muted
    grey for a flat 0 (e.g. market not yet open — a zero is neutral, not a
    gain), nothing for NaN/non-numbers (Styler .map callback).

    `muted=True` dims the green/red to their off-session tints, marking a day
    change that isn't a live regular-session tick (market closed → last close).
    """
    try:
        if pd.isna(v):
            return ""
        f = float(v)
        if f == 0:
            return f"color: {TEXT_MUTED}"
        up, down = (
            (PROFIT_COLOR_MUTED, LOSS_COLOR_MUTED)
            if muted
            else (PROFIT_COLOR, LOSS_COLOR)
        )
        return f"color: {up}" if f > 0 else f"color: {down}"
    except (TypeError, ValueError):
        return ""


def _neutral_zero_formatter(template: str):
    """Wrap a signed format template ("{:+.1%}", "€{:+,.0f}") so an exact 0
    renders without the leading "+" — a flat/market-closed value shows as a
    plain "0.0%"/"€0", pairing with signed_color's muted grey."""
    plain = template.replace(":+", ":")

    def fmt(v) -> str:
        try:
            if pd.isna(v):
                return "n/a"
            return plain.format(v) if float(v) == 0 else template.format(v)
        except (TypeError, ValueError):
            return template.format(v)

    return fmt


def _value_formatter(fmt: dict[str, str] | None, signed: tuple[str, ...], col: str):
    """Formatter for one column's raw value, matching what the Styler would
    render for it (signed columns drop the "+" on an exact 0, NaN reads
    "n/a") — pair cells are built as HTML before the Styler runs, so they
    have to format their own numbers."""
    template = (fmt or {}).get(col, "{}")
    if col in signed:
        return _neutral_zero_formatter(template)

    def plain(v) -> str:
        try:
            if pd.isna(v):
                return "n/a"
        except (TypeError, ValueError):
            pass
        try:
            return template.format(v)
        except (TypeError, ValueError):
            return str(v)

    return plain


def _delta_chip(v, text: str, *, muted: bool = False) -> str:
    """Percentage as a tinted pill — green gain, red loss, grey flat, bare
    text when there's no number. Pairs an absolute figure with its relative
    one inside a single cell (see ticker_table_html's `pairs`)."""
    try:
        f = None if pd.isna(v) else float(v)
    except (TypeError, ValueError):
        f = None
    if f is None:
        return f'<span style="color:{TEXT_MUTED};font-size:{FS_XS}">{text}</span>'
    if f == 0:
        bg, fg = SURFACE_SUNKEN, TEXT_MUTED
    elif f > 0:
        bg, fg = PROFIT_BAND, (PROFIT_COLOR_MUTED if muted else PROFIT_COLOR)
    else:
        bg, fg = LOSS_BAND, (LOSS_COLOR_MUTED if muted else LOSS_COLOR)
    return (
        f'<span style="display:inline-block;padding:1px 6px;'
        f"border-radius:{RADIUS_PILL};background:{bg};color:{fg};"
        f'font-size:{FS_XS};font-weight:600;line-height:1.5">{text}</span>'
    )


def _pair_cell(value_html: str, chip_html: str) -> str:
    """One cell holding "€+3,210  (+3.0%)" — the absolute figure and its pill,
    kept on one line and pushed to the cell's right edge."""
    return (
        '<span style="display:inline-flex;align-items:center;gap:6px;'
        'justify-content:flex-end;white-space:nowrap">'
        f"{value_html}{chip_html}</span>"
    )


def _sort_key(v) -> str:
    """One cell's machine-sortable value: a bare number for anything numeric,
    lowercased text otherwise, "" for missing (the client sorts blanks last
    either way). Read off the RAW frame, so the click-sort never has to parse
    "€8,372" or a merged "€-97 (-1.1%)" cell back into a number."""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        return repr(float(v))
    try:
        return repr(float(v))  # numpy scalars, Decimal, numeric strings
    except (TypeError, ValueError):
        return str(v).strip().lower()


def _with_sort_keys(markup: str, uuid: str, keys: list[list[str]]) -> str:
    """Stamp data-s="<raw value>" on every body cell of a Styler table.

    Styler can set cell classes but not arbitrary attributes, so the keys are
    injected into the rendered HTML by cell id (`T_<uuid>_row<r>_col<c>`, the
    one handle pandas guarantees per cell). The client sorter reads data-s and
    never sees the formatted text.
    """
    for r, row in enumerate(keys):
        for c, key in enumerate(row):
            token = f'id="T_{uuid}_row{r}_col{c}"'
            markup = markup.replace(
                token, f'{token} data-s="{html.escape(key, quote=True)}"', 1
            )
    return markup


# Verdict chip palette: verdict() speaks in Streamlit color names, the DS in
# tokens. One map, so a band's color lands the same in every chip.
_VERDICT_FILL = {
    "green": (PROFIT_BAND, UP_COLOR),
    "orange": (WARN_BAND, WARN_COLOR),
    "red": (LOSS_BAND, DOWN_COLOR),
    "gray": (SURFACE_SUNKEN, TEXT_MUTED),
}

_KPI_CSS = f"""<style>
.ag-kpis {{
  display: grid; gap: 8px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}}
.ag-kpi {{
  background: {SURFACE_PAGE}; border: 1px solid {BORDER};
  border-radius: {RADIUS_MD}; padding: 9px 11px;
  display: flex; flex-direction: column; gap: 3px; min-width: 0;
}}
.ag-kpi-h {{ display: flex; align-items: center; gap: 4px; }}
.ag-kpi-l {{
  font-size: {FS_SM}; font-weight: 500; color: {TEXT_SECONDARY};
  line-height: 1.25;
}}
.ag-kpi-q {{
  flex: none; width: 14px; height: 14px; border-radius: {RADIUS_PILL};
  border: 1px solid {BORDER}; color: {TEXT_MUTED}; cursor: help;
  font-size: {FS_2XS}; line-height: 1;
  display: inline-flex; align-items: center; justify-content: center;
}}
.ag-kpi-r {{ display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }}
.ag-kpi-v {{
  font-family: 'Epilogue', 'Instrument Sans', sans-serif;
  font-weight: 700; font-size: {FS_XL}; line-height: 1.1;
  color: {TEXT_PRIMARY};
}}
.ag-kpi-c {{
  font-size: {FS_XS}; font-weight: 600; white-space: nowrap;
  padding: 1px 7px; border-radius: {RADIUS_PILL};
}}
</style>"""


def kpi_grid_html(
    tiles: list[tuple[str, str, tuple[str, str] | None, str | None]],
) -> str:
    """KPI tiles as ONE self-contained grid: label, value, verdict chip.

    The Streamlit version of this block (st.metric + st.caption in a bordered
    column) could not keep a verdict with its number: in a wrapping metric row
    the caption printed above the NEXT tile's label, and Streamlit under-sizes
    those fixed-width flex boxes, so even a bordered container had the caption
    escaping below its own edge. Rendering the whole grid as one HTML element
    takes Streamlit's layout out of the question — and puts the verdict on the
    value's line, where it can't be read as belonging to anything else.

    `tiles` are (label, formatted value, verdict, tooltip) — verdict as
    returned by analysis.fundamentals.verdict (text, Streamlit color) or None
    for a KPI with no band. The tooltip rides a native `title`, the one hover
    hint that survives without Streamlit's help popover.
    """
    cells = []
    for label, value, verdict, tip in tiles:
        head = f'<span class="ag-kpi-l">{html.escape(label)}</span>'
        if tip:
            head += (
                f'<span class="ag-kpi-q" title="{html.escape(tip, quote=True)}">'
                "?</span>"
            )
        row = f'<span class="ag-kpi-v">{html.escape(value)}</span>'
        if verdict:
            fill, ink = _VERDICT_FILL.get(verdict[1], (SURFACE_SUNKEN, TEXT_MUTED))
            row += (
                f'<span class="ag-kpi-c" style="background:{fill};color:{ink}">'
                f"{html.escape(verdict[0])}</span>"
            )
        cells.append(
            f'<div class="ag-kpi"><div class="ag-kpi-h">{head}</div>'
            f'<div class="ag-kpi-r">{row}</div></div>'
        )
    return f'{_KPI_CSS}<div class="ag-kpis">{"".join(cells)}</div>'


def kpi_delta_chip(
    pct: float | None, fmt: str = "{:+.1%}", off: bool = False
) -> tuple[str, str] | None:
    """A signed percentage as a `kpi_grid_html` verdict chip — the same pill
    the Ticker fundamentals wear — colored by sign, grey when the reading is
    stale (the st.metric delta_color="off" equivalent)."""
    if pct is None:
        return None
    return fmt.format(pct), "gray" if off else ("green" if pct >= 0 else "red")


def ticker_cell(ticker: str, *, name: bool = True, link: bool = True) -> str:
    """Logo + bold ticker (+ dim company name) as one HTML table cell.

    Wrapped in a plain anchor to the Ticker page: `ticker?ticker=SYM` is
    resolved against the current page's directory, so it lands on /ticker
    from any page (subpath deployments included), and the Ticker page reads
    the query param to select the company.
    """
    img = (
        f'<img src="{html.escape(src, quote=True)}" loading="lazy" '
        'style="height:22px;width:22px;object-fit:contain;'
        'border-radius:var(--ag-radius-xs);vertical-align:-6px;margin-right:8px">'
        if (src := logo(ticker))
        else '<span style="display:inline-block;width:30px"></span>'
    )
    label = company_name(ticker) if name else None
    tail = (
        f' <span style="opacity:.65">— {html.escape(label)}</span>' if label else ""
    )
    body = f"{img}<b>{html.escape(ticker)}</b>{tail}"
    if not link:
        return body
    return (
        f'<a href="ticker?ticker={quote(ticker)}" target="_self" '
        f'style="text-decoration:none;color:inherit">{body}</a>'
    )


def ticker_pill_md(ticker: str, max_name: int = 18) -> str:
    """Markdown label for selection widgets that render option Markdown
    (st.pills / st.segmented_control): logo as an icon-sized image, bold
    symbol, dim company name. Dropdown widgets (multiselect/selectbox) render
    options as plain text — this helper is wasted on them."""
    src = logo(ticker)
    img = f"![logo]({src}) " if src else ""
    name = company_name(ticker)
    if name and name.upper() != ticker.upper():
        if len(name) > max_name:
            name = name[: max_name - 1].rstrip() + "…"
        tail = f" :gray[{name}]"
    else:
        tail = ""
    return f"{img}**{ticker}**{tail}"


# Live search input (CCv2). Streamlit's st.text_input only reruns on Enter/blur,
# so it can't drive an as-you-type dropdown. This tiny bidirectional component
# streams the field's value to Python on every keystroke (debounced ~160ms) via
# setStateValue; Python echoes it back through `data` so the cursor is never
# fought. Styled in its own shadow root to match the old field.
#
# Registered on first mount, NOT at import: server.py imports this module at
# ASGI boot (via landing_static) before the Streamlit runtime exists, and a
# registration made then lands in a throwaway local manager — every later
# mount would raise "Component 'topstocks_live_search' is not registered".
# The first mount always happens inside a script run, where the runtime's
# registry is live; cached so the name is registered once per process.
# Magnifier glyph for the collapsed mobile search state (spliced into the
# component CSS below — CSS url() forbids line breaks, hence one long
# line). var() can't reach inside a data URI; the stroke is TEXT_MUTED.
_SEARCH_GLYPH_URI = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23827F8C' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'/%3E%3C/svg%3E"  # noqa: E501
_SEARCH_GLYPH_CSS = f'background-image: url("{_SEARCH_GLYPH_URI}");'
_LIVE_SEARCH = None


def _live_search_component():
    global _LIVE_SEARCH
    if _LIVE_SEARCH is not None:
        return _LIVE_SEARCH
    _LIVE_SEARCH = st.components.v2.component(
        "topstocks_live_search",
        html=(
            '<div class="lsw">'
            '<input id="q" class="lsi" type="text"'
            ' autocomplete="off" spellcheck="false" />'
            '<span id="spin" class="lss"></span>'
            "</div>"
        ),
        css="""
    .lsw { position: relative; display: block; }
    /* Busy dot on the field's right edge, shown from the keystroke until
       Python echoes that exact value back (see the JS ack below). The gutter
       is reserved permanently so the query text never jumps when it appears. */
    .lss {
      position: absolute; right: 10px; top: 50%; width: 14px; height: 14px;
      margin-top: -7px; box-sizing: border-box; border-radius: 50%;
      border: 2px solid var(--ag-border);
      border-top-color: var(--ag-brand-accent);
      opacity: 0; transition: opacity 120ms ease; pointer-events: none;
    }
    .lss.on { opacity: 1; animation: lsspin 0.7s linear infinite; }
    @keyframes lsspin { to { transform: rotate(360deg); } }
    .lsi {
      width: 100%; box-sizing: border-box; height: 36px;
      padding: 0 1.75rem 0 0.75rem;
      /* height set again below for phones — 44px DS touch target */
      background: var(--ag-surface-card); color: var(--ag-text-primary);
      border: 1px solid var(--ag-border);
      border-radius: var(--ag-radius-sm); font-size: var(--ag-fs-sm);
      outline: none;
    }
    .lsi::placeholder { color: var(--ag-text-muted); }
    .lsi:focus { border-color: var(--ag-brand-accent); }
    @media (max-width: 640px) {
      .lsi { height: 44px; }
      /* Collapsed 44px icon state (host width is set by the page CSS):
         magnifier glyph, no visible text until focus expands the field.
         var() can't reach inside a data URI — stroke is TEXT_MUTED. */
      .lsi:not(:focus):placeholder-shown {
        color: transparent;
        /* Drop the spinner gutter while collapsed: background-position centers
           on the PADDING box, so the asymmetric padding would sit the
           magnifier 8px left of the 44px button's middle. The field is empty
           in this state, so there is no text to shift. */
        padding: 0;
        /*SEARCH-GLYPH*/
        background-repeat: no-repeat;
        background-position: center;
        background-size: 18px 18px;
      }
      .lsi:not(:focus):placeholder-shown::placeholder { color: transparent; }
    }
    """.replace("/*SEARCH-GLYPH*/", _SEARCH_GLYPH_CSS),
        js="""
export default function (component) {
  const { parentElement, data, setStateValue } = component
  const input = parentElement.querySelector("#q")
  if (!input) return
  // Busy state = "the field shows a query Python has not answered yet". Set on
  // the keystroke itself (before the debounce even fires) and cleared only by
  // the ack below, so the whole dead window — debounce, a keystroke rerun that
  // died behind a full app run, every 250-800ms retry — is visibly loading
  // instead of looking like the field ate the query.
  const spin = parentElement.querySelector("#spin")
  const busy = (on) => spin && spin.classList.toggle("on", !!on)
  input.placeholder = (data && data.placeholder) || ""
  const nextValue = (data && data.value) ?? ""
  // Only overwrite the field when the user isn't typing in it — a render whose
  // run started before the last keystroke echoes the stale value and would
  // wipe the in-progress query.
  if (input.value !== nextValue && !input.matches(":focus")) input.value = nextValue
  if (input.value === nextValue) {
    // Python echoed exactly what the field shows: the keystroke landed, so
    // stop re-asserting it. The results (or the server-side "searching" row
    // this same run draws under the field) take over the feedback from here.
    clearTimeout(input._retry)
    input._retryN = 0
    busy(false)
  } else {
    busy(input.value.trim().length > 0)
  }
  if (data && data.blur) {
    // A row click navigated. Clearing only the DOM input is not enough: the
    // frontend widget manager re-sends its stored "value" with every rerun,
    // so the old query would resurrect the dropdown. Sync the clear into
    // widget state and drop any pending debounce/retry still holding it.
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    busy(false)
    input.value = ""
    setStateValue("value", "")
    setStateValue("focused", false)
    input.blur()
  }
  if (!input.dataset.wired) {
    input.dataset.wired = "1"
    // A keystroke's fragment-rerun request dies when a full app run is in
    // flight (the run cleared the fragment ids, or fastReruns replaced the
    // ScriptRunner holding the queued request) — the value never reaches
    // Python and the dropdown stays closed even though the field shows the
    // query. Re-assert until a render echoes it back (the ack above); bumping
    // "nonce" defeats same-value dedup so each retry still forces a rerun.
    // Backing off 250ms → 800ms over 14 tries (~9s total) still outlasts the
    // slowest throttled-Yahoo page run, but recovers in a quarter of a second
    // when the blocking run was short — the flat 800ms made every miss feel
    // like a dead field.
    const send = (v) => {
      setStateValue("value", v)
      clearTimeout(input._retry)
      if ((input._retryN = (input._retryN || 0) + 1) > 14) return
      const wait = Math.min(800, 250 * Math.pow(1.25, input._retryN - 1))
      input._retry = setTimeout(() => {
        setStateValue("nonce", (input._nonce = (input._nonce || 0) + 1))
        send(input.value)
      }, wait)
    }
    input.addEventListener("input", (e) => {
      clearTimeout(input._timer)
      input._retryN = 0
      const v = e.target.value
      busy(v.trim().length > 0)
      input._timer = setTimeout(() => send(v), 160)
    })
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(input._timer); input._retryN = 0
        busy(e.target.value.trim().length > 0); send(e.target.value)
      }
    })
    // Report focus so Python can show recent searches on an empty, focused
    // field. Blur is delayed so a click on a dropdown row (a Streamlit button
    // in the parent document, outside this shadow root) registers before the
    // rerun that hides the list would tear the button out from under it.
    input.addEventListener("focus", () => {
      clearTimeout(input._blurTimer)
      setStateValue("focused", true)
    })
    input.addEventListener("blur", () => {
      clearTimeout(input._blurTimer)
      input._blurTimer = setTimeout(() => setStateValue("focused", false), 200)
    })
  }
  // Close the dropdown the instant a row is clicked. The server-side close
  // only lands after the app rerun + ticker page load (seconds), so the stale
  // list would linger on screen the whole time. Listen in the bubble phase so
  // Streamlit's own button handler dispatches its click event FIRST — hiding
  // before that would race the navigation. Re-attached every render so the
  // closure always points at the live input/setStateValue.
  const doc = input.ownerDocument
  if (doc.__lsRowCloser) doc.removeEventListener("click", doc.__lsRowCloser)
  doc.__lsRowCloser = (e) => {
    // The container's key carries a generation counter (see _go_ticker), so
    // match on the prefix and take whichever one holds the clicked row.
    const results = [...doc.querySelectorAll('[class*="st-key-topbar_results"]')]
      .find((el) => el.contains(e.target))
    if (!results) return
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    clearTimeout(input._blurTimer)
    results.style.display = "none"
    input.value = ""
    setStateValue("value", "")
    // The row's mousedown already blurred the field, so the blur listener's
    // pending focused=false was the only one — and the clearTimeout above just
    // killed it; input.blur() re-fires nothing. Say it outright, or "focused"
    // stays true and the recents dropdown re-opens on the page we land on.
    setStateValue("focused", false)
    input.blur()
  }
  doc.addEventListener("click", doc.__lsRowCloser)
}
""",
    )
    return _LIVE_SEARCH


def _live_search_input(*, key: str, placeholder: str) -> tuple[str, bool]:
    """Mount the live-search field; return its (stripped value, focused?)."""
    state = st.session_state.get(key)
    value = state.get("value", "") if isinstance(state, dict) else ""
    focused = bool(state.get("focused")) if isinstance(state, dict) else False
    blur = bool(st.session_state.pop(f"{key}_blur", False))
    if blur:
        # The frontend may re-send the old query with this rerun, resurrecting
        # it in session state; echo an empty value so the JS clears the field.
        value = ""
    result = _live_search_component()(
        key=key,
        data={"value": value, "placeholder": placeholder, "blur": blur},
        width="stretch",
        on_value_change=lambda: None,
        on_focused_change=lambda: None,
        # "nonce" exists only to force a rerun: the JS bumps it when it retries
        # a keystroke the server never acked (same "value" would be deduped).
        on_nonce_change=lambda: None,
    )
    if blur:
        # Just navigated from a row click: the browser input still reports focus
        # (blur is debounced), which would re-open the recents dropdown. Force it
        # closed for this render; the JS above drops the real focus to match.
        return "", False
    return (
        (getattr(result, "value", "") or "").strip(),
        bool(getattr(result, "focused", focused)),
    )


def _fuzzy_order(
    q: str,
    tickers: list[str],
    labels: dict[str, str],
    tag_map: dict[str, tuple],
) -> list[str]:
    """Tickers whose symbol, name or tag fuzzy-matches `q`, best score first.

    Typo fallback shared by the top-bar dropdown and the drawer picker; both
    call it only after exact substring matching came up empty.
    """
    if len(q) < MIN_QUERY:
        return []
    scored = []
    for i, t in enumerate(tickers):  # ties keep list order (favorites first)
        score = max(
            fuzzy_ratio(q, t.upper()),
            fuzzy_ratio(q, labels[t].upper()),
            *(fuzzy_ratio(q, tag.upper()) for tag in tag_map.get(t, ())),
        )
        if score >= FUZZY_CUTOFF:
            scored.append((-score, i, t))
    return [t for _, _, t in sorted(scored)]


def _topbar_matches(raw: str):
    """Picker-parity search for the top-bar dropdown.

    Mirrors the left-drawer `ticker_picker` exactly: the watchlist is matched by
    symbol, company name OR tag-group (favorites first, and open-but-unlisted
    positions from the ledger folded in), then coins, then the SEC ticker map,
    then a worldwide Yahoo lookup for everything the US-only map can't see,
    plus an "Analyze <SYMBOL>" fallback for a symbol none of them know. Returns
    `(watch, crypto, funds, sec, world, analyze)` where watch rows carry their
    star/briefcase mark and world rows carry their exchange.
    """
    q = raw.strip().upper()
    if not q:
        return [], [], [], [], [], None
    holdings = load_watchlist(auth.watchlist_path())
    labels = {h.ticker: (h.name or h.ticker) for h in holdings}
    fav_set = {h.ticker for h in holdings if h.favorite}
    tag_map = {h.ticker: h.tags for h in holdings if h.tags}
    db = str(auth.db_path())
    held_set = set(held_tickers(db, db_mtime(db)))
    for t in sorted(held_set - set(labels)):
        labels[t] = sec_title(t) or t
    order = [t for t in labels if t in fav_set] + [t for t in labels if t not in fav_set]

    watch: list[tuple[str, str, str]] = []
    for t in order:
        if (
            q in t.upper()
            or q in labels[t].upper()
            or any(q in tag.upper() for tag in tag_map.get(t, ()))
        ):
            mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
            watch.append((t, labels[t], mark))
    if not watch:
        # Typo fallback ("oracel"): fuzzy over the same fields, best first.
        # Only when exact substring found nothing, so it never dilutes results.
        for t in _fuzzy_order(q, order, labels, tag_map):
            mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
            watch.append((t, labels[t], mark))

    from stocks.data.crypto import search_crypto
    from stocks.data.funds import search_funds

    crypto = [(t, n) for t, n in search_crypto(q) if t not in labels]
    # The fund catalog is local, so this tier is the one that still answers
    # "where is my ETF" while Yahoo has the deploy's egress IP in timeout.
    funds = [(t, n) for t, n in search_funds(q) if t not in labels]
    sec = [(t, n) for t, n in sec_matches(q) if t not in labels]
    # Worldwide runs on every query, not just when the tiers above came up
    # empty: their fuzzy fallbacks always produce SOMETHING, so "nothing found
    # locally" is not a usable trigger — "MIPS" pulls VIPS/CMPS/MVIS out of the
    # SEC map and would have suppressed the one real answer (MIPS.ST). It is
    # deduped against them instead, and `_world_first` decides which of the two
    # groups leads.
    seen = (
        set(labels)
        | {t for t, _ in crypto}
        | {t for t, _ in funds}
        | {t for t, _ in sec}
    )
    world = [(t, n, x) for t, n, x in world_matches(q) if t not in seen]
    known = seen | {t for t, _, _ in world}
    analyze = q if (q not in known and re.fullmatch(r"[A-Z0-9.\-]{1,12}", q)) else None
    return watch[:8], crypto[:4], funds[:4], sec[:6], world[:3], analyze


def _recent_rows() -> list[tuple[str, str, str]]:
    """Recently explored tickers as `(symbol, name, mark)`, newest first.

    Names/marks are resolved from the current watchlist + ledger like the
    live matches, so a recent row looks identical to its search-result twin;
    a symbol no longer in either just shows bare.
    """
    recents = auth.load_recent_searches()
    if not recents:
        return []
    holdings = load_watchlist(auth.watchlist_path())
    labels = {h.ticker: (h.name or h.ticker) for h in holdings}
    fav_set = {h.ticker for h in holdings if h.favorite}
    db = str(auth.db_path())
    held_set = set(held_tickers(db, db_mtime(db)))
    rows = []
    for t in recents:
        name = labels.get(t) or (sec_title(t) if t in held_set else None) or ""
        mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
        rows.append((t, name if name != t else "", mark))
    return rows


def _go_ticker(ticker: str) -> None:
    """Navigate to a ticker from the top-bar dropdown.

    Reuses the picker's contract — set the shared selection and raise
    "picker_clicked" so app.py switches to the Ticker page on the rerun — then
    clear the query so the dropdown closes.
    """
    st.session_state.pop("topbar_q", None)  # reset the live input (its state is a dict)
    # The closer JS hides the open dropdown with an INLINE display:none (the
    # server-side close lands seconds later). Streamlit reuses a keyed
    # container's DOM node, and React never clears a style it didn't set, so
    # reusing that node for the next query renders the rows invisible. Bump the
    # generation: the next dropdown gets a new key, hence a new node.
    st.session_state["topbar_res_gen"] = st.session_state.get("topbar_res_gen", 0) + 1
    st.session_state["topbar_q_blur"] = True  # blur the field so recents don't re-open
    auth.push_recent_search(ticker)  # remember it for the empty-field dropdown
    st.session_state["picker_selected"] = ticker.strip().upper()
    st.session_state["picker_clicked"] = True


def _results_key() -> str:
    """Key for the dropdown container, carrying the generation counter.

    A row click hides the open dropdown from JS with an inline style; keying the
    container per generation guarantees the next dropdown is a brand-new DOM
    node, never the hidden one. CSS/JS match it with `[class*=...]`.
    """
    return f"topbar_results_{st.session_state.get('topbar_res_gen', 0)}"


def _search_row(t: str, label: str, key: str) -> None:
    """One dropdown row: a full-width button that navigates to ticker `t`."""
    st.button(label, key=key, on_click=_go_ticker, args=(t,), width="stretch")


def _render_ticker_rows(
    rows: list[tuple[str, str, str]], *, key_prefix: str = "tbres"
) -> None:
    """Render `(symbol, name, mark)` rows as logo'd buttons in the dropdown.

    Each carries its watchlist logo (CSS background, like the picker) and its
    star/briefcase mark. Logo rules are scoped under the results container so they beat
    the base row rule's specificity — otherwise its `background: transparent`
    shorthand wipes the logo back to none.
    """
    logo_rules = [
        f'[class*="st-key-topbar_results"] .st-key-{key_prefix}_{_slug(t)} button {{'
        f'background-image:url("{src}"); background-repeat:no-repeat;'
        " background-position:8px center; background-size:16px 16px;"
        " padding-left:30px;}"
        for t, _, _ in rows
        if (src := logo(t))
    ]
    if logo_rules:
        st.html("<style>" + "".join(logo_rules) + "</style>")
    for t, name, mark in rows:
        pre = f"{mark} " if mark else ""
        tail = f"  {name}" if name and name != t else ""
        _search_row(t, f"{pre}**{t}**{tail}", f"{key_prefix}_{_slug(t)}")


@st.fragment
def _topbar_search_panel() -> None:
    """Live ticker search + autocomplete dropdown, isolated in a fragment.

    Typing streams through the CCv2 field and reruns ONLY this fragment, so the
    dropdown updates as-you-type without re-running the whole page (charts and
    all). A dropdown row click sets the picker flags but — being inside the
    fragment — reruns only the fragment; we then escalate with an app-scoped
    rerun so app.py's switch_page runs. On any full rerun app.py has already
    popped the flag before this renders, so the escalation never double-fires.
    Escalate BEFORE rendering: the intermediate fragment run is discarded by the
    app rerun, so drawing (and consuming the blur flag) here would waste both.
    """
    if st.session_state.get("picker_clicked"):
        st.rerun(scope="app")
    with st.container(key="topbar_search"):
        q, focused = _live_search_input(
            key="topbar_q", placeholder=tr("widgets.search_placeholder")
        )
        if not q and is_mobile():
            # DS mobile header: an empty field is a 44px magnifier button.
            # Emitted here (not in the page stylesheet) because the dropdown
            # below is a child of this container — collapsing the host while
            # results are showing would squash them to 44px. The fragment
            # reruns as-you-type, so the rule lifts on the first keystroke.
            st.html(
                "<style>@media (max-width: 640px) {"
                ".st-key-topbar_search:not(:focus-within)"
                " { width: 44px !important; } }</style>"
            )
        if q:
            # Typed query: live matches. Recents never show here — searching
            # something else replaces them (they only stand in for an empty field).
            #
            # The panel opens BEFORE the matches are known. The last tier is a
            # network round-trip (worldwide symbols, up to a 6s timeout on a
            # cold query), so computing first and rendering after left the
            # field looking inert for seconds — the field's own busy dot is
            # already gone by then, cleared by this run's echo. A "searching"
            # row goes into the open panel and is replaced in place by the
            # rows: Streamlit flushes deltas as they are produced, so it
            # reaches the browser while the tier is still running.
            with st.container(key=_results_key()):
                pending = st.empty()
                with pending.container(key="topbar_pending"):
                    st.caption(tr("widgets.searching"))
                watch, crypto, funds, sec, world, analyze = _topbar_matches(q)
                pending.empty()
                if not (watch or crypto or funds or sec or world or analyze):
                    # Never leave the panel blank: an empty bordered box reads
                    # as "still working", which is what this whole path fixes.
                    st.caption(tr("widgets.no_results"))
                _render_ticker_rows(watch)
                if crypto:
                    st.caption(tr("widgets.crypto"))
                    for t, name in crypto:
                        _search_row(t, f"🪙 **{t}**  {name}", f"tbrescx_{_slug(t)}")
                if funds:
                    st.caption(tr("widgets.funds"))
                    for t, name in funds:
                        _search_row(t, f"🧺 **{t}**  {name}", f"tbresfd_{_slug(t)}")

                def _world_group() -> None:
                    if world:
                        st.caption(tr("widgets.from_world_search"))
                        for t, name, exch in world:
                            _search_row(
                                t, _world_label(t, name, exch), f"tbresw_{_slug(t)}"
                            )

                def _sec_group() -> None:
                    if sec:
                        st.caption(tr("widgets.from_sec_search"))
                        for t, name in sec:
                            _search_row(t, f"🔎 **{t}**  {name}", f"tbressec_{_slug(t)}")

                # Whichever of the two searched the query better goes first.
                groups = (_world_group, _sec_group)
                if not _world_first(q.strip().upper(), sec):
                    groups = groups[::-1]
                for group in groups:
                    group()
                if analyze:
                    st.button(
                        tr("widgets.analyze", q=analyze),
                        key="tbres_analyze",
                        on_click=_go_ticker,
                        args=(analyze,),
                        width="stretch",
                        type="primary",
                    )
        elif focused and (recent := _recent_rows()):
            # Empty but focused: offer the last few explored tickers.
            with st.container(key=_results_key()):
                st.caption(tr("widgets.recent"))
                _render_ticker_rows(recent, key_prefix="tbrec")


# Bottom tab bar (phones) — the DS mobile spec replaces the sidebar with a
# fixed 4-destination bar: Inicio · Cartera · Screener · Perfil. Stroke icons
# straight from the spec's Amphora set (24×24 grid, 1.5px stroke, round caps).
# The remaining pages stay reachable through the drawer behind the native
# header's menu toggle, which the bar deliberately does not remove.
_BOTTOM_NAV = (
    # (url_path, i18n label key, Material Symbols ligature — the same glyphs
    # app.py's st.navigation uses, so drawer and tab bar agree)
    ("", "nav.home", "home"),
    ("portfolio", "nav.portfolio", "pie_chart"),
    ("screener", "nav.screener", "filter_alt"),
    ("profile", "nav.profile", "account_circle"),
)


def render_bottom_nav(active_path: str) -> None:
    """Fixed bottom tab bar on phones — DS mobile spec (section 10).

    Plain anchors, like the mobile table rows: relative hrefs resolve against
    the current page's directory, so they land on the right route from any
    page and under any base path. Rendered from app.py on every page; the
    style block keeps it display:none above 640px, so desktop never sees it.
    app.py's mobile padding reserves the bar's height under the content.
    """
    items = []
    for path, key, icon in _BOTTOM_NAV:
        cls = "ts-bn-item active" if path == active_path else "ts-bn-item"
        href = path or "./"
        items.append(
            f'<a class="{cls}" href="{href}" target="_self">'
            f'<span class="ts-bn-ic">{icon}</span>'
            f"<span>{html.escape(tr(key))}</span></a>"
        )
    st.html(
        """
        <style>
        .ts-bottomnav { display: none; }
        @media (max-width: 640px) {
          .ts-bottomnav {
            position: fixed; left: 0; right: 0; bottom: 0; z-index: 999998;
            display: flex;
            background: var(--ag-surface-page);
            border-top: 1px solid var(--ag-border);
            /* env() clears the iPhone home indicator */
            padding: 6px 8px calc(10px + env(safe-area-inset-bottom, 0px));
          }
          .ts-bn-item {
            flex: 1; display: flex; flex-direction: column;
            align-items: center; gap: 3px;
            padding: 6px 0; min-height: 44px; box-sizing: border-box;
            color: var(--ag-text-muted); text-decoration: none;
            font-size: var(--ag-fs-2xs); font-weight: 500; line-height: 1.2;
          }
          .ts-bn-ic {
            font-family: "Material Symbols Rounded";
            font-size: 20px; line-height: 1; font-weight: 400;
            font-variation-settings: "FILL" 0, "wght" 300;
          }
          .ts-bn-item.active { color: var(--ag-brand-accent); font-weight: 600; }
          .ts-bn-item.active .ts-bn-ic { font-variation-settings: "FILL" 1, "wght" 300; }
          /* Touch press feedback — no hover states on phones. */
          .ts-bn-item:active { color: var(--ag-text-secondary); }
        }
        </style>
        """
        f'<nav class="ts-bottomnav">{"".join(items)}</nav>'
    )


def render_topbar(page_title: str, ticker: str | None = None) -> None:
    """Sticky breadcrumb bar pinned to the top of the main content area.

    Shows the app name + current page title, plus the focused ticker (logo,
    symbol, company name) when one is passed — a persistent "you are here"
    strip. It is `position: sticky` so it stays put while the page scrolls,
    and its z-index sits above the page content beneath it.

    Rendered from app.py on every page as the first element in the main
    column, so it inherits the main area's sidebar offset in every sidebar
    state (collapsed rail / expanded / hidden phone) without any width math.
    """
    crumbs = [
        '<span class="tb-brand">TopStocks</span>',
        '<span class="tb-sep">›</span>',
        f'<span class="tb-page">{html.escape(page_title)}</span>',
    ]
    if ticker:
        name = company_name(ticker)
        src = logo(ticker)
        img = (
            f'<img class="tb-logo" src="{html.escape(src, quote=True)}" loading="lazy">'
            if src
            else ""
        )
        tail = (
            f'<span class="tb-name">{html.escape(name)}</span>'
            if name and name.upper() != ticker.upper()
            else ""
        )
        crumbs += [
            '<span class="tb-sep">›</span>',
            f'<span class="tb-ticker">{img}<b>{html.escape(ticker)}</b>{tail}</span>',
        ]
    # The top strip is two independent pieces so all its controls line up on
    # ONE row at every width:
    #  1. A sticky breadcrumb bar (this "you are here" strip), pinned to the top
    #     of the MAIN column. Breadcrumb-only and single-line, so it never wraps
    #     or clips the way a search-in-bar row did on phones. Stickiness rides on
    #     the stElementContainer that st.html produces (a direct child of the
    #     full-height main block), singled out with `:has(.topstocks-topbar)`;
    #     negative margins bleed it to the block-container's content edges.
    #  2. A GLOBAL ticker search (below), rendered as a VIEWPORT-FIXED field that
    #     sits in the very top strip just left of the assistant launcher (the
    #     fixed chat FAB). Fixed — not in the bar's flow — so it clears the FAB
    #     and can't reflow/clip; the sidebar-menu toggle, the search and the chat
    #     button then share one row (the Streamlit header row on phones, the
    #     breadcrumb bar on desktop). The bar reserves right padding for it.
    st.html(
        """
        <style>
        /* Streamlit fixes the element container's width at 100%, so the
           negative side margins alone shift it left without widening it and
           the bar's bottom border stops short of the main column's right
           edge — the width calc adds both bled margins back so the border
           runs edge to edge. */
        [data-testid="stElementContainer"]:has(.topstocks-topbar) {
          position: sticky !important; top: 0; z-index: 100000;
          /* -1.2rem swallows the block-container top padding; the extra
             0.55rem swallows the vertical-block gap the hidden st.html
             style/script elements above the bar still contribute. */
          margin: calc(-1.2rem - 0.55rem) -2.5rem 0.4rem;
          width: calc(100% + 5rem) !important;
          max-width: calc(100% + 5rem) !important;
        }
        /* 64px tall like the design header (14px padding + 36px controls). */
        .topstocks-topbar {
          padding: 0 2.5rem; min-height: 64px;
          display: flex; align-items: center; gap: 0.5rem;
          background: var(--ag-surface-page-haze); backdrop-filter: blur(7px);
          border-bottom: 1px solid var(--ag-border);
          font-size: var(--ag-fs-md); line-height: 1.2;
          white-space: nowrap; overflow: hidden;
        }
        .topstocks-topbar .tb-brand { color: var(--ag-text-muted); font-weight: 400; }
        .topstocks-topbar .tb-sep { color: var(--ag-text-faint); }
        .topstocks-topbar .tb-page { color: var(--ag-text-primary); font-weight: 600; }
        .topstocks-topbar .tb-ticker {
          color: var(--ag-text-primary); font-weight: 600;
          display: inline-flex; align-items: center; gap: 6px;
          min-width: 0; overflow: hidden; text-overflow: ellipsis;
        }
        .topstocks-topbar .tb-name { color: var(--ag-text-muted); font-weight: 400; }
        .topstocks-topbar .tb-logo {
          height: 18px; width: 18px; object-fit: contain;
          border-radius: var(--ag-radius-xs);
        }

        /* Fixed global search: top strip, right side, clearing the chat FAB so
           menu + search + chat read as one row. Shifts left when the FAB is
           present (signed-in); sits at the edge otherwise. */
        /* Phones: centered in the native header row. Desktop overrides the
           top below to center in the 4rem breadcrumb bar. */
        .st-key-topbar_search {
          /* (3.75rem header - 44px field) / 2 = 8px — DS 44px touch target. */
          position: fixed; top: 8px; right: 1rem; z-index: 999999;
          width: min(300px, 44vw) !important; min-width: 0 !important;
        }
        @media (min-width: 641px) {
          /* (4rem - 36px) / 2 = 14px — search centered in the taller bar. */
          .st-key-topbar_search { top: 14px; }
        }
        body:has(.st-key-chatfab) .st-key-topbar_search { right: 4.25rem; }
        /* The live-search field is a CCv2 component; keep its host flush and
           tighten the block gap so the dropdown hugs it. */
        .st-key-topbar_search [data-testid="stVerticalBlock"] { gap: 0.25rem; }
        .st-key-topbar_search [data-testid="stElementContainer"] { margin: 0; }
        /* Autocomplete dropdown: a floating result panel under the field. Rows
           are borderless list buttons; clicking one navigates to the ticker. */
        [class*="st-key-topbar_results"] {
          background: var(--ag-surface-card); border: 1px solid var(--ag-border);
          border-radius: var(--ag-radius-sm);
          padding: 4px; max-height: 60vh; overflow-y: auto;
          box-shadow: var(--ag-shadow-overlay);
        }
        [class*="st-key-topbar_results"] [data-testid="stVerticalBlock"] { gap: 0.1rem; }
        [class*="st-key-topbar_results"] button {
          justify-content: flex-start; text-align: left;
          border: 0; background: transparent; color: var(--ag-text-primary);
          padding: 0.3rem 0.5rem; font-size: var(--ag-fs-sm); min-height: 0;
        }
        [class*="st-key-topbar_results"] button:hover {
          background: var(--ag-surface-hover); color: var(--ag-text-primary);
        }
        /* The button's inner flex wrapper centers its label; pin it left so the
           text sits right after the logo/emoji instead of mid-row. */
        [class*="st-key-topbar_results"] button > div { justify-content: flex-start; }
        /* Streamlit centers button labels; force them left so the text sits
           flush after the logo/emoji instead of floating mid-row. */
        [class*="st-key-topbar_results"] button [data-testid="stMarkdownContainer"] {
          width: 100%; text-align: left;
        }
        [class*="st-key-topbar_results"] button p {
          font-weight: 400; text-align: left;
        }
        [class*="st-key-topbar_results"] button strong { color: var(--ag-purple-400); }
        /* "Searching…" row, drawn while the network tier answers. Pulsing so
           it reads as work in progress rather than a result. */
        .st-key-topbar_pending [data-testid="stCaptionContainer"] p {
          animation: tb-pending 1.1s ease-in-out infinite;
        }
        @keyframes tb-pending { 50% { opacity: 0.35; } }
        /* Section captions ("crypto" / "SEC search") — small, dim, tight. */
        [class*="st-key-topbar_results"] [data-testid="stCaptionContainer"] {
          padding: 0.25rem 0.5rem 0.1rem; margin: 0;
        }
        [class*="st-key-topbar_results"] [data-testid="stCaptionContainer"] p {
          font-size: var(--ag-fs-2xs); color: var(--ag-text-muted); margin: 0;
        }
        /* Analyze-new fallback keeps the brand primary fill to read as an action. */
        [class*="st-key-topbar_results"] button[kind="primary"] {
          background: var(--ag-purple-900); border-color: var(--ag-purple-800);
          color: var(--ag-purple-300); box-shadow: none;
        }

        /* Desktop: the search overlays the bar's right, so reserve room and keep
           long breadcrumbs from sliding under it. Phones put the search up in
           the header row instead, so the bar keeps its full width there. */
        @media (min-width: 641px) {
          /* Room for the 300px search + 36px launcher riding the bar's right. */
          .topstocks-topbar { padding-right: 26rem; }
        }
        @media (max-width: 640px) {
          [data-testid="stElementContainer"]:has(.topstocks-topbar) {
            margin-left: -1rem; margin-right: -1rem;
            width: calc(100% + 2rem) !important;
            max-width: calc(100% + 2rem) !important;
          }
          .topstocks-topbar { padding-left: 1rem; padding-right: 1rem; }
          /* DS mobile header: brand mark + screen title beside the menu
             toggle, in the native header strip. Informational only —
             pointer-events off so header taps pass through. */
          .topstocks-mheader {
            position: fixed; top: 0; left: 3.25rem; height: 3.75rem;
            display: flex; align-items: center; gap: 10px;
            z-index: 999997; pointer-events: none;
            max-width: calc(100vw - 12rem); overflow: hidden;
          }
          .topstocks-mheader img {
            width: 24px; height: 24px; object-fit: contain;
          }
          .topstocks-mheader span {
            font-size: var(--ag-fs-lg); font-weight: 600;
            color: var(--ag-text-primary);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          }
          /* DS mobile header: the search collapses to a 44px icon button
             and expands over the title while focused (keyboard up). The
             collapse itself is emitted from Python (_topbar_search_panel)
             only while the field is empty — the autocomplete dropdown is a
             child of this container, so a 44px host would squash it. */
          .st-key-topbar_search { transition: width 150ms ease; }
          .st-key-topbar_search:focus-within {
            width: min(300px, 62vw) !important;
          }
        }
        </style>
        """
    )
    # Breadcrumb strip: desktop only. Phones carry the native header + page
    # heading, so a third bar there just clutters the top — but the search below
    # still renders, so the menu toggle + search + chat button share the header
    # row on phones.
    if not is_mobile():
        st.html(f'<div class="topstocks-topbar">{"".join(crumbs)}</div>')
    else:
        mark = asset_logo("topstocks-icon.svg")
        img = f'<img src="{html.escape(mark, quote=True)}" alt="">' if mark else ""
        st.html(
            f'<div class="topstocks-mheader">{img}'
            f"<span>{html.escape(page_title)}</span></div>"
        )
    # Live search + dropdown, in a fragment so typing reruns only the panel.
    _topbar_search_panel()


# Revolut-style dense rows for phones: shared look for every mobile ticker
# list. One style block per list is idempotent — several lists per page fine.
_ROWS_CSS = f"""<style>
.agr-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 8px 2px; min-height: 44px; box-sizing: border-box;
  border-bottom: 1px solid {RULE_SOFT};
  text-decoration: none; color: inherit;
}}
/* Touch: no hover states — the row washes the hover surface while pressed. */
@media (hover: none) {{
  .agr-row:active {{ background: {SURFACE_HOVER}; }}
}}
.agr-logo {{
  width: 30px; height: 30px; object-fit: contain;
  border-radius: {RADIUS_XS}; flex: none; display: inline-block;
}}
.agr-main {{ flex: 1 1 auto; min-width: 0; }}
.agr-side {{ flex: none; text-align: right; max-width: 45%; }}
.agr-l1 {{ font-size: {FS_BASE}; font-weight: 600; line-height: 1.4; }}
.agr-l2 {{
  font-size: {FS_SM}; color: {TEXT_MUTED}; line-height: 1.4;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.agr-l2.agr-wrap {{ white-space: normal; overflow: visible; }}
.agr-side .agr-l2 {{ overflow: visible; }}
</style>"""


def _ticker_rows_html(
    frame: pd.DataFrame,
    *,
    spec: dict,
    fmt: dict[str, str] | None,
    signed: tuple[str, ...],
    ticker_col: str,
    names: bool,
    muted: set[str] | frozenset[str],
    muted_cols: tuple[str, ...],
) -> str:
    """Phone rendering of a ticker table: one dense two-line row per ticker
    (Revolut-style) instead of columns, so nothing pans horizontally.

        [logo]  TICKER  (+54%)             €6,345
                Company · 9%                +1.2%

    `spec` maps columns onto the row slots (see ticker_table_html's `mobile`
    arg); fmt/signed/muted keep the exact semantics of the table renderer, so
    a phone row and its desktop cell always print the same string and color.
    """
    value_col = spec.get("value")
    delta_col = spec.get("delta")
    badge_col = spec.get("badge")
    sub_cols = tuple(spec.get("sub", ()))
    sub_labels: dict[str, str] = spec.get("sub_labels", {})

    fmt_map: dict = dict(fmt or {})
    for c in signed:
        if c in fmt_map:
            fmt_map[c] = _neutral_zero_formatter(fmt_map[c])

    def text(col: str, v) -> str:
        try:
            if pd.isna(v):
                return "n/a"
        except (TypeError, ValueError):
            pass
        f = fmt_map.get(col)
        if callable(f):
            return html.escape(f(v))
        if f:
            try:
                return html.escape(f.format(v))
            except (TypeError, ValueError):
                pass
        return html.escape(str(v))

    def colored(col: str, v, tick: str) -> str:
        s = text(col, v)
        if col in signed and (
            css := signed_color(v, muted=(tick in muted and col in muted_cols))
        ):
            return f'<span style="{css}">{s}</span>'
        return s

    rows = []
    for _, r in frame.iterrows():
        tick = str(r[ticker_col])
        img = (
            f'<img src="{html.escape(src, quote=True)}" loading="lazy" '
            'class="agr-logo">'
            if (src := logo(tick))
            else '<span class="agr-logo"></span>'
        )
        parts = []
        if names and (label := company_name(tick)):
            parts.append(html.escape(label))
        for c in sub_cols:
            if c not in frame.columns:
                continue
            v = r[c]
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                pass
            item = colored(c, v, tick)
            if lbl := sub_labels.get(c):
                item = f"{html.escape(lbl)} {item}"
            parts.append(item)
        wrap = " agr-wrap" if spec.get("wrap") else ""
        # A percentage next to the symbol beats one buried in the dim line: on
        # a phone the sub line ellipsizes, so a labelled "P/L +46.5%" there was
        # cut mid-number. As a pill on line 1 it always reads in full.
        badge = ""
        if badge_col and badge_col in frame.columns:
            bv = r[badge_col]
            badge = " " + _delta_chip(
                bv,
                text(badge_col, bv),
                muted=(tick in muted and badge_col in muted_cols),
            )
        left = f'<div class="agr-l1">{html.escape(tick)}{badge}</div>'
        if parts:
            left += f'<div class="agr-l2{wrap}">{" · ".join(parts)}</div>'
        right = (
            f'<div class="agr-l1">{text(value_col, r[value_col])}</div>'
            if value_col and value_col in frame.columns
            else ""
        )
        if delta_col and delta_col in frame.columns:
            right += f'<div class="agr-l2">{colored(delta_col, r[delta_col], tick)}</div>'
        rows.append(
            f'<a class="agr-row" href="ticker?ticker={quote(tick)}" target="_self">'
            f'{img}<div class="agr-main">{left}</div>'
            + (f'<div class="agr-side">{right}</div>' if right else "")
            + "</a>"
        )
    return f"<div>{_ROWS_CSS}{''.join(rows)}</div>"


# Viewport switch for the dual-rendered tables below. UA sniffing (is_mobile)
# misses narrow desktop windows and desktop-UA tablets (iPadOS Safari sends no
# "Mobi"), so both renderings ship and a media query picks by actual width.
_RESP_BREAKPOINT = 640
_RESP_CSS = f"""<style>
.ag-resp-mob {{ display: none; }}
@media (max-width: {_RESP_BREAKPOINT}px) {{
  .ag-resp-desk {{ display: none; }}
  .ag-resp-mob {{ display: block; }}
}}
</style>"""


def responsive_ticker_table_html(
    frame: pd.DataFrame,
    *,
    mobile: dict,
    mobile_names: bool = False,
    fmt: dict[str, str] | None = None,
    signed: tuple[str, ...] = (),
    ticker_col: str = "ticker",
    names: bool = True,
    labels: dict[str, str] | None = None,
    muted: set[str] | frozenset[str] = frozenset(),
    muted_cols: tuple[str, ...] = (),
    pairs: tuple[tuple[str, str], ...] = (),
    sortable: str | None = None,
    left_cols: tuple[str, ...] = (),
) -> str:
    """Both renderings of a ticker table, switched by viewport width.

    The full column table shows above _RESP_BREAKPOINT px, the dense
    Revolut-style rows below it — so a narrow window adapts live, with no
    rerun and regardless of User-Agent. Args are ticker_table_html's;
    `mobile` is its row-slot spec (always applied to the row rendering here,
    not UA-gated) and `mobile_names` controls the company name on the rows
    (the table keeps `names`).
    """
    desk = ticker_table_html(
        frame, fmt=fmt, signed=signed, ticker_col=ticker_col, names=names,
        labels=labels, muted=muted, muted_cols=muted_cols, pairs=pairs,
        sortable=sortable, left_cols=left_cols,
    )
    rows = _ticker_rows_html(
        frame, spec=mobile, fmt=fmt, signed=signed, ticker_col=ticker_col,
        names=mobile_names, muted=muted, muted_cols=muted_cols,
    )
    return (
        f'{_RESP_CSS}<div class="ag-resp-desk">{desk}</div>'
        f'<div class="ag-resp-mob">{rows}</div>'
    )


def ticker_table_html(
    frame: pd.DataFrame,
    *,
    fmt: dict[str, str] | None = None,
    signed: tuple[str, ...] = (),
    ticker_col: str | None = "ticker",
    left_cols: tuple[str, ...] = (),
    names: bool = True,
    show_index: bool = False,
    labels: dict[str, str] | None = None,
    muted: set[str] | frozenset[str] = frozenset(),
    muted_cols: tuple[str, ...] = (),
    pairs: tuple[tuple[str, str], ...] = (),
    sortable: str | None = None,
    mobile: dict | None = None,
) -> str:
    """Positions-style table HTML: logo+name ticker cells, semantic P/L colors.

    Logo + "TICK — Company Name" share ONE cell, which st.dataframe can't do
    (ImageColumn is image-only), so ticker tables render as styled HTML via
    pandas Styler. Rows keep the caller's order; click-to-sort is the only
    capability given up. Render the result with st.html().

    Args:
        fmt: column -> format string, applied with na_rep="n/a".
        signed: columns colored green/red by sign (profit/loss semantics).
        ticker_col: column of raw symbols replaced by rich logo+name cells
            (None to leave the frame untouched).
        left_cols: columns kept left-aligned; every other non-ticker column
            right-aligns like a numbers column.
        names: include the dim company name after the symbol.
        show_index: keep the index column (e.g. KPI-labelled comps rows).
        labels: column -> displayed header text. Display-only relabel: fmt,
            signed and left_cols keep keying on the original column names.
        muted: raw ticker values (from `ticker_col`) whose `muted_cols` cells
            render in the dimmed off-session tint — the market's closed, so the
            day change is the last completed session's move, not a live tick.
        muted_cols: which signed columns dim for `muted` rows (e.g. the day
            change); other signed columns (total P/L) keep full color.
        pairs: (absolute_col, pct_col) couples merged into ONE cell each —
            "€+3,210  (+3.0%)", the percentage as a tinted pill. The pct
            column is dropped; the cell keeps the absolute column's position
            and its `labels` header. Both keep their own `fmt`; `signed` and
            `muted_cols` still key on the original names. Desktop only —
            the mobile rows below take their columns from `mobile`.
        sortable: a stable id ("positions") turning the headers into
            click-to-sort controls. Sorting runs client-side on the raw
            values (see _sort_key), so it costs no rerun and no refetch, and
            the chosen column/direction is remembered per id for the session
            — a rerun re-renders the table already sorted. A merged `pairs`
            column sorts by its absolute figure, the one it prints first.
        mobile: when set and the request comes from a phone, render dense
            Revolut-style rows (no horizontal panning) instead of a table.
            Maps columns onto row slots: {"value": col (line-1 right),
            "delta": col (line-2 right, signed-colored), "badge": col (a
            tinted pill on line 1, right after the symbol), "sub": (cols,)
            for the dim line under the ticker, "sub_labels": {col: prefix},
            "wrap": True to let the sub line wrap instead of ellipsize}.
            fmt/signed/muted apply unchanged. Desktop ignores this arg.
    """
    if mobile and ticker_col and ticker_col in frame.columns and is_mobile():
        return _ticker_rows_html(
            frame,
            spec=mobile,
            fmt=fmt,
            signed=signed,
            ticker_col=ticker_col,
            names=names,
            muted=muted,
            muted_cols=muted_cols,
        )
    frame = frame.copy()
    # Raw values, before formatting turns them into "€8,372" strings — the
    # click-sort keys are stamped from these (see _with_sort_keys).
    raw = {c: list(frame[c]) for c in frame.columns} if sortable else {}
    # Capture raw ticker ids before ticker_col is swapped for HTML cells, so
    # off-session muting can key rows by symbol regardless of the frame index.
    muted_mask = (
        [t in muted for t in frame[ticker_col]]
        if muted and muted_cols and ticker_col and ticker_col in frame.columns
        else None
    )
    for c in frame.columns:
        # Cell text renders as raw HTML (that's how the ticker cell works),
        # so escape every other string column — imports carry CSV content.
        # (pandas 3 infers `str` dtype, not object, for string columns.)
        if c != ticker_col and (
            pd.api.types.is_object_dtype(frame[c])
            or pd.api.types.is_string_dtype(frame[c])
        ):
            frame[c] = [
                html.escape(v) if isinstance(v, str) else v for v in frame[c]
            ]
    if ticker_col and ticker_col in frame.columns:
        frame[ticker_col] = [ticker_cell(t, name=names) for t in frame[ticker_col]]
    # Absolute + percentage couples collapse into one pre-rendered HTML cell,
    # so they carry their own formatting and colors and drop out of the
    # Styler's fmt/signed subsets below.
    merged: set[str] = set()
    for vcol, pcol in pairs:
        if vcol not in frame.columns or pcol not in frame.columns:
            continue
        vfmt = _value_formatter(fmt, signed, vcol)
        pfmt = _value_formatter(fmt, signed, pcol)
        pair_dim = vcol in muted_cols or pcol in muted_cols
        frame[vcol] = [
            _pair_cell(
                f'<span style="{signed_color(v, muted=m) if vcol in signed else ""}">'
                f"{vfmt(v)}</span>",
                _delta_chip(pct, pfmt(pct), muted=m),
            )
            for v, pct, m in zip(
                frame[vcol],
                frame[pcol],
                (pair_dim and m for m in (muted_mask or [False] * len(frame))),
                strict=True,
            )
        ]
        frame = frame.drop(columns=[pcol])
        merged.add(vcol)
    right = [c for c in frame.columns if c != ticker_col and c not in left_cols]
    fmt_map = {
        k: v for k, v in (fmt or {}).items()
        if k in frame.columns and k not in merged
    }
    # Signed columns drop the "+" on an exact 0 so market-closed rows read
    # "0.0%"/"€0" (neutral), matching signed_color's grey.
    for c in signed:
        if c in fmt_map:
            fmt_map[c] = _neutral_zero_formatter(fmt_map[c])
    sty = frame.style.format(fmt_map or None, na_rep="n/a")
    if colored := [c for c in signed if c in frame.columns and c not in merged]:
        # Rows whose market is closed dim only their day-change (muted_cols)
        # cells; total-P/L columns stay full color. Everything else keeps the
        # plain elementwise coloring.
        dim = [c for c in muted_cols if c in colored] if muted_mask else []
        plain = [c for c in colored if c not in dim]
        if plain:
            sty = sty.map(signed_color, subset=plain)
        for c in dim:
            sty = sty.apply(
                lambda col: [
                    signed_color(v, muted=m)
                    for v, m in zip(col, muted_mask, strict=False)
                ],
                subset=[c],
                axis=0,
            )
    if right:
        sty = sty.set_properties(subset=right, **{"text-align": "right"})
    if not show_index:
        sty = sty.hide(axis="index")
    if labels:
        # relabel_index takes the full new-label list in column order and only
        # changes the rendered headers — the per-column header alignment below
        # still keys on the original names.
        sty = sty.relabel_index([labels.get(c, c) for c in frame.columns], axis=1)
    sty = sty.set_table_styles(_TABLE_STYLES)
    if right:
        sty = sty.set_table_styles(
            {c: [{"selector": "th", "props": [("text-align", "right")]}]
             for c in right},
            overwrite=False,
            axis=0,
        )
    if sortable:
        sty = sty.set_table_styles(_SORT_STYLES, overwrite=False)
    markup = sty.to_html()
    if not sortable:
        return f'<div style="overflow-x:auto">{markup}</div>'
    markup = _with_sort_keys(
        markup,
        sty.uuid,
        [
            [_sort_key(raw[c][i]) for c in frame.columns]
            for i in range(len(frame))
        ],
    )
    # The click handler is wired once per page by app.py, for every table
    # carrying this hook — see the sorter script there.
    return (
        f'<div class="ag-sortable" data-ag-sort="{html.escape(sortable, quote=True)}"'
        f' style="overflow-x:auto">{markup}</div>'
    )


# Stacked label/value cards: the phone rendering of every table that ISN'T a
# ticker list (quarterly detail, dividends by year, insider trades, KPI
# sources...). Those have no symbol to hang a dense .agr-row off, and their
# columns are too many to fit a phone, so each row becomes a small card with
# one "label — value" line per column. One style block per table is
# idempotent, same as _ROWS_CSS.
_STACK_CSS = f"""<style>
.ags-card {{ padding: 9px 2px; border-bottom: 1px solid {RULE_SOFT}; }}
.ags-card:last-child {{ border-bottom: none; }}
.ags-title {{
  font-size: {FS_MD}; font-weight: 600; line-height: 1.4; margin-bottom: 3px;
}}
.ags-kv {{
  display: flex; gap: 12px; justify-content: space-between;
  align-items: baseline; font-size: {FS_SM}; line-height: 1.6;
}}
.ags-k {{ color: {TEXT_MUTED}; flex: 0 0 auto; }}
.ags-v {{ text-align: right; min-width: 0; overflow-wrap: anywhere; }}
</style>"""


def stacked_table_html(
    frame: pd.DataFrame,
    *,
    title: str | None = None,
    index_title: bool = False,
    title_html: bool = False,
    fmt: dict[str, str] | None = None,
    signed: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    hide: tuple[str, ...] = (),
) -> str:
    """Phone rendering of a non-ticker table: one card per row.

        Q2 FY26                     <- title
        Revenue            $94.0B   <- one line per remaining column
        YoY               +12.3%

    A wide grid on a 390px screen either pans sideways or squeezes every
    column to three characters; stacking the columns as label/value lines
    keeps every figure readable and the page scrolling in one direction.
    Missing cells are dropped rather than printed as "n/a" — on a phone a
    short card beats a complete one.

    Args:
        title: column whose value heads each card (dropped from the lines).
        index_title: head each card with the row index instead (for frames
            keyed by year/period, and for transposed grids).
        title_html: the title value is already markup (e.g. a `ticker_cell`)
            and must not be escaped.
        fmt: column -> format string or callable, as ticker_table_html.
        signed: columns tinted green/red by sign.
        labels: column -> displayed label; fmt/signed keep the raw names.
        hide: columns left out of the cards entirely.
    """
    labels = labels or {}
    cols = [
        c for c in frame.columns if c != title and c not in hide
    ]
    cards = []
    for idx, row in frame.iterrows():
        head = ""
        if index_title:
            head = str(idx)
        elif title is not None and title in frame.columns:
            head = str(row[title])
        lines = []
        for c in cols:
            v = row[c]
            try:
                if pd.isna(v):
                    continue
            except (TypeError, ValueError):
                pass
            if isinstance(v, str) and not v.strip():
                continue
            f = (fmt or {}).get(c)
            text = html.escape(
                f(v) if callable(f) else _value_formatter(fmt, signed, c)(v)
            )
            if c in signed and (css := signed_color(v)):
                text = f'<span style="{css}">{text}</span>'
            lines.append(
                f'<div class="ags-kv"><span class="ags-k">'
                f'{html.escape(str(labels.get(c, c)))}</span>'
                f'<span class="ags-v">{text}</span></div>'
            )
        if head:
            head = (
                '<div class="ags-title">'
                + (head if title_html else html.escape(head))
                + "</div>"
            )
        cards.append(f'<div class="ags-card">{head}{"".join(lines)}</div>')
    return f"<div>{_STACK_CSS}{''.join(cards)}</div>"


def data_table(
    frame: pd.DataFrame,
    *,
    title: str | None = None,
    index_title: bool = False,
    title_html: bool = False,
    fmt: dict[str, str] | None = None,
    signed: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    hide: tuple[str, ...] = (),
    container=None,
    **kwargs,
) -> None:
    """st.dataframe on desktop, `stacked_table_html` cards on phones.

    The mobile-only arguments mirror stacked_table_html; `fmt` doubles as the
    desktop number format (applied through a Styler) unless the caller drives
    that with its own `column_config`. Everything else is forwarded to
    st.dataframe untouched.
    """
    target = container if container is not None else st
    if is_mobile():
        target.html(
            stacked_table_html(
                frame,
                title=title,
                index_title=index_title,
                title_html=title_html,
                fmt=fmt,
                signed=signed,
                labels=labels,
                hide=hide,
            )
        )
        return
    show = frame
    if fmt and "column_config" not in kwargs:
        show = frame.style.format(
            {k: v for k, v in fmt.items() if k in frame.columns}, na_rep="n/a"
        )
    target.dataframe(show, **kwargs)


@st.cache_data(ttl=86400, show_spinner=False)
def sec_title(ticker: str) -> str | None:
    """Company name for a held-but-unlisted ticker, from the SEC map.

    Without it those rows carry the bare symbol, so a name query ("oracle")
    can't find an imported ORCL position — and the `t not in labels` dedup
    then drops the SEC result too, leaving only the Analyze fallback. None
    (cached, so an offline miss isn't retried per rerun) for non-US symbols.
    """
    from stocks.data.edgar import title_for

    try:
        return title_for(ticker)
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False)
def sec_matches(query: str) -> list[tuple[str, str]]:
    """SEC ticker-map search (symbol or company name) behind the picker.

    Pure in-memory scan of the cached map, but memoised per query anyway so
    reruns while typing don't rescan 10k rows. Empty when the map has never
    been cached and the network is down — search degrades, picker survives.
    """
    from stocks.data.edgar import search_companies

    try:
        return search_companies(query, limit=6)
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def world_matches(query: str) -> list[tuple[str, str, str]]:
    """Worldwide symbol search (Yahoo), the last tier of the picker.

    The tiers above it are all local tables and all partial — the watchlist,
    the coin list, and the SEC map's US filers — so a foreign listing had no
    way to be found by name. This one covers every venue Yahoo quotes and is
    typo-tolerant, which is what makes "mips" reach MIPS.ST instead of falling
    through to an Analyze button for a symbol Yahoo has no data for.

    Unlike its siblings this is a network round-trip, so it is cached for an
    hour per query (listings barely move) and hard-capped inside
    `search_symbols` by a short timeout plus a cooldown after any failure.
    """
    from stocks.data.symbols import search_symbols

    try:
        return search_symbols(query, limit=6)
    except Exception:
        return []


# How close a SEC company name must be to the query to count as "nailed it".
# Above the general FUZZY_CUTOFF: this decides which group leads, so it should
# admit a typo ("SANDISC" vs "SANDISK" .86) but not a near-miss neighbour
# ("MIPS" vs "VIPSHOP" .55, "IWDA" vs "IDEA" .75).
STRONG_MATCH = 0.8


def _norm_name(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


def _world_first(q: str, sec: list[tuple[str, str]]) -> bool:
    """Whether the worldwide group should render above the SEC group.

    The SEC tier degrades as it goes: after its exact and prefix hits it falls
    back to substrings and then to fuzz, so "MIPS" answers with VIPS, CMPS and
    MVIS — six wrong US tickers that would bury the one real match (MIPS.ST).
    It keeps the top slot only when it actually nailed the query.

    "Nailed it" is an exact symbol, a company name starting with the query, or
    a company name whose OPENING words are a near-match for it. Only the
    opening words, because the query being buried anywhere in a longer name
    proves nothing — "hermes" scores .92 against "Federated Hermes, Inc." and
    would hand the lead to an asset manager over Hermès itself. Matching word
    for word from the start instead keeps "sandisc" on Sandisk Corp and
    "nvidia" on Nvidia Corp (above the leveraged NVDA ETFs Yahoo returns),
    while "bank of amrica" still lands on BANK OF AMERICA CORP.
    """
    key = _norm_name(q)
    for t, n in sec:
        if t == q or (key and _norm_name(n).startswith(key)):
            return False
        words = re.sub(r"[^A-Z0-9 ]", " ", n.upper()).split()
        head = " ".join(words[: len(q.split())])
        if head and fuzzy_ratio(q, head) >= STRONG_MATCH:
            return False
    return True


def _world_label(t: str, name: str, exch: str) -> str:
    """Dropdown label for a worldwide hit: 🌐 SYMBOL  Name · Exchange.

    The exchange is what disambiguates this tier — several rows can be the
    same brand on different venues, and it is also the hint that the symbol
    is foreign (MIPS.ST · Stockholm). Long legal names ("Hermès International
    Société en commandite par actions") are clipped so the row stays one line.
    """
    short = name if len(name) <= 34 else name[:33].rstrip() + "…"
    tail = f"{short} · {exch}" if exch else short
    return f"🌐 **{t}**  {tail}"


@st.cache_data(ttl=900, show_spinner=False)
def recent_closes(tickers: tuple[str, ...]) -> dict[str, list[float]]:
    """Last two daily closes per ticker (prev, last).

    One bulk download (data.fetch.fetch_many) for the whole watchlist, cached
    15 min so the ticker list renders without hammering the network on every
    rerun. The cache key is the ticker tuple, so every page that shows the
    picker shares one download; change chips and portfolio weights both derive
    from it.
    """
    from stocks.data.fetch import fetch_many

    out: dict[str, list[float]] = {}
    for t, df in fetch_many(list(tickers), period="5d").items():
        close = df["Close"].dropna() if "Close" in df else None
        if close is not None and len(close):
            out[t] = [float(v) for v in close.iloc[-2:]]
    return out


def daily_changes(tickers: tuple[str, ...]) -> dict[str, float]:
    """Latest daily % change (last close vs previous close) per ticker."""
    return {
        t: (c[-1] / c[-2] - 1) * 100
        for t, c in recent_closes(tickers).items()
        if len(c) >= 2 and c[-2]
    }


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", s)


def db_mtime(db: str) -> float:
    """Ledger file mtime — a cache key that changes only when the book does,
    so ledger-derived caches stay hot until the next import instead of
    expiring on a timer. 0.0 when the file doesn't exist yet."""
    try:
        return Path(db).stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False, max_entries=64)
def held_tickers(db: str, mtime: float) -> list[str]:
    """Tickers with an open position in the ledger — shown in the picker even
    when they're not on the watchlist, so imported activity is browsable.
    `db` is the session user's ledger path; it keys the cache so concurrent
    users never see each other's positions. `mtime` (db_mtime) invalidates
    the entry exactly when the ledger file changes."""
    try:
        from stocks.portfolio.ledger import all_transactions
        from stocks.portfolio.positions import build

        # Identity converter: quantities don't need FX, keeps this offline.
        positions, _ = build(all_transactions(Path(db)), to_eur=lambda a, c, d: a)
        return [p.ticker for p in positions]
    except Exception:
        return []  # empty/inconsistent ledger must never break the picker


@st.cache_data(ttl=900, show_spinner=False)
def portfolio_stats(
    tickers: tuple[str, ...], db: str, mtime: float
) -> tuple[dict[str, float], dict[str, float]]:
    """(weight, unrealised P/L %) per held ticker, for the "Portfolio %" sort.

    Weights are EUR market-value shares (0–1) — note they need one FX spot
    lookup per non-EUR currency (cached 15 min with the rest). P/L is each
    open position's price return in its native currency — (qty·last / cost)
    − 1 — FX moves excluded. `tickers` is the picker's full list: prices come
    from the same cached bulk download the change chips use, so this adds no
    extra market-data call.
    """
    try:
        from pathlib import Path

        from stocks.analysis.portfolio import market_value_weights_eur
        from stocks.portfolio.ledger import all_transactions
        from stocks.portfolio.positions import build

        # Identity converter keeps this offline; quantity/cost_native don't need FX.
        positions, _ = build(all_transactions(Path(db)), to_eur=lambda a, c, d: a)
        if not positions:
            return {}, {}
        closes = recent_closes(tickers)
        prices = {t: c[-1] for t, c in closes.items() if c}
        weights = market_value_weights_eur(positions, prices, {})
        pnl = {
            p.ticker: (p.quantity * prices[p.ticker] / p.cost_native - 1) * 100
            for p in positions
            if prices.get(p.ticker) and p.cost_native
        }
        return weights, pnl
    except Exception:
        return {}, {}  # missing ledger/prices/FX must never break the picker


def _change_md(chg: float | None) -> str:
    """Colored daily-change chip: green up, red down, gray when unknown."""
    if chg is None:
        return ":gray[—]"
    return f":{'green' if chg >= 0 else 'red'}[{chg:+.2f}%]"


def ticker_picker(
    *,
    key: str = "ticker",
    container=None,
    title: str | None = None,
    allow_custom: bool = True,
    show_changes: bool = True,
    list_height: int = 360,
) -> str | None:
    """Searchbar + scrollable watchlist button list; returns the picked ticker.

    Args:
        key: prefix isolating this picker's widget keys, so multiple pickers
            (main page, valuation page) never collide. The selected ticker and
            sort choice are deliberately shared across all pickers (session
            keys "picker_selected"/"picker_sort_by"), so switching pages keeps
            the same selection and ordering.
        container: where to render; defaults to `st.sidebar`.
        title: bold label above the searchbar (defaults to the localized
            "Tickers" heading when None).
        allow_custom: show an "Analyze <SYMBOL>" button when the query is a
            symbol not on the watchlist, so any ticker can be analyzed.
        show_changes: render the per-ticker daily-change chip (one bulk
            download, cached and shared across pages).
        list_height: pixel height of the scroll region holding the buttons.

    Returns the selected ticker (upper-cased, stripped) or None when the
    watchlist is empty and nothing has been typed.
    """
    if title is None:
        title = tr("widgets.tickers_title")
    mobile = is_mobile()
    if container is not None:
        box = container
    elif mobile:
        # Phones: no picker UI. The fixed top-bar search already covers ticker
        # navigation on phones, so the popover picker is redundant clutter.
        # Skip drawing it (early return below, after the selection is seeded).
        box = None
    else:
        box = st.sidebar
    if mobile:
        # Shorter scroll region: a tall inner scroller inside the page scroll
        # is a touch trap.
        list_height = min(list_height, 260)

    holdings = load_watchlist(auth.watchlist_path())
    labels = {h.ticker: (h.name or h.ticker) for h in holdings}
    fav_set = {h.ticker for h in holdings if h.favorite}
    tag_map = {h.ticker: h.tags for h in holdings if h.tags}
    # Held-but-unlisted tickers (from the imported ledger) join the list so
    # every open position is reachable; a briefcase icon marks them in the row.
    _db = str(auth.db_path())
    held_set = set(held_tickers(_db, db_mtime(_db)))
    for t in sorted(held_set - set(labels)):
        labels[t] = sec_title(t) or t
    # Favorites float to the top of the list; a star icon marks them in the row.
    tickers = [t for t in labels if t in fav_set]
    tickers += [t for t in labels if t not in fav_set]

    # Shared across pickers (not keyed by `key`) so the picked ticker follows
    # you between pages.
    sel_key = "picker_selected"
    if tickers:
        st.session_state.setdefault(sel_key, tickers[0])

    if mobile and container is None:
        # Selection seeded above; return it without rendering any picker UI.
        ticker = st.session_state.get(sel_key)
        return ticker.strip().upper() if ticker else ticker

    def _select(t: str) -> None:
        st.session_state[sel_key] = t
        # Flag for app.py: a pick anywhere navigates to the Ticker page (also
        # when re-clicking the already-selected row). Consumed there via pop().
        st.session_state["picker_clicked"] = True

    def _btn_key(t: str) -> str:
        """Widget key for a ticker's pick button (also its .st-key-* CSS class)."""
        return f"{key}_pick_{_slug(t)}"

    if not (mobile and container is None):
        box.markdown(f"**{title}**")  # popover already carries the label
    # Search box with a sort-menu icon button right beside it. The popover keeps
    # the sort options out of the way until clicked. Change sorts are only
    # offered when chips are on, since they need the daily-change numbers;
    # "Portfolio %" only when the ledger has open positions to weigh.
    weights, pnl = (
        portfolio_stats(tuple(tickers), _db, db_mtime(_db)) if held_set else ({}, {})
    )
    # Sort options as stable ids paired with translated labels. The id (not the
    # label) is what persists in session + user prefs, so the choice survives a
    # browser reload and a language switch; the label is display-only via the
    # selectbox format_func below.
    sort_labels = {
        "watchlist": tr("widgets.sort_watchlist"),
        "az": tr("widgets.sort_az"),
        "change_up": tr("widgets.sort_change_up"),
        "change_down": tr("widgets.sort_change_down"),
        "portfolio": tr("widgets.sort_portfolio"),
    }
    sort_ids = ["watchlist", "az"]
    if show_changes:  # change sorts need the daily-change numbers
        sort_ids += ["change_up", "change_down"]
    if weights:  # portfolio % only when the ledger has open positions to weigh
        sort_ids.append("portfolio")
    query = box.text_input(
        tr("widgets.search"),
        key=f"{key}_search",
        placeholder=tr("widgets.search_placeholder"),
        label_visibility="collapsed",
    ).strip()
    # Sort choice. Session "picker_sort_by" shares it across pickers/pages; user
    # prefs persist it across a browser reload (session state is wiped on reload,
    # so a fresh load re-seeds from prefs — read once and cache into session so
    # the common no-change reruns skip the disk read).
    if "picker_sort_by" not in st.session_state:
        st.session_state["picker_sort_by"] = (
            auth.load_prefs().get("picker_sort_by") or sort_ids[0]
        )
    stored_sort = st.session_state["picker_sort_by"]
    if stored_sort not in sort_ids:
        stored_sort = sort_ids[0]

    def _set_sort() -> None:
        sid = st.session_state[f"{key}_sort"]
        st.session_state["picker_sort_by"] = sid
        prefs = auth.load_prefs()
        if prefs.get("picker_sort_by") != sid:  # persist across reloads
            prefs["picker_sort_by"] = sid
            auth.save_prefs(prefs)

    # Inline segmented control, not a dropdown/popover: the options render in
    # place, so nothing portals to page root and gets dismissed the moment the
    # cursor leaves the (collapsible, overlay) sidebar. required=True blocks the
    # single-select "deselect on re-click", so a sort is always active.
    sort_by = box.segmented_control(
        tr("widgets.sort_by"),
        sort_ids,
        default=stored_sort,
        required=True,
        format_func=lambda sid: sort_labels[sid],
        key=f"{key}_sort",
        on_change=_set_sort,
    )

    q = query.upper()
    # Query matches ticker, company name or any tag — typing a tag-group name
    # (e.g. "semis") filters the list down to that group.
    shown = (
        [
            t
            for t in tickers
            if q in t.upper()
            or q in labels[t].upper()
            or any(q in tag.upper() for tag in tag_map.get(t, ()))
        ]
        if q
        else tickers
    )
    if q and not shown:
        # Typo fallback ("oracel") — same fields, fuzzy, best score first.
        shown = _fuzzy_order(q, tickers, labels, tag_map)

    # Rows shimmer from here until the scroll region below is built: the change
    # chips need a live quote per ticker, so on a cold cache the sidebar would
    # otherwise show a search box above empty space.
    rows_slot = skeletons.reserve("rows", container=box, rows=8)
    # The picker renders before page.run(), outside the app-level guard — a
    # throttled Yahoo or dead network must dim the change chips, not crash the
    # app. The miss isn't cached (st.cache_data skips exceptions), so a rerun
    # retries; the toast (deduped app-wide) explains the blank chips once.
    try:
        changes = daily_changes(tuple(tickers)) if show_changes else {}
    except (YFRateLimitError, URLError) as exc:
        notices.data_toast(exc)
        changes = {}

    # Apply the sort chosen in the popover. "Watchlist" keeps the favorites-first
    # source order; the rest reorder `shown`. Missing changes sink to the bottom.
    if sort_by == "az":
        shown = sorted(shown)
    elif sort_by in ("change_up", "change_down"):
        desc = sort_by == "change_down"
        # Unknown change → +inf so it sorts last in both directions.
        shown = sorted(
            shown,
            key=lambda t: (
                changes.get(t) if changes.get(t) is not None else float("inf")
            ),
            reverse=desc,
        )
        if desc:
            # reverse=True pushes the +inf unknowns to the front; move them back.
            known = [t for t in shown if changes.get(t) is not None]
            unknown = [t for t in shown if changes.get(t) is None]
            shown = known + unknown
    elif sort_by == "portfolio":
        # Biggest position first; unheld tickers weigh 0 and sink to the bottom
        # keeping their watchlist order (sorted is stable).
        shown = sorted(shown, key=lambda t: -weights.get(t, 0.0))

    # Logo lives inside each button as a left-aligned background image (Streamlit
    # won't render an image in a button label). Per-ticker CSS targets the
    # .st-key-<key> class Streamlit stamps on each keyed widget's container.
    # Covers watchlist rows (_pick_), SEC search rows (_sec_) and crypto
    # search rows (_cx_).
    prefixes = (f"{key}_pick_", f"{key}_sec_", f"{key}_cx_")

    def _sel(suffix: str) -> str:
        return ",".join(f'[class*="st-key-{p}"] {suffix}' for p in prefixes)

    primary = 'button[kind="primary"]'
    rules = [
        f"{_sel('button')} {{justify-content:flex-start;"
        " text-align:left; padding-left:34px; background-repeat:no-repeat;"
        " background-position:9px center; background-size:18px 18px;}",
        # Selected pick highlighted with the brand "active" chip — purple-900
        # fill, purple-800 border, purple-300 text (overrides the Streamlit
        # primary fill), like the design's active nav row. The `*` rule beats
        # the inline color on the :green/:red change chip so the whole label
        # reads in the brand tint.
        f"{_sel(primary)},"
        f"{_sel(primary + ':hover')},"
        f"{_sel(primary + ':active')},"
        f"{_sel(primary + ':focus')} "
        f" {{background-color:{PURPLE_900} !important;"
        f" border-color:{PURPLE_800} !important;"
        f" color:{PURPLE_300} !important;"
        " box-shadow:none !important;}"  # active chip, not a CTA — no glow
        f"{_sel(primary + ' *')}"
        f" {{color:{PURPLE_300} !important;}}",
    ]
    for t in shown:
        src = logo(t)
        if src:
            bg = f'background-image:url("{src}");'
            rules.append(f".st-key-{_btn_key(t)} button {{{bg}}}")
    box.html("<style>" + "".join(rules) + "</style>")

    # Beyond the watchlist: the query also searches the whole SEC ticker map
    # (symbol or company name), so typing "airbnb" surfaces ABNB even when it
    # isn't listed or held, and then Yahoo's worldwide lookup, which is what
    # carries the non-US names the SEC map has never heard of ("mips" ->
    # MIPS.ST). A raw "Analyze <SYMBOL>" fallback stays for exact symbols
    # neither one knows.
    matches: list[tuple[str, str]] = []
    crypto_hits: list[tuple[str, str]] = []
    fund_hits: list[tuple[str, str]] = []
    world_hits: list[tuple[str, str, str]] = []
    if allow_custom and q:
        matches = [(t, n) for t, n in sec_matches(q) if t not in labels]
        # Coin codes and names too, so "bitcoin" or "btc" offers BTC-USD.
        from stocks.data.crypto import search_crypto
        from stocks.data.funds import search_funds

        crypto_hits = [(t, n) for t, n in search_crypto(q) if t not in labels]
        # And the fund catalog, so "world" or "sp500" offers the UCITS line a
        # European broker actually sells — locally, with no Yahoo round trip.
        fund_hits = [(t, n) for t, n in search_funds(q) if t not in labels]
        seen = (
            set(labels)
            | {t for t, _ in matches}
            | {t for t, _ in crypto_hits}
            | {t for t, _ in fund_hits}
        )
        world_hits = [(t, n, x) for t, n, x in world_matches(q) if t not in seen][:3]
        known = seen | {t for t, _, _ in world_hits}
        if q not in known and re.fullmatch(r"[A-Z0-9.\-]{1,12}", q):
            box.button(
                tr("widgets.analyze", q=q),
                key=f"{key}_analyze_new",
                on_click=_select,
                args=(q,),
                width="stretch",
                type="primary",
            )

    selected = st.session_state.get(sel_key)
    # Fixed-height scroll region so a long watchlist stays a tidy, scrollable list.
    with rows_slot.container(height=list_height):
        if not shown and not q:
            st.caption(tr("widgets.no_tickers"))
        for t in shown:
            star = (
                ":material/star: " if t in fav_set
                else (":material/work: " if t in held_set else "")
            )
            chip = f"  {_change_md(changes.get(t))}" if show_changes else ""
            if sort_by == "portfolio":
                # Sorting by weight — show the weight plus the position's total
                # P/L (green/red) instead of the day change.
                w = weights.get(t)
                parts = [f":gray[{w * 100:.1f}%]"] if w else []
                if (p := pnl.get(t)) is not None:
                    parts.append(_change_md(p))
                chip = "  " + " ".join(parts) if parts else ""
            st.button(
                f"{star}**{t}** {labels[t]}{chip}",
                key=_btn_key(t),
                on_click=_select,
                args=(t,),
                width="stretch",
                # Selected ticker highlighted via primary style (opposing fill/text).
                type="primary" if t == selected else "secondary",
            )
        # Crypto hits above the SEC rows: coin code or name matched the query.
        # Same no-logo rule as SEC rows; 🪙 marks them. Picking one selects the
        # Yahoo pair symbol (BTC-USD), the only form the app stores.
        if crypto_hits:
            st.caption(tr("widgets.crypto"))
            for t, name in crypto_hits:
                st.button(
                    f"🪙 **{t}** {name}",
                    key=f"{key}_cx_{_slug(t)}",
                    on_click=_select,
                    args=(t,),
                    width="stretch",
                    type="primary" if t == selected else "secondary",
                )
        # Funds next, 🧺 for the basket: same no-logo rule, and the symbol
        # picked is the Yahoo one the catalog stores (IWDA.AS, not IWDA).
        if fund_hits:
            st.caption(tr("widgets.funds"))
            for t, name in fund_hits:
                st.button(
                    f"🧺 **{t}** {name}",
                    key=f"{key}_fd_{_slug(t)}",
                    on_click=_select,
                    args=(t,),
                    width="stretch",
                    type="primary" if t == selected else "secondary",
                )
        # Then the two searched tiers, 🌐 worldwide and 🔎 SEC map, in whichever
        # order matched the query better (see _world_first). Neither carries a
        # logo background — resolving one hits the network per uncached ticker,
        # too costly per keystroke; the marks say "searched, not listed".
        # Picking one selects it like any row (and the favorite star can then
        # pin it to the watchlist).
        def _world_group() -> None:
            if world_hits:
                st.caption(tr("widgets.from_world_search"))
                for t, name, exch in world_hits:
                    st.button(
                        _world_label(t, name, exch),
                        key=f"{key}_w_{_slug(t)}",
                        on_click=_select,
                        args=(t,),
                        width="stretch",
                        type="primary" if t == selected else "secondary",
                    )

        def _sec_group() -> None:
            if matches:
                st.caption(tr("widgets.from_sec_search"))
                for t, name in matches:
                    st.button(
                        f"🔎 **{t}** {name}",
                        key=f"{key}_sec_{_slug(t)}",
                        on_click=_select,
                        args=(t,),
                        width="stretch",
                        type="primary" if t == selected else "secondary",
                    )

        groups = (_world_group, _sec_group)
        if not _world_first(q, matches):
            groups = groups[::-1]
        for group in groups:
            group()

    ticker = st.session_state.get(sel_key)
    ticker = ticker.strip().upper() if ticker else ticker
    return ticker


def ticker_actions(ticker: str, *, container=None, key: str = "ticker") -> None:
    """Per-ticker quick actions: favorite star toggle + tag-group editor.

    Both write to this account's watchlist.yaml (auth.toggle_favorite /
    auth.set_tags), creating the entry when the symbol isn't listed yet — so
    favoriting or tagging a custom-analyzed or held-only ticker also adds it
    to the watchlist. Tags are free-form groups ("semis", "EM dividend"…);
    the picker search matches them, so typing a tag filters to its group.

    Rendered in the ticker page header; pass `container` to place it
    elsewhere. Writes, so login-gated: anonymous visitors (on the shared
    guest watchlist) get a sign-in button instead of the actions.
    """
    box = container if container is not None else st
    if not auth.is_logged_in():
        if "auth" in st.secrets:
            box.button(
                tr("widgets.sign_in_favorite"),
                key=f"{key}_login_{_slug(ticker)}",
                icon=":material/login:",
                on_click=auth.login,
                width="stretch",
            )
        return
    h = next(
        (
            x
            for x in load_watchlist(auth.watchlist_path())
            if x.ticker.upper() == ticker.upper()
        ),
        None,
    )
    fav = bool(h and h.favorite)
    tags = list(h.tags) if h else []
    alerts = list(h.alerts) if h else []
    ms_key = f"{key}_tags_{_slug(ticker)}"

    def _toggle_fav() -> None:
        now = auth.toggle_favorite(ticker)
        msg = (
            tr("widgets.toast_fav_added", ticker=ticker)
            if now
            else tr("widgets.toast_fav_removed", ticker=ticker)
        )
        st.toast(msg, icon=":material/star:")

    def _save_tags() -> None:
        auth.set_tags(ticker, st.session_state[ms_key])

    def _tag_editor() -> None:
        st.multiselect(
            tr("widgets.tag_groups"),
            options=auth.all_tags(),
            default=tags,
            key=ms_key,
            accept_new_options=True,
            on_change=_save_tags,
            placeholder=tr("widgets.tags_placeholder"),
            help=tr("widgets.tags_help"),
        )

    # ------------------------------------------------------------- alerts
    # Rules live per-holding in this account's watchlist.yaml (config.Alert);
    # the hourly cron (notify/fanout.py) evaluates them and messages the
    # user's linked Telegram. The editor writes through auth.set_alerts, so a
    # rule on a not-yet-listed ticker also adds it to the watchlist.

    def _alert_to_dict(a: Alert) -> dict:
        return {
            k: v
            for k, v in (
                ("type", a.type), ("price", a.price), ("pct", a.pct),
                ("level", a.level), ("window", a.window),
            )
            if v is not None
        }

    def _alert_summary(a: Alert) -> str:
        parts = [tr(f"widgets.alert_t_{a.type}")]
        if a.price is not None:
            parts.append(f"{a.price:g}")
        if a.pct is not None:
            parts.append(f"{a.pct:g}%")
        if a.level is not None:
            parts.append(f"{a.level:g}")
        if a.window:
            parts.append(f"({a.window}d)")
        return " ".join(parts)

    _ALERT_TYPE_ORDER = ("above", "below", "pct_move", "drawdown", "rsi_below",
                         "rsi_above", "sma_cross", "high_52w", "low_52w")
    _WINDOW_DEFAULTS = {"rsi_below": 14, "rsi_above": 14, "sma_cross": 50,
                        "high_52w": 252, "low_52w": 252}

    def _alert_editor() -> None:
        st.caption(tr("widgets.alerts_caption"))
        if alerts:
            for i, a in enumerate(alerts):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f":small[{_alert_summary(a)}]", width="stretch")
                    if st.button(
                        ":material/delete:",
                        key=f"{key}_al_del_{i}_{_slug(ticker)}",
                        help=tr("widgets.alert_removed"),
                    ):
                        auth.set_alerts(
                            ticker,
                            [_alert_to_dict(x) for j, x in enumerate(alerts) if j != i],
                        )
                        st.toast(tr("widgets.alert_removed"),
                                 icon=":material/notifications_off:")
                        st.rerun()
        else:
            st.caption(tr("widgets.alert_none", ticker=ticker))

        atype = st.selectbox(
            tr("widgets.alert_type"),
            _ALERT_TYPE_ORDER,
            format_func=lambda t: tr(f"widgets.alert_t_{t}"),
            key=f"{key}_al_type_{_slug(ticker)}",
        )
        entry: dict = {"type": atype}
        fk = f"{key}_al_{atype}_{_slug(ticker)}"  # per-type keys: no stale values
        if atype in ("above", "below"):
            entry["price"] = st.number_input(
                tr("widgets.alert_price"), min_value=0.0, key=f"{fk}_price"
            )
        elif atype in ("pct_move", "drawdown"):
            entry["pct"] = st.number_input(
                tr("widgets.alert_pct"), min_value=0.0, value=5.0, key=f"{fk}_pct"
            )
        elif atype in ("rsi_below", "rsi_above"):
            entry["level"] = st.number_input(
                tr("widgets.alert_level"), min_value=0.0, max_value=100.0,
                value=30.0 if atype == "rsi_below" else 70.0, key=f"{fk}_level",
            )
        if atype in _WINDOW_DEFAULTS:
            entry["window"] = int(st.number_input(
                tr("widgets.alert_window"), min_value=2,
                value=_WINDOW_DEFAULTS[atype], key=f"{fk}_window",
            ))

        incomplete = (entry.get("price") == 0.0 and atype in ("above", "below")) or (
            entry.get("pct") == 0.0 and atype in ("pct_move", "drawdown")
        )
        if st.button(
            tr("widgets.alert_add"),
            key=f"{fk}_add",
            icon=":material/notification_add:",
            disabled=incomplete,
        ):
            auth.set_alerts(ticker, [*(_alert_to_dict(a) for a in alerts), entry])
            st.toast(tr("widgets.alert_added", ticker=ticker),
                     icon=":material/notifications_active:")
            st.rerun()

    if is_mobile():
        # Phones: both actions fold into one compact kebab menu that sits inline
        # beside the title, instead of two full-width rows under the header. The
        # favorited state and current tags show once the menu is open.
        with box.popover(":material/more_vert:"):
            st.button(
                tr("widgets.remove_favorite") if fav else tr("widgets.add_favorite"),
                key=f"{key}_fav_{_slug(ticker)}",
                icon=":material/star:",
                on_click=_toggle_fav,
                type="primary" if fav else "secondary",
                width="stretch",
            )
            _tag_editor()
            st.divider()
            _alert_editor()
        return

    c1, c2, c3 = box.columns([1, 2, 2], vertical_alignment="center")
    c1.button(
        ":material/star:",
        key=f"{key}_fav_{_slug(ticker)}",
        on_click=_toggle_fav,
        help=tr("widgets.remove_favorite") if fav else tr("widgets.add_favorite"),
        # Primary fill marks the favorited state (label is the same star).
        type="primary" if fav else "secondary",
        width="stretch",
    )

    with c2.popover(f":material/label: {tr('widgets.tags')}", width="stretch"):
        _tag_editor()

    with c3.popover(
        f":material/notifications: {tr('widgets.alerts')}"
        + (f" ({len(alerts)})" if alerts else ""),
        width="stretch",
    ):
        _alert_editor()

    if tags:
        box.markdown(" ".join(f":gray-badge[{t}]" for t in tags))
