"""The Streamlit app — st.navigation over the app_pages/ modules.

Run: uv run stocks dashboard   (which serves stocks.web.server, the ASGI entry
point that fronts this script with the static landing page; running this file
directly with `streamlit run` still works and simply has no landing).

Page config, the dense-layout CSS and the nav are defined once here; the page
modules under app_pages/ carry only their own content. Colors and fonts live
in .streamlit/config.toml — the CSS below is only spacing the theme can't
express.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import URLError

# Hosts that run this file straight from the repo checkout (no editable
# install) need src/ on sys.path; locally it pins imports to the source tree.
_SRC = str(Path(__file__).resolve().parents[2])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import streamlit as st  # noqa: E402
from yfinance.exceptions import YFRateLimitError  # noqa: E402

from stocks import obs  # noqa: E402
from stocks.web import (  # noqa: E402
    auth,
    chat_core,
    css,
    feedback,
    i18n,
    landing,
    notices,
    onboarding,
    skeletons,
    telemetry,
)
from stocks.web.i18n import t as tr  # noqa: E402
from stocks.web.widgets import (  # noqa: E402
    ds_vars_css,
    is_mobile,
    render_bottom_nav,
    render_topbar,
    seed_selection,
)

_ASSETS = Path(__file__).parent / "assets"
st.set_page_config(
    page_title="TopStocks",
    page_icon=str(_ASSETS / "topstocks-icon.svg"),
    layout="wide",
)
st.logo(
    str(_ASSETS / "topstocks-logo.svg"),
    icon_image=str(_ASSETS / "topstocks-icon.svg"),
    size="large",
)

# Design tokens first: every stylesheet below (and on every page, and inside
# the CCv2 shadow roots) reads its colors, radii, elevations and type steps
# from the `--ag-*` custom properties this emits. widgets.py owns the values;
# nothing downstream writes a raw hex. Must precede the blocks that use them.
css.inject(ds_vars_css())

# Dense layout: kill Streamlit's default top padding and wide element gaps so
# charts and metrics sit high and tight instead of floating in whitespace.
css.inject(
    """
    <style>
      /* Epilogue — the DS display face for KPI numbers; config.toml can only
         load one body/heading/code font each, so it rides in here. */
      @import url('https://fonts.googleapis.com/css2?family=Epilogue:wght@600;700;800&display=swap');
      /* DS type spec: numerals lining + tabular at page level, so every
         figure column (tables, KPIs, tickers) aligns without per-cell rules. */
      [data-testid="stApp"] {font-variant-numeric: lining-nums tabular-nums;}
      .block-container {padding-top: 1.2rem; padding-bottom: 1rem;
                        padding-left: 2.5rem; padding-right: 2.5rem; max-width: 100%;}
      /* Desktop only: reclaim the header strip. On phones the header must
         survive — it carries the sidebar/nav toggle, and the sidebar starts
         collapsed there. */
      @media (min-width: 641px) {
        header[data-testid="stHeader"] {height: 0; background: transparent;}
        /* Collapsed sidebar drops the logo + expand arrow into the (now-zeroed)
           header strip, where they paint over the first heading. That state is
           uniquely marked by the expand button; when it's present, give the page
           body room to clear the strip. Expanded, the logo sits in the sidebar,
           the top-left is clear, and the tight reclaim above stands. */
        [data-testid="stApp"]:has([data-testid="stExpandSidebarButton"])
        .block-container {
          padding-top: 3.5rem;
        }
      }
      @media (max-width: 640px) {
        /* The fixed header (3.75rem) overlays content on phones; clear it
           exactly instead of relying on the accumulated hidden-element gaps
           below to push the first heading past it. Sides are the DS mobile
           16px page margin; the bottom pad clears the fixed bottom tab bar
           (widgets.render_bottom_nav). */
        .block-container {padding-left: 1rem; padding-right: 1rem;
                          padding-top: 4rem; padding-bottom: 5.5rem;}
        /* Script-only st.html containers (the card tagger, chat wiring) are
           zero-height but still flex items, so each contributes one 0.55rem
           vertical-block gap above the first heading. (Style-only blocks are
           exempt: Streamlit routes them to the event container, out of the
           page flow.) Desktop swallows the stragglers under the breadcrumb
           bar's negative margin; phones have no bar, so hide them — scripts
           run on mount regardless of display. */
        [data-testid="stElementContainer"]:has(> [data-testid="stHtml"]
            > script:only-child) {
          display: none;
        }
        /* Same problem, different fix, for the two containers whose content is
           viewport-fixed (the bottom tab bar and the mobile header strip):
           display:none would take the fixed child down with them, so unwrap
           the boxes instead. What is left is a display:none style element and
           a fixed element — neither is a flex item, so neither adds a gap. */
        [data-testid="stElementContainer"]:has(> [data-testid="stHtml"]
            > .ts-bottomnav),
        [data-testid="stElementContainer"]:has(> [data-testid="stHtml"]
            > .topstocks-mheader),
        [data-testid="stHtml"]:has(> .ts-bottomnav),
        [data-testid="stHtml"]:has(> .topstocks-mheader) {
          display: contents;
        }
        /* Open drawer must cover the viewport-fixed topbar search
           (z 999999, widgets.py) and chat launcher (z 1000000,
           chat_core.py) instead of sliding under them. */
        section[data-testid="stSidebar"] {z-index: 1000001 !important;}
        [data-testid="stMetricLabel"] p {font-size: var(--ag-fs-sm);}
        [data-testid="stCaptionContainer"] p {font-size: var(--ag-fs-sm);}
        /* Phone metric rows (metric_cells) are wrapping horizontal containers of
           fixed-width tiles. Streamlit under-sizes each tile's flex box, so the
           verdict caption under the last wrapped row spills ~14px past the row
           and the next element paints over it. Pad the row bottom to swallow the
           spill. :has(stMetric) targets exactly these rows, not button groups. */
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {
          padding-bottom: 1.1rem;
        }
        /* Full-bleed charts: the card's 1.2rem side padding costs ~38px of
           plot width on a ~390px screen. Negative margins let the chart span
           the card edge-to-edge (same bleed trick as the topbar); text and
           metrics keep the card padding. Plotly measures its container after
           CSS applies, so the widened box is picked up on first render. */
        .topstocks-card
        [data-testid="stElementContainer"]:has(> [data-testid="stPlotlyChart"]) {
          margin-left: -1rem; margin-right: -1rem;
        }
        /* Markdown tables — the one table shape the page can't restructure,
           because the assistant writes them at runtime (chat answers, skill
           output). Everything else stacks into cards on a phone
           (widgets.stacked_table_html); these get their own scroll box so a
           six-column answer pans inside the bubble instead of widening the
           whole page. */
        [data-testid="stMarkdown"] table {
          display: block; width: max-content; max-width: 100%;
          overflow-x: auto; font-size: var(--ag-fs-sm);
        }
        [data-testid="stMarkdown"] table td,
        [data-testid="stMarkdown"] table th {white-space: nowrap;}
        /* One screen title per phone screen (DS mobile spec): it lives in the
           header strip, so a page-level st.title under it would only repeat
           the nav label. Scoped to Streamlit's own heading block — the pages
           that write their own h1 (the ticker hero, which names the company,
           not the screen) render through stHtml and are untouched. */
        [data-testid="stMainBlockContainer"]
          [data-testid="stHeadingWithActionElements"]:has(h1) {
          display: none;
        }
      }
      [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] {gap: 0.55rem;}
      [data-testid="stMetric"] {padding: 0;}
      /* Metric rows breathe: a right gutter keeps long labels + the help "?"
         icon off the next column, and a row bottom-pad stops the verdict
         caption under the tiles from being painted over by the next block.
         On phones the fixed-width tiles are already gap-separated, so only the
         bottom-pad applies there (set in the mobile block above). */
      @media (min-width: 641px) {
        [data-testid="stMetric"] {padding-right: 1.5rem;}
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {
          padding-bottom: 0.9rem;
        }
      }
      /* KPI figures follow the design's stat block: Epilogue 700 value over a
         12px/500 muted label, delta rendered as a filled success/critical pill
         (branchable via the arrow icon's testid). !important beats the inline
         green/red Streamlit puts on the delta div. */
      [data-testid="stMetricValue"] {
        font-family: 'Epilogue', 'Instrument Sans', sans-serif;
        font-weight: 700; font-size: var(--ag-fs-xl); line-height: 1.1;
      }
      [data-testid="stMetricLabel"] p {
        font-size: var(--ag-fs-sm); font-weight: 500; color: var(--ag-text-secondary);
      }
      [data-testid="stMetricDelta"] {
        font-size: var(--ag-fs-xs); font-weight: 600;
        border-radius: var(--ag-radius-pill); padding: 1px 8px; width: fit-content;
      }
      [data-testid="stMetricDelta"]:has([data-testid="stMetricDeltaIcon-Up"]) {
        background: var(--ag-success-fill); color: var(--ag-up) !important;
      }
      [data-testid="stMetricDelta"]:has([data-testid="stMetricDeltaIcon-Down"]) {
        background: var(--ag-down-fill); color: var(--ag-down) !important;
      }
      [data-testid="stCaptionContainer"] p {font-size: var(--ag-fs-xs); margin-bottom: 0;}
      h1 {font-size: var(--ag-fs-3xl); padding: 0.2rem 0;}
      h2, h3 {padding: 0.2rem 0; margin-top: 0.3rem;}
      hr {margin: 0.4rem 0;}
      /* Section cards (st.container(border=True)) follow the design's card
         spec: neutral-900 surface over the neutral-950 page, 16px radius,
         soft dark shadow, roomier padding. Streamlit 1.60 dropped the old
         stVerticalBlockBorderWrapper testid and moved the border onto the
         inner stVerticalBlock, where a bordered block differs from a plain
         one only by computed style — the tagger script below stamps
         .topstocks-card on main-area blocks that carry a border. */
      [data-testid="stMainBlockContainer"]
        [data-testid="stVerticalBlock"].topstocks-card {
        background: var(--ag-surface-card);
        border-color: var(--ag-border);
        border-radius: var(--ag-radius-lg);
        box-shadow: var(--ag-shadow-card);
        padding: 1.1rem 1.2rem;
      }
      [data-testid="stElementToolbar"] {display: none;}
      /* Chart hover tooltips finish the DS card look. widgets.HOVERLABEL paints
         the neutral-900 surface, neutral-800 border and Instrument Sans text on
         Plotly's SVG box; radius and elevation have no hoverlabel equivalent, so
         they ride here. st.plotly_chart renders inline (not iframed), so this
         top-document CSS reaches the .hoverlayer. rx rounds the unified-hover
         rect; the drop-shadow lifts both the rect and the closest-hover
         path bubble off the plot. NOTE: never write a left angle bracket
         anywhere inside this style block, not even in a comment — DOMPurify
         silently drops the WHOLE block when its text contains one. */
      .js-plotly-plot .hoverlayer .hovertext > rect {
        rx: var(--ag-radius-md); ry: var(--ag-radius-md);
      }
      .js-plotly-plot .hoverlayer .hovertext > rect,
      .js-plotly-plot .hoverlayer .hovertext > path {
        filter: drop-shadow(var(--ag-shadow-hover));
      }
      /* Left-menu rows per the design: 6px radius, 13px/500 labels in muted
         neutral-400 with a 2px accent slot on the left; the active page gets
         the purple-900 fill, the purple-500 accent bar and full-strength
         text. aria-current marks the active link (Streamlit sets it). */
      [data-testid="stSidebarNavLink"] {
        border-radius: var(--ag-radius-nav);
        border-left: 2px solid transparent;
        margin: 1px 8px 1px 0;
        padding-top: 7px; padding-bottom: 7px;
        transition: background 100ms ease-in-out;
      }
      [data-testid="stSidebarNavLink"] span {
        color: var(--ag-text-secondary); font-size: var(--ag-fs-md); font-weight: 500;
      }
      [data-testid="stSidebarNavLink"]:hover {background: var(--ag-surface-hover);}
      [data-testid="stSidebarNavLink"][aria-current="page"] {
        background: var(--ag-purple-900);
        border-left-color: var(--ag-brand-accent);
      }
      [data-testid="stSidebarNavLink"][aria-current="page"] span {
        color: var(--ag-text-primary); font-weight: 600;
      }
      /* Nav group labels (Cartera / Mercado / Cuenta): tiny tracked caps in
         neutral-600, like the design's section headers. */
      [data-testid="stNavSectionHeader"] {
        font-size: var(--ag-fs-2xs); font-weight: 500; letter-spacing: 0.06em;
        text-transform: uppercase; color: var(--ag-text-faint);
      }
      /* Selector chips — the design's time-range buttons, applied to every
         segmented control and pills group: detached outlined chips instead of
         Streamlit's joined bar; the active chip gets the purple-900 fill,
         purple-500 border and purple-400 label. data-variant + data-selected
         are the stable hooks (emotion classes churn per release). */
      [data-testid="stButtonGroup"] > div[data-orientation] {gap: 4px;}
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"],
      [data-testid="stButtonGroup"] button[data-variant="pills"] {
        border: 1px solid var(--ag-border);
        border-radius: var(--ag-radius-nav);
        background: transparent;
        padding: 5px 14px;
        transition: border-color 50ms ease-in-out, background 50ms ease-in-out;
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"] p,
      [data-testid="stButtonGroup"] button[data-variant="pills"] p {
        color: var(--ag-text-secondary); font-size: var(--ag-fs-md); font-weight: 600;
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"]:hover,
      [data-testid="stButtonGroup"] button[data-variant="pills"]:hover {
        border-color: var(--ag-border-focus);
        background: transparent;
      }
      /* Range-selector hover per the DS: the label lifts to primary text;
         the fill never changes (spec 08 — "Tab inactiva / leyenda" family). */
      [data-testid="stButtonGroup"]
        button[data-variant="segmented_control"]:not([data-selected="true"]):hover p,
      [data-testid="stButtonGroup"]
        button[data-variant="pills"]:not([data-selected="true"]):hover p {
        color: var(--ag-text-primary);
      }
      [data-testid="stButtonGroup"]
        button[data-variant="segmented_control"][data-selected="true"],
      [data-testid="stButtonGroup"] button[data-variant="pills"][data-selected="true"] {
        background: var(--ag-purple-900);
        border-color: var(--ag-brand-accent);
      }
      [data-testid="stButtonGroup"]
        button[data-variant="segmented_control"][data-selected="true"] p,
      [data-testid="stButtonGroup"] button[data-variant="pills"][data-selected="true"] p {
        color: var(--ag-purple-400);
      }
      /* Chip groups that share a header row with a title sit flush right,
         against the card edge, instead of hugging the title. The widget key
         is the only hook Streamlit gives a single control. */
      [class*="st-key-tax_granularity"] [data-testid="stButtonGroup"]
        > div[data-orientation] {
        justify-content: flex-end;
      }
      /* Tabs — the DS tab spec (Aguait Tabs canvas): quiet underline nav, no
         chip wash. Rest labels in neutral-400 at 500; hover lifts the label
         to primary over a neutral-800 underline (an inset shadow, so the
         strip never reflows); the active tab is purple-500 at 600 over the
         sliding 2px accent bar. The full-width track stays the 1px
         neutral-800 rule. data-baseweb hooks are stable across releases;
         emotion classes are not. */
      [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 28px;
      }
      [data-testid="stTabs"] button[data-baseweb="tab"] {
        padding: 0 0 13px;
        border-radius: 0;
        background: transparent;
        transition: box-shadow 50ms ease-in-out;
      }
      [data-testid="stTabs"] button[data-baseweb="tab"] p {
        font-size: var(--ag-fs-lg); font-weight: 500;
        color: var(--ag-text-secondary);
        transition: color 50ms ease-in-out;
      }
      [data-testid="stTabs"]
        button[data-baseweb="tab"]:not([aria-selected="true"]):hover {
        box-shadow: inset 0 -2px 0 var(--ag-border);
      }
      [data-testid="stTabs"]
        button[data-baseweb="tab"]:not([aria-selected="true"]):hover p {
        color: var(--ag-text-primary);
      }
      [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] p {
        color: var(--ag-brand-accent); font-weight: 600;
      }
      [data-testid="stTabs"] [data-baseweb="tab-highlight"] {
        background: var(--ag-brand-accent); height: 2px;
      }
      [data-testid="stTabs"] [data-baseweb="tab-border"] {
        background: var(--ag-border); height: 1px;
      }
      /* Count badges in tab labels (markdown `:gray-badge[n]`): the spec's
         mono pill — neutral fill at rest, purple-800/purple-300 on the
         active tab. Overrides the badge directive's own palette. */
      [data-testid="stTabs"] button[data-baseweb="tab"]
        [data-testid="stMarkdownBadge"] {
        display: inline-flex; align-items: center; justify-content: center;
        min-width: 18px; height: 18px; padding: 0 5px;
        border-radius: var(--ag-radius-pill); vertical-align: middle;
        background: var(--ag-border); color: var(--ag-text-secondary);
        font-family: "Martian Mono", monospace;
        font-size: var(--ag-fs-2xs); font-weight: 500; line-height: 1;
      }
      [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"]
        [data-testid="stMarkdownBadge"] {
        background: var(--ag-purple-800); color: var(--ag-purple-300);
      }
      /* Buttons — DS component spec: the primary CTA carries the purple glow
         and darkens one step on hover (two when pressed); secondary stays
         outlined and washes the hover surface behind the focus-step border.
         50ms per the motion spec — no scale, no fades, no new shadows. */
      .stButton button, .stDownloadButton button, .stFormSubmitButton button {
        transition: background 50ms ease-in-out, border-color 50ms ease-in-out,
                    color 50ms ease-in-out;
      }
      .stButton button[kind="primary"],
      .stFormSubmitButton button[kind="primary"] {
        box-shadow: 0px 4px 12px var(--ag-cta-glow);
      }
      .stButton button[kind="primary"]:hover,
      .stFormSubmitButton button[kind="primary"]:hover {
        background-color: var(--ag-purple-700);
        border-color: var(--ag-purple-700);
      }
      .stButton button[kind="primary"]:active,
      .stFormSubmitButton button[kind="primary"]:active {
        background-color: var(--ag-purple-800);
        border-color: var(--ag-purple-800);
      }
      .stButton button[kind="secondary"]:hover,
      .stDownloadButton button:hover,
      .stFormSubmitButton button[kind="secondary"]:hover {
        background-color: var(--ag-surface-hover);
        border-color: var(--ag-border-focus);
        color: var(--ag-text-primary);
      }
      /* UI tooltips (widget help "?") — DS spec 08: page-tone surface, radius
         8, 6x10 padding, 12px primary text, dialog shadow, no arrow. */
      [data-testid="stTooltipContent"] {
        background: var(--ag-surface-page);
        border: 1px solid var(--ag-border);
        border-radius: var(--ag-radius-sm);
        box-shadow: var(--ag-shadow-overlay);
        padding: 6px 10px;
        font-size: var(--ag-fs-sm); color: var(--ag-text-primary);
      }
      /* Text-entry focus flips the border to the accent (DS search-field
         spec); the theme's primaryColor would paint the CTA purple instead. */
      [data-testid="stTextInput"] [data-baseweb="input"]:focus-within,
      [data-testid="stNumberInput"] [data-baseweb="input"]:focus-within,
      [data-testid="stTextArea"] [data-baseweb="textarea"]:focus-within {
        border-color: var(--ag-brand-accent) !important;
      }
      @media (max-width: 640px) {
        /* The strip scrolls sideways on phones (active tab auto-centers, see
           the tab-center script); the scrollbar under it is just noise. When
           the strip actually overflows (data-ts-overflow, set by the same
           script) a page-color fade + chevron cues the hidden tabs — a
           sticky flex item, not an absolute overlay, so it pins to the
           scrollport edge and spans the strip height with no magic numbers. */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
          gap: 24px; scrollbar-width: none;
        }
        [data-testid="stTabs"] [data-baseweb="tab-list"]::-webkit-scrollbar {
          display: none;
        }
        [data-testid="stTabs"] button[data-baseweb="tab"] {padding: 0 0 12px;}
        [data-testid="stTabs"]
          [data-baseweb="tab-list"][data-ts-overflow]::after {
          content: "";
          position: sticky; right: 0;
          flex: 0 0 44px; margin-left: -44px; align-self: stretch;
          pointer-events: none;
          /* Chevron stroke is TEXT_MUTED #827F8C — var() can't reach inside
             a data URI. The URI is wrapped with backslash continuations to
             stay inside the line limit; they join with no newline, so the
             continuation lines start at column 0 — any indent would land
             inside the URI. */
          background:
            url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' \
viewBox='0 0 24 24' fill='none' stroke='%23827F8C' stroke-width='1.5' \
stroke-linecap='round' stroke-linejoin='round'%3E\
%3Cpolyline points='9 18 15 12 9 6'/%3E%3C/svg%3E")
              right 6px center / 16px 16px no-repeat,
            linear-gradient(to right, transparent, var(--ag-surface-page) 70%);
        }
      }
      /* Toasts (stocks.web.notices — transient data-fetch notices) park
         bottom-LEFT. Streamlit anchors the container top-right, where it lands
         on the topbar search; bottom-right is the chat launcher's corner. The
         container is position:fixed with top/right from the theme and an inline
         top when the header is offset, so every side needs !important. The
         3.5rem lift keeps them off the very bottom edge, clear of any host
         chrome pinned there.
         Toasts stack downward via margin-top; column-reverse keeps the newest
         one nearest the bottom edge instead of drifting up the viewport. */
      [data-testid="stToastContainer"] {
        top: auto !important;
        bottom: 3.5rem !important;
        right: auto !important;
        left: 0 !important;
        flex-direction: column-reverse !important;
        align-items: flex-start !important;
      }
      /* ---- DS mobile spec (section 10) ---- Trailing block: these override
         base rules above, so they must stay LAST in this stylesheet. */
      @media (max-width: 640px) {
        /* 12px between cards, 16px inside them (vs the tighter desktop
           stack and its 1.1/1.2rem card padding). */
        [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] {
          gap: 0.75rem;
        }
        [data-testid="stMainBlockContainer"]
          [data-testid="stVerticalBlock"].topstocks-card {
          padding: 1rem;
        }
        /* 44px minimum touch targets on buttons, chips and tabs. */
        .stButton button, .stDownloadButton button, .stFormSubmitButton button,
        [data-testid="stButtonGroup"] button,
        [data-testid="stTabs"] button[data-baseweb="tab"] {
          min-height: 44px;
        }
        /* Period selector goes full-width and JOINED on phones: card-tone
           container, borderless flexed cells, active cell on purple-900 with
           a primary-text label. Segmented only — multi-row pills keep the
           detached chips. */
        [data-testid="stButtonGroup"] > div[data-orientation]:has(
            button[data-variant="segmented_control"]) {
          width: 100%;
          background: var(--ag-surface-card);
          border: 1px solid var(--ag-border);
          border-radius: var(--ag-radius-sm);
          padding: 3px;
        }
        [data-testid="stButtonGroup"] button[data-variant="segmented_control"] {
          flex: 1 1 0; min-width: 0; justify-content: center;
          border-color: transparent; padding: 5px 4px;
        }
        /* Never ellipsize a cell label ("20…"): the labels are 2-4 chars by
           design, so let them paint over the 4px cell padding instead. */
        [data-testid="stButtonGroup"] button[data-variant="segmented_control"]
          [data-testid="stMarkdownContainer"],
        [data-testid="stButtonGroup"] button[data-variant="segmented_control"] p {
          overflow: visible !important; text-overflow: clip !important;
          min-width: max-content;
        }
        [data-testid="stButtonGroup"]
          button[data-variant="segmented_control"]:hover,
        [data-testid="stButtonGroup"]
          button[data-variant="segmented_control"][data-selected="true"] {
          border-color: transparent;
        }
        [data-testid="stButtonGroup"]
          button[data-variant="segmented_control"][data-selected="true"] p {
          color: var(--ag-text-primary);
        }
        /* Toasts ride above the fixed bottom tab bar. */
        [data-testid="stToastContainer"] {bottom: 5.5rem !important;}
      }
      /* Touch devices: no hover states — controls wash the hover surface
         while pressed (DS mobile spec). The primary CTA keeps its own
         purple pressed step from the button rules above. */
      @media (hover: none) {
        [data-testid="stSidebarNavLink"]:active,
        .stButton button[kind="secondary"]:active,
        .stDownloadButton button:active,
        [data-testid="stButtonGroup"] button:not([data-selected="true"]):active,
        [class*="st-key-topbar_results"] button:active {
          background: var(--ag-surface-hover) !important;
        }
      }
    </style>
    """
)

# Loading skeletons (stocks.web.skeletons) — every fetching section shimmers a
# placeholder in its own shape instead of a spinner. Its style block lives with
# the shapes it paints; injected here so it is on the page before any page body
# runs, and once per rerun rather than once per skeleton.
css.inject(skeletons.CSS)

# Mobile KPI figures. metric_cells packs the headline numbers into ~110px
# fixed-width tiles on phones, where the 1.35rem base value (€112,432) overruns
# the box and Streamlit truncates it with an ellipsis. is_mobile() is a
# server-side User-Agent check with no viewport width, so a CSS @media query
# can't gate this (the phone's CSS viewport may exceed 640px). Inject the
# override only when the request is mobile — matching exactly when the fixed
# tiles render — and kill the clip so the whole number always shows. Loaded
# after the base <style> above, so it wins on source order.
if is_mobile():
    css.inject(
        """
        <style>
          [data-testid="stMetricValue"] {
            font-size: var(--ag-fs-md) !important; line-height: 1.15;
          }
          [data-testid="stMetricValue"],
          [data-testid="stMetricValue"] * {
            overflow: visible !important; text-overflow: clip !important;
            white-space: nowrap !important;
          }
        </style>
        """
    )
    # Touch chart readout — the DS mobile chart spec: the floating tooltip is
    # replaced by a fixed reading row pinned to the chart's top-left (fecha ·
    # precio · variación) that updates as the finger moves; the crosshair
    # stays. The row's text is lifted from the tooltip Plotly already resolved
    # (so every chart's custom hover template survives verbatim) and the
    # floating boxes themselves are hidden. Injected after the base styles, so
    # these rules win on source order. NOTE: no left angle bracket may appear
    # inside the style block (DOMPurify drops the whole block).
    st.html(
        """
        <style>
          [data-testid="stPlotlyChart"] { position: relative; }
          .ts-chart-readout {
            position: absolute; top: 8px; left: 8px; z-index: 5;
            background: var(--ag-surface-page-veil);
            border-radius: var(--ag-radius-sm);
            padding: 4px 8px; pointer-events: none;
            font-size: var(--ag-fs-sm); color: var(--ag-text-primary);
            max-width: calc(100% - 16px);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          }
          .ts-chart-readout:empty { display: none; }
          /* Hide the floating boxes; spike/crosshair lines live outside these
             groups and stay visible. */
          .js-plotly-plot .hoverlayer g.legend,
          .js-plotly-plot .hoverlayer g.hovertext { display: none; }
        </style>
        <script>
        (function () {
          if (window.__topstocksChartReadout) return;  /* wire once */
          window.__topstocksChartReadout = true;
          const wired = new WeakSet();
          const rowOf = (gd) => {
            const host =
              gd.closest('[data-testid="stPlotlyChart"]') || gd.parentElement;
            if (!host) return null;
            let ro = host.querySelector(".ts-chart-readout");
            if (!ro) {
              /* Streamlit replaces the host's children on rerun, so the row is
                 looked up (and recreated) per event, never captured. */
              ro = document.createElement("div");
              ro.className = "ts-chart-readout";
              host.appendChild(ro);
            }
            return ro;
          };
          const read = (gd) => {
            const layer = gd.querySelector(".hoverlayer");
            const ro = rowOf(gd);
            if (!layer || !ro) return;
            const parts = [];
            layer.querySelectorAll("text").forEach((t) => {
              const s = Array.from(t.childNodes)
                .map((n) => n.textContent).join(" ")
                .replace(/\\s+/g, " ").trim();
              if (s && parts.indexOf(s) === -1) parts.push(s);
            });
            /* Cap the row: a unified box can carry OHLC plus both SMAs, which
               would just ellipsize away on a phone. */
            if (parts.length) ro.textContent = parts.slice(0, 4).join(" \u00b7 ");
          };
          const wire = () => {
            document.querySelectorAll(".js-plotly-plot").forEach((gd) => {
              if (typeof gd.on !== "function" || wired.has(gd)) return;
              wired.add(gd);
              gd.on("plotly_hover", () => requestAnimationFrame(() => read(gd)));
              /* The row is a live readout, not a caption: it clears with the
                 crosshair so a stale figure never sits over the chart. */
              const clear = () => {
                const ro = rowOf(gd);
                if (ro) ro.textContent = "";
              };
              gd.on("plotly_unhover", () => requestAnimationFrame(clear));
              /* Plotly resolves hover from mouse events only; bridge touch
                 drags onto it so the readout tracks the finger (DS mobile
                 chart spec). dragmode is off on phones, so nothing pans. */
              const drag = gd.querySelector(".nsewdrag") || gd;
              drag.addEventListener("touchmove", (ev) => {
                const t = ev.touches && ev.touches[0];
                if (!t) return;
                drag.dispatchEvent(new MouseEvent("mousemove", {
                  clientX: t.clientX, clientY: t.clientY, bubbles: true,
                }));
              }, {passive: true});
              /* Lifting the finger fires no unhover on touch; retire the row
                 shortly after so it does not linger over the plot. */
              drag.addEventListener("touchend", () => {
                clearTimeout(gd.__tsReadoutTimer);
                gd.__tsReadoutTimer = setTimeout(clear, 2000);
              }, {passive: true});
            });
          };
          let queued = false;
          const schedule = () => {
            if (queued) return;
            queued = true;
            requestAnimationFrame(() => { queued = false; wire(); });
          };
          new MutationObserver(schedule).observe(document.body, {
            subtree: true, childList: true,
          });
          wire();
        })();
        </script>
        """,
        unsafe_allow_javascript=True,
    )

# Card tagger for the CSS above. Bordered and borderless st.container() render
# identical DOM in Streamlit 1.60 (same stVerticalBlock testid; the border is
# an emotion class whose hash changes per release), so the stable signal is
# the computed border width. Runs in the top document (st.html is not
# iframed); the MutationObserver re-tags blocks Streamlit re-renders. The
# main-area scope keeps sidebar scroll regions (also bordered) untouched.
st.html(
    """
    <script>
    (function () {
      if (window.__topstocksCardTagger) return;  /* survive reruns — wire once */
      window.__topstocksCardTagger = true;
      const tag = () => {
        document
          .querySelectorAll(
            '[data-testid="stMainBlockContainer"] ' +
            '[data-testid="stVerticalBlock"]:not(.topstocks-card)'
          )
          .forEach((el) => {
            if (parseFloat(getComputedStyle(el).borderTopWidth) > 0) {
              el.classList.add("topstocks-card");
            }
          });
      };
      new MutationObserver(tag).observe(document.body, {subtree: true, childList: true});
      tag();
    })();
    </script>
    """,
    unsafe_allow_javascript=True,
)

# Keep the ACTIVE tab centered in overflowing tab bars. On phones the tab bar
# scrolls horizontally; without this, selecting the right-most visible tab
# leaves the next one off-screen, so you can never see where to go. Centering
# on selection (and on load, for the tab restored from ?tab=) always keeps a
# neighbor visible on each side. Desktop is untouched: the scroll only fires
# when the list actually overflows. scrollTo on the list itself — not
# scrollIntoView — so the page never jumps vertically. The same wiring stamps
# data-ts-overflow on overflowing strips, which gates the mobile edge-fade
# CSS above.
st.html(
    """
    <script>
    (function () {
      if (window.__topstocksTabCenter) return;  /* survive reruns — wire once */
      window.__topstocksTabCenter = true;
      /* data-ts-overflow drives the mobile edge-fade CSS: only a strip that
         actually scrolls gets the fade + chevron cue. */
      const flag = (list) =>
        list.toggleAttribute(
          "data-ts-overflow", list.scrollWidth > list.clientWidth + 1);
      const center = (tab) => {
        const list = tab && tab.closest('[data-baseweb="tab-list"]');
        if (!list) return;
        flag(list);
        if (list.scrollWidth <= list.clientWidth + 1) return;
        const lr = list.getBoundingClientRect();
        const tr = tab.getBoundingClientRect();
        const left =
          list.scrollLeft + (tr.left - lr.left) - (lr.width - tr.width) / 2;
        list.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
      };
      /* Selection flips aria-selected on the newly active tab. */
      new MutationObserver((muts) => {
        muts.forEach((m) => {
          if (m.target.getAttribute("aria-selected") === "true") center(m.target);
        });
      }).observe(document.body, {
        subtree: true,
        attributes: true,
        attributeFilter: ["aria-selected"],
      });
      /* Fresh tab bars (page load / rerun re-render): center their active tab. */
      const init = () => {
        document
          .querySelectorAll('[data-baseweb="tab-list"]:not([data-ts-centered])')
          .forEach((list) => {
            list.setAttribute("data-ts-centered", "1");
            flag(list);
            center(list.querySelector('[aria-selected="true"]'));
          });
      };
      new MutationObserver(init).observe(document.body, {subtree: true, childList: true});
      init();
      /* Rotation / viewport resize can flip a strip in or out of overflow. */
      window.addEventListener("resize", () => {
        document
          .querySelectorAll('[data-baseweb="tab-list"]')
          .forEach(flag);
      });
    })();
    </script>
    """,
    unsafe_allow_javascript=True,
)

# Click-to-sort for the HTML ticker tables (widgets.ticker_table_html with a
# `sortable` id). Those tables are pandas Styler markup, not st.dataframe, so
# they had no sorting at all — but every body cell ships a data-s attribute
# holding its RAW value, so sorting is a pure DOM reorder here: no rerun, no
# refetch, no re-render of the 20-row price burst behind it. The chosen
# column/direction lives in sessionStorage per table id, so a rerun (tab
# switch, widget change) re-applies it to the freshly rendered table instead
# of snapping back to weight order. Numeric columns open on descending
# (biggest position/gain first); text columns open A→Z. Blanks ("n/a" prices)
# always sort last, whichever direction.
st.html(
    r"""
    <script>
    (function () {
      if (window.__topstocksTableSort) return;  /* survive reruns — wire once */
      window.__topstocksTableSort = true;
      let store = null;
      try { store = window.sessionStorage; } catch (e) { /* blocked — no memory */ }
      const cell = (row, ci) => {
        const td = row.querySelector("td.col" + ci);
        return td ? (td.getAttribute("data-s") || "") : "";
      };
      const numeric = (rows, ci) => rows.every((r) => {
        const v = cell(r, ci);
        return v === "" || !isNaN(Number(v));
      });
      const apply = (table, ci, dir) => {
        const body = table.tBodies[0];
        if (!body) return;
        const rows = Array.from(body.rows);
        const num = numeric(rows, ci);
        rows.sort((a, b) => {
          const x = cell(a, ci), y = cell(b, ci);
          if (x === "" || y === "") return x === y ? 0 : (x === "" ? 1 : -1);
          const c = num ? Number(x) - Number(y) : x.localeCompare(y);
          return dir === "desc" ? -c : c;
        });
        rows.forEach((r) => body.appendChild(r));
        table.querySelectorAll("th[data-ag-dir]")
             .forEach((th) => th.removeAttribute("data-ag-dir"));
        table.querySelectorAll("th .ag-arrow").forEach((e) => e.remove());
        const th = table.querySelector("thead th.col" + ci);
        if (th) {
          th.setAttribute("data-ag-dir", dir);
          const arrow = document.createElement("span");
          arrow.className = "ag-arrow";
          arrow.textContent = dir === "desc" ? " \u2193" : " \u2191";
          th.appendChild(arrow);
        }
      };
      const wire = () => {
        document
          .querySelectorAll("[data-ag-sort] table:not([data-ag-wired])")
          .forEach((table) => {
            table.setAttribute("data-ag-wired", "1");
            const key = "ag-sort:" +
              table.closest("[data-ag-sort]").getAttribute("data-ag-sort");
            const heads = table.querySelectorAll("thead th.col_heading");
            heads.forEach((th) => {
              const m = /\bcol(\d+)\b/.exec(th.className);
              if (!m) return;
              const ci = Number(m[1]);
              th.addEventListener("click", () => {
                const rows = Array.from(table.tBodies[0].rows);
                const first = numeric(rows, ci) ? "desc" : "asc";
                const prev = (store && store.getItem(key) || "").split(":");
                const dir = (Number(prev[0]) === ci && prev[1] === first)
                  ? (first === "desc" ? "asc" : "desc")
                  : first;
                if (store) store.setItem(key, ci + ":" + dir);
                apply(table, ci, dir);
              });
            });
            const saved = (store && store.getItem(key) || "").split(":");
            if (saved.length === 2 && saved[0] !== "") {
              apply(table, Number(saved[0]), saved[1]);
            }
          });
      };
      new MutationObserver(wire).observe(document.body, {subtree: true, childList: true});
      wire();
    })();
    </script>
    """,
    unsafe_allow_javascript=True,
)

# Left-drawer rail (desktop only). Streamlit's collapsed sidebar slides fully
# off-screen; instead keep it as a slim rail showing the app + page + ticker
# glyphs, and expand the full panel on hover. The expand is an OVERLAY — the
# rail's flex box stays --rail-w wide so the main content never reflows on
# hover; the inner scroll wrapper (which carries the opaque panel bg) is what
# grows out over the page. Phones keep the hidden sidebar.
css.inject(
    """
    <style>
    @media (min-width: 641px) {
      :root {--rail-w: 72px; --rail-open: 21rem;}

      /* Collapsed sidebar: pin it open as a slim rail instead of translating
         it off-screen. overflow:visible lets the hover panel spill over main. */
      section[data-testid="stSidebar"][aria-expanded="false"] {
        min-width: var(--rail-w) !important;
        max-width: var(--rail-w) !important;
        transform: none !important;
        overflow: visible !important;
        transition: none !important;
      }
      /* showSidebarBorder's line is painted by the sidebar RESIZE HANDLE — an
         8px col-resize div pinned to the section's right edge. The section stays
         at rail-w, so the handle sits at 72px. In the glyph rail that gradient
         is the wanted right divider; on hover the content overlays wider but the
         handle stays at 72px, leaving a stray vertical line through the open
         panel. Hide it on hover — the panel's own edge border rides on the
         stSidebarContent:hover rule below. `.eelgd2m3` is Streamlit's emotion
         target class for that handle; the sibling selector is a hash-free
         fallback (the handle is the div sibling of stSidebarContent). Both are
         version-coupled like the testids in this block — revisit on upgrades. */
      section[data-testid="stSidebar"][aria-expanded="false"]:hover .eelgd2m3,
      section[data-testid="stSidebar"][aria-expanded="false"]:hover
        [data-testid="stSidebarContent"] ~ div {
        display: none !important;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]
        [data-testid="stSidebarContent"] {
        width: var(--rail-w);
        overflow-x: hidden;
        background: var(--ag-surface-page); /* opaque panel tone over the page */
        transition: width 180ms ease;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:hover
        [data-testid="stSidebarContent"] {
        width: var(--rail-open);
        overflow-y: auto;
        box-shadow: 6px 0 2.5rem var(--ag-shadow-color-strong);
        border-right: 1px solid var(--ag-border);
      }

      /* ---- Rail glyph-only state (collapsed, not hovered) ---- */

      /* Rail and hover panel must share the SAME vertical metrics — logo
         height, row padding/line-height, header heights — so glyphs stay put
         while the panel slides out. Hover-only rules below are limited to
         hiding/centering, never to anything that changes an element's height. */

      /* App logo pinned at the top, centered; collapse arrow hidden until
         hover. 40px in BOTH states (hover used to fall back to the 2rem
         default, resizing the logo mid-slide). */
      section[data-testid="stSidebar"][aria-expanded="false"]
        [data-testid="stSidebarHeader"] {
        justify-content: center;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]
        [data-testid="stSidebarLogo"] {
        height: 40px; width: auto;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stSidebarLogo"] {
        margin: 0 auto;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stSidebarCollapseButton"] {
        display: none;
      }

      /* Page-nav rows: same padding in rail and hover panel. The 1.6rem
         line-height matches the forced glyph size below — without it the
         13px labels ride the theme's 2.0 menu-item line box on hover and
         every row grows, shifting the column. */
      section[data-testid="stSidebar"][aria-expanded="false"]
        [data-testid="stSidebarNavLink"] {
        padding-top: 0.4rem; padding-bottom: 0.4rem;
        line-height: 1.6rem;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stSidebarNavLink"] {
        justify-content: center;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stSidebarNavLink"] > span + span {
        display: none;
      }

      /* Section headers (Cartera / Mercado / Cuenta): keep the FULL word in
         the rail instead of an ellipsised first letter. The 72px rail minus
         stSidebarContent's ~20px side paddings leaves ~32px, so the negative
         margins buy that padding back (~60px) and the font drops a notch to
         fit "PORTFOLIO". The chevron slot is removed in the rail but its
         1.25rem height is pinned on the header for both states, so the rows
         below never move on hover. */
      section[data-testid="stSidebar"][aria-expanded="false"]
        [data-testid="stNavSectionHeader"] {
        min-height: 1.25rem;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stNavSectionHeader"] {
        justify-content: center;
        margin-left: -14px; margin-right: -14px;
        padding-right: 0; gap: 0;
        font-size: var(--ag-fs-2xs); letter-spacing: 0.04em;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stNavSectionHeader"] > div {
        display: none;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stNavSectionHeader"] * {
        overflow: visible !important; text-overflow: clip !important;
      }
      /* Streamlit sets the icon size inline, so !important is needed to grow it.
         Keep the SAME size in every state (collapsed rail, hover panel, pinned
         open) so the glyph never resizes and shifts the row layout. The glyph
         sits inside TWO wrapper spans that emotion pins at the theme's 1rem
         icon size — grow them too, or the row's layout height stays 1rem and
         the row shrinks whenever the label is hidden (the rail state). */
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]
        [data-testid="stIconMaterial"],
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]
        > span:first-child,
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"]
        > span:first-child span {
        font-size: var(--ag-icon-nav) !important;
        width: var(--ag-icon-nav) !important;
        height: var(--ag-icon-nav) !important;
      }

      /* Minimized rail carries only the app logo + nav glyphs. Hide the rest
         of the drawer's own content (sign-in button, feedback entry point) —
         it slides back in with the panel on hover. */
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover)
        [data-testid="stSidebarUserContent"] {
        display: none !important;
      }

      /* Kill the floating ">>" expand control and the duplicate collapsed logo
         Streamlit drops into the top toolbar — the rail carries both now — so
         the top-padding reclaim for that control no longer applies. */
      [data-testid="stExpandSidebarButton"] {display: none !important;}
      [data-testid="stHeaderLogo"] {display: none !important;}
      [data-testid="stApp"]:has([data-testid="stExpandSidebarButton"]) .block-container {
        padding-top: 1.2rem;
      }
    }
    </style>
    """
)

# Browsing is public: anonymous visitors get the shared read-only guest
# watchlist. resolve_user() puts the session's data paths (watchlist, ledger,
# prefs) in session state; the Portfolio/Import/Profile pages and mutating
# widgets gate themselves with require_login()/is_logged_in().
auth.resolve_user()

# Observability: from here on every log record this run emits (from any module)
# carries the session, the account and — once the nav resolves — the page. See
# stocks.obs for the emit side and `stocks logs` for the query side.
telemetry.bind_run()

# Resolve the run's language (Profile pref > browser locale > English) before
# the nav is built and any page runs, so page titles and page bodies read one
# stable value. A Profile change lands on its rerun, which re-runs this first.
i18n.set_active_language()

# Arriving from the landing page (served outside this script by server.py, see
# stocks.web.landing_static): its CTAs are links, so the click shows up here as
# a query parameter — ?signin=1 starts the OIDC round-trip, ?lang= pins the
# language the visitor was reading. After set_active_language(), so the
# parameter wins over the resolved default; before the nav, because st.login()
# redirects and nothing after it runs.
landing.consume_params()

# Signed-in first load: the guided tour for a brand-new account, "what's new"
# for one that has already taken it and missed a release. Only one modal can be
# open per run, so the tour claims the run when it fires and the investor-
# profile nudge stands down — the profile is one of the tour's own steps.
# Both are no-ops for guests; the tour itself is still reachable by hand.
if not onboarding.maybe_open():
    # Nudge the user to set up their investor profile so the assistant tailors
    # its analysis. Skippable; nags again next session until set (or filled
    # from the Profile page).
    auth.maybe_prompt_profile()

ticker_page = st.Page(
    "app_pages/ticker.py",
    title=tr("nav.ticker"),
    icon=":material/query_stats:",
    url_path="ticker",
)

_portfolio_pages = [
    st.Page(
        "app_pages/portfolio.py",
        title=tr("nav.portfolio"),
        icon=":material/pie_chart:",
    ),
    st.Page(
        "app_pages/import_transactions.py",
        title=tr("nav.import"),
        icon=":material/upload_file:",
    ),
]
# Grouped like the design's left menu: Inicio on top, then the Cartera and
# Mercado sections, with the account entry in its own bottom group.
page = st.navigation(
    {
        "": [
            st.Page(
                "app_pages/home.py",
                title=tr("nav.home"),
                icon=":material/home:",
                default=True,
            ),
        ],
        tr("nav.section_portfolio"): _portfolio_pages,
        tr("nav.section_market"): [
            ticker_page,
            st.Page(
                "app_pages/sentiment.py",
                title=tr("nav.sentiment"),
                icon=":material/speed:",
            ),
            st.Page(
                "app_pages/screener.py",
                title=tr("nav.screener"),
                icon=":material/filter_alt:",
            ),
            st.Page(
                "app_pages/earnings.py",
                title=tr("nav.earnings"),
                icon=":material/calendar_month:",
            ),
        ],
        tr("nav.section_account"): [
            st.Page(
                "app_pages/profile.py",
                title=tr("nav.profile"),
                icon=":material/account_circle:",
            ),
        ],
    }
)

# Anonymous visitors get a sign-in entry point on every page; the gated
# pages (Portfolio, Import, Profile) render a full login screen themselves.
if "auth" in st.secrets and not auth.is_logged_in():
    st.sidebar.button(
        tr("common.sign_in_google"),
        icon=":material/login:",
        on_click=auth.login,
        width="stretch",
    )

# Deep link: ?ticker=SYM selects that symbol (applied once per new URL value,
# so it doesn't fight the search box). Away from the Ticker page it also jumps
# there — keeps pre-refactor /?ticker= bookmarks and table links working.
_qp = (st.query_params.get("ticker") or "").strip().upper()
if _qp and st.session_state.get("_url_ticker") != _qp:
    st.session_state["picker_selected"] = _qp
    st.session_state["_url_ticker"] = _qp
    if page.url_path != ticker_page.url_path:
        st.switch_page(ticker_page)

# The tour's pending navigation, from a "take me there" click on the previous
# run: it may switch pages (ending this run before anything is drawn) and it
# seeds the state of in-page targets like the assistant panel, so it has to
# come before the topbar and the panel below.
onboarding.consume_goto(page)

# Selection only, no UI: ticker navigation is the top-bar search (plus
# ?ticker= links), so the drawer carries just the page nav. Seeding runs above
# page.run() so every page starts with a ticker in hand. A pick from the
# top-bar results sets "picker_clicked", which routes to the Ticker page.
seed_selection()
_clicked = st.session_state.pop("picker_clicked", False)
if _clicked and page.url_path != ticker_page.url_path:
    st.switch_page(ticker_page)

# Feedback entry point, bottom of the sidebar on every page. Guests included:
# a visitor who bounced off the pitch knowing why is worth more than a login.
feedback.render_sidebar(page.title)

# Sticky top bar + global ticker search. The breadcrumb "you are here" strip is
# desktop-only (phones already carry the native header and the page's own
# heading), but the search field rides the top strip on every width — sitting
# beside the assistant launcher so the menu toggle, search and chat button share
# one row on phones too. render_topbar handles that per-width split internally.
_focus = (
    st.session_state.get("picker_selected")
    if page.url_path == ticker_page.url_path
    else None
)
render_topbar(page.title, _focus)

# Phones swap the sidebar for the DS bottom tab bar (Inicio · Cartera ·
# Screener · Perfil); the drawer stays behind the header's menu toggle for the
# remaining pages. Rendered before page.run() like the topbar, so a page that
# crashes or st.stop()s still leaves the primary navigation standing.
if is_mobile():
    render_bottom_nav(page.url_path)

# Assistant overlay: a top-right launcher icon + slide-in chat panel, reachable
# from every page and carrying the current view (page + focused ticker) as
# context. The panel is fully self-contained (provider choice, key entry, chat),
# so there is no separate Chat page in the nav. Signed-in only — it reads the
# account's real book. Rendered BEFORE page.run(): the launcher is position:
# fixed (DOM order irrelevant) and must survive pages that crash or st.stop()
# mid-run — an uncaught page exception used to eat the button entirely.
if auth.is_logged_in():
    chat_core.render_side_panel(page.title)

# Yahoo throttles datacenter egress IPs; when the fetch layer's
# backoff (stocks.data.fetch._retry) is exhausted the error would otherwise
# surface as Streamlit's opaque crash page. Degrade to a banner instead —
# st.cache_data never caches exceptions, so a rerun retries the failed fetches
# while every cached section keeps rendering.
# `page.render` times the whole script body and, crucially, logs the exception
# of any page that crashes — which used to reach the user as Streamlit's red
# box and reach us not at all. Fragment reruns never re-enter this file, so the
# timing covers full runs only.
# The guided tour's visible half: the modal when it is open, the thin resume
# strip when the user sent themselves to a page to look at the feature being
# explained. After the topbar (which has to be the main column's first element
# to stay sticky) and before page.run(), so the strip sits between the two and
# every page gets it for free — including pages that st.stop() at their login
# gate.
onboarding.render(page)

with obs.timed("page.render", passthrough=telemetry.CONTROL_FLOW, page=page.title):
    try:
        page.run()
    except (YFRateLimitError, URLError) as exc:
        # Backstop only: the fetching sections catch this pair themselves and
        # degrade in place (they must — a fragment rerun never re-enters this
        # file, see stocks.web.notices). What still lands here is a fetch in the
        # non-fragment page body; the toast explains the gap in what did render.
        obs.warn("data.degraded", error_type=type(exc).__name__,
                 error=str(exc)[:200], page=page.title)
        notices.data_toast(exc)
