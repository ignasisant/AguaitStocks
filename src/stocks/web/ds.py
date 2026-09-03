"""The design system in Python: tokens, chart chrome and the CSS they emit.

Single source of truth for every color, radius, elevation and type step the
Streamlit theme (.streamlit/config.toml) can't reach — our own HTML, CCv2
component CSS and Plotly figures. Values are Amphora Web DS tokens and MUST
stay in lockstep with config.toml, which paints Streamlit's own chrome from
the same ramp.

Nothing outside this module may write a raw hex: Python code imports these
names, CSS reads the `--ag-*` custom properties `ds_vars_css` emits from them,
so a token change lands everywhere at once. This is the bottom of the web
layer — it imports no other `stocks.web` module at all, so everything else is
free to import it.
"""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import streamlit as st

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
# Plotly wants a number, not a CSS length: bar-cap radius in px, the "6 · nav"
# step. Plotly clamps it to half the bar width (thin monthly bars stay
# square-ish) and rounds only the ends of a stack, never the joins inside it.
BAR_RADIUS = 6

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


def viewer_tz() -> tzinfo | None:
    """The reader's timezone, or None for "whatever the server runs in".

    Never the server's by choice: this app serves from Cloud Run in UTC, so a
    server-side strftime would tell a Madrid reader that their 16:40 question
    was asked at 14:40. Streamlit reports the browser's IANA zone, with the
    raw minute offset as the fallback for a client that only sends that.
    """
    try:
        name, offset = st.context.timezone, st.context.timezone_offset
    except Exception:  # no browser context (AppTest, a bare script run)
        return None
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown zone id, or no tzdata in the image
            pass
    if isinstance(offset, int):
        # JS sign convention: getTimezoneOffset is minutes *behind* UTC.
        return timezone(timedelta(minutes=-offset))
    return None


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
    # Rounded bar caps, app-wide: every bar chart splats this helper, and a
    # no-op on line/pie/heatmap figures. Stacked bars keep square joins —
    # plotly only rounds the ends of a stack.
    layout["barcornerradius"] = BAR_RADIUS
    if mobile:
        # DS mobile chart spec: a finger drag drives the crosshair and the
        # reading row (app.py's touch bridge), so nothing may pan under it.
        layout["dragmode"] = False
    return layout

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
