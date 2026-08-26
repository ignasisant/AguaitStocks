"""Streamlit entry point — st.navigation over the app_pages/ modules.

Run: uv run stocks dashboard   (or: uv run streamlit run src/stocks/web/app.py)

Page config, the dense-layout CSS and the nav are defined once here; the page
modules under app_pages/ carry only their own content. Colors and fonts live
in .streamlit/config.toml — the CSS below is only spacing the theme can't
express.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import URLError

# Streamlit Community Cloud runs this file straight from the repo checkout;
# make src/ importable there (and pin imports to the source tree locally).
_SRC = str(Path(__file__).resolve().parents[2])
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import streamlit as st  # noqa: E402
from yfinance.exceptions import YFRateLimitError  # noqa: E402

from stocks.web import auth  # noqa: E402
from stocks.web import chat_core  # noqa: E402
from stocks.web import i18n  # noqa: E402
from stocks.web import notices  # noqa: E402
from stocks.web import skeletons  # noqa: E402
from stocks.web.i18n import t as tr  # noqa: E402
from stocks.web.widgets import (  # noqa: E402
    ds_vars_css,
    is_mobile,
    render_topbar,
    ticker_picker,
)

# Aguait — Catalan "estar a l'aguait": to be on the lookout.
_ASSETS = Path(__file__).parent / "assets"
st.set_page_config(
    page_title="Aguait Stocks",
    page_icon=str(_ASSETS / "aguait-icon.svg"),
    layout="wide",
)
st.logo(
    str(_ASSETS / "aguait-logo.svg"),
    icon_image=str(_ASSETS / "aguait-icon.svg"),
    size="large",
)

# Design tokens first: every stylesheet below (and on every page, and inside
# the CCv2 shadow roots) reads its colors, radii, elevations and type steps
# from the `--ag-*` custom properties this emits. widgets.py owns the values;
# nothing downstream writes a raw hex. Must precede the blocks that use them.
st.html(ds_vars_css())

# Dense layout: kill Streamlit's default top padding and wide element gaps so
# charts and metrics sit high and tight instead of floating in whitespace.
st.html(
    """
    <style>
      /* Epilogue — the DS display face for KPI numbers; config.toml can only
         load one body/heading/code font each, so it rides in here. */
      @import url('https://fonts.googleapis.com/css2?family=Epilogue:wght@600;700;800&display=swap');
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
        [data-testid="stApp"]:has([data-testid="stExpandSidebarButton"]) .block-container {
          padding-top: 3.5rem;
        }
      }
      @media (max-width: 640px) {
        /* The fixed header (3.75rem) overlays content on phones; clear it
           exactly instead of relying on the accumulated hidden-element gaps
           below to push the first heading past it. */
        .block-container {padding-left: 0.75rem; padding-right: 0.75rem;
                          padding-top: 4rem;}
        /* Script-only st.html containers (the card tagger, chat wiring) are
           zero-height but still flex items, so each contributes one 0.55rem
           vertical-block gap above the first heading. (Style-only blocks are
           exempt: Streamlit routes them to the event container, out of the
           page flow.) Desktop swallows the stragglers under the breadcrumb
           bar's negative margin; phones have no bar, so hide them — scripts
           run on mount regardless of display. */
        [data-testid="stElementContainer"]:has(> [data-testid="stHtml"] > script:only-child) {
          display: none;
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
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {padding-bottom: 1.1rem;}
        /* Full-bleed charts: the card's 1.2rem side padding costs ~38px of
           plot width on a ~390px screen. Negative margins let the chart span
           the card edge-to-edge (same bleed trick as the topbar); text and
           metrics keep the card padding. Plotly measures its container after
           CSS applies, so the widened box is picked up on first render. */
        .aguait-card [data-testid="stElementContainer"]:has(> [data-testid="stPlotlyChart"]) {
          margin-left: -1.2rem; margin-right: -1.2rem;
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
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {padding-bottom: 0.9rem;}
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
        background: var(--ag-critical-fill); color: var(--ag-down) !important;
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
         .aguait-card on main-area blocks that carry a border. */
      [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"].aguait-card {
        background: var(--ag-surface-card);
        border-color: var(--ag-border);
        border-radius: var(--ag-radius-lg);
        box-shadow: var(--ag-shadow-card);
        padding: 1.1rem 1.2rem;
      }
      [data-testid="stElementToolbar"] {display: none;}
      /* Community Cloud's "Manage app" pill (owner) / "Hosted with Streamlit"
         badge (viewers) is fixed bottom-right above our z-stack and covers the
         chat panel's send button. Park it bottom-LEFT instead — still usable,
         off the chat input. These are the badge's known stable hooks; its
         CSS-module class hashes churn per cloud deploy, so the badge-mover
         script below is the real guarantee. */
      [data-testid="manage-app-button"],
      div[class*="viewerBadge"] {left: 0.75rem !important; right: auto !important;}
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
        border-radius: var(--ag-radius-sm);
        border-left: 2px solid transparent;
        margin: 1px 8px 1px 0;
        padding-top: 0.3rem; padding-bottom: 0.3rem;
      }
      [data-testid="stSidebarNavLink"] span {
        color: var(--ag-text-secondary); font-size: var(--ag-fs-md); font-weight: 500;
      }
      [data-testid="stSidebarNavLink"]:hover {background: var(--ag-surface-card);}
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
        border-radius: var(--ag-radius-sm);
        background: transparent;
        padding: 5px 14px;
        transition: border-color 100ms ease-in-out, background 100ms ease-in-out;
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"] p,
      [data-testid="stButtonGroup"] button[data-variant="pills"] p {
        color: var(--ag-text-secondary); font-size: var(--ag-fs-md); font-weight: 600;
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"]:hover,
      [data-testid="stButtonGroup"] button[data-variant="pills"]:hover {
        border-color: var(--ag-text-faint);
        background: transparent;
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"][data-selected="true"],
      [data-testid="stButtonGroup"] button[data-variant="pills"][data-selected="true"] {
        background: var(--ag-purple-900);
        border-color: var(--ag-brand-accent);
      }
      [data-testid="stButtonGroup"] button[data-variant="segmented_control"][data-selected="true"] p,
      [data-testid="stButtonGroup"] button[data-variant="pills"][data-selected="true"] p {
        color: var(--ag-purple-400);
      }
      /* Toasts (stocks.web.notices — transient data-fetch notices) park
         bottom-LEFT. Streamlit anchors the container top-right, where it lands
         on the topbar search; bottom-right is the chat launcher's corner. The
         container is position:fixed with top/right from the theme and an inline
         top when the header is offset, so every side needs !important. The
         3.5rem lift clears the Community Cloud "Manage app" badge, which the
         script below parks bottom-left too (it lives in the parent shell
         document, so it paints over this iframe regardless of z-index).
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
    </style>
    """
)

# Loading skeletons (stocks.web.skeletons) — every fetching section shimmers a
# placeholder in its own shape instead of a spinner. Its style block lives with
# the shapes it paints; injected here so it is on the page before any page body
# runs, and once per rerun rather than once per skeleton.
st.html(skeletons.CSS)

# Mobile KPI figures. metric_cells packs the headline numbers into ~110px
# fixed-width tiles on phones, where the 1.35rem base value (€112,432) overruns
# the box and Streamlit truncates it with an ellipsis. is_mobile() is a
# server-side User-Agent check with no viewport width, so a CSS @media query
# can't gate this (the phone's CSS viewport may exceed 640px). Inject the
# override only when the request is mobile — matching exactly when the fixed
# tiles render — and kill the clip so the whole number always shows. Loaded
# after the base <style> above, so it wins on source order.
if is_mobile():
    st.html(
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
      if (window.__aguaitCardTagger) return;  /* survive reruns — wire once */
      window.__aguaitCardTagger = true;
      const tag = () => {
        document
          .querySelectorAll(
            '[data-testid="stMainBlockContainer"] ' +
            '[data-testid="stVerticalBlock"]:not(.aguait-card)'
          )
          .forEach((el) => {
            if (parseFloat(getComputedStyle(el).borderTopWidth) > 0) {
              el.classList.add("aguait-card");
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
      if (window.__aguaitTableSort) return;  /* survive reruns — wire once */
      window.__aguaitTableSort = true;
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

# Badge mover for the Community Cloud "Manage app" / "Hosted with Streamlit"
# pill: it sits fixed bottom-right over the chat panel's send button, so park
# it bottom-left. On Community Cloud the app runs inside a same-origin iframe
# (src //<host>/~/+/, sandbox includes allow-same-origin) under the cloud
# shell SPA, and the badge lives in the PARENT shell document — querying our
# own document never finds it. So walk up to window.parent when reachable and
# operate there; falls back to our own document when embedded cross-origin or
# running locally (where no badge exists anyway). The shell's class hashes
# change per deploy, so match the stable data-testid plus label text, and
# re-anchor the outermost fixed-position ancestor. The stApp guard only
# matters in the own-document fallback. Badge mounts async, hence the
# MutationObserver.
st.html(
    """
    <script>
    (function () {
      if (window.__aguaitBadgeMover) return;  /* survive reruns — wire once */
      window.__aguaitBadgeMover = true;
      let doc = document;
      try {
        if (window.parent !== window && window.parent.document.body) {
          doc = window.parent.document;
        }
      } catch (e) { /* cross-origin parent — keep own document */ }
      const LABELS = ["manage app", "hosted with streamlit", "made with streamlit"];
      const move = () => {
        doc.querySelectorAll('[data-testid="manage-app-button"], button, a').forEach((el) => {
          if (el.closest('[data-testid="stApp"]')) return;
          if (el.getAttribute("data-testid") !== "manage-app-button" &&
              !LABELS.includes((el.textContent || "").trim().toLowerCase())) return;
          let fixed = null;
          for (let n = el; n && n !== doc.body; n = n.parentElement) {
            if (doc.defaultView.getComputedStyle(n).position === "fixed") fixed = n;
          }
          const target = fixed || el.closest("body > div") || el;
          target.style.setProperty("left", "0.75rem", "important");
          target.style.setProperty("right", "auto", "important");
        });
      };
      new MutationObserver(move).observe(doc.body, {subtree: true, childList: true});
      move();
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
# grows out over the page. Phones keep the popover picker and hidden sidebar.
st.html(
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
      section[data-testid="stSidebar"][aria-expanded="false"]:hover [data-testid="stSidebarContent"] ~ div {
        display: none !important;
      }
      section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"] {
        width: var(--rail-w);
        overflow-x: hidden;
        background: var(--ag-surface-page); /* opaque panel tone over the page */
        transition: width 180ms ease;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:hover [data-testid="stSidebarContent"] {
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
      section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarHeader"] {
        justify-content: center;
      }
      section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarLogo"] {
        height: 40px; width: auto;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stSidebarLogo"] {
        margin: 0 auto;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stSidebarCollapseButton"] {
        display: none;
      }

      /* Page-nav rows: same padding in rail and hover panel. The 1.6rem
         line-height matches the forced glyph size below — without it the
         13px labels ride the theme's 2.0 menu-item line box on hover and
         every row grows, shifting the column. */
      section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] {
        padding-top: 0.4rem; padding-bottom: 0.4rem;
        line-height: 1.6rem;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stSidebarNavLink"] {
        justify-content: center;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stSidebarNavLink"] > span + span {
        display: none;
      }

      /* Section headers (Cartera / Mercado / Cuenta): keep the FULL word in
         the rail instead of an ellipsised first letter. The 72px rail minus
         stSidebarContent's ~20px side paddings leaves ~32px, so the negative
         margins buy that padding back (~60px) and the font drops a notch to
         fit "PORTFOLIO". The chevron slot is removed in the rail but its
         1.25rem height is pinned on the header for both states, so the rows
         below never move on hover. */
      section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stNavSectionHeader"] {
        min-height: 1.25rem;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stNavSectionHeader"] {
        justify-content: center;
        margin-left: -14px; margin-right: -14px;
        padding-right: 0; gap: 0;
        font-size: var(--ag-fs-3xs); letter-spacing: 0.04em;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stNavSectionHeader"] > div {
        display: none;
      }
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stNavSectionHeader"] * {
        overflow: visible !important; text-overflow: clip !important;
      }
      /* Streamlit sets the icon size inline, so !important is needed to grow it.
         Keep the SAME size in every state (collapsed rail, hover panel, pinned
         open) so the glyph never resizes and shifts the row layout. The glyph
         sits inside TWO wrapper spans that emotion pins at the theme's 1rem
         icon size — grow them too, or the row's layout height stays 1rem and
         the row shrinks whenever the label is hidden (the rail state). */
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] [data-testid="stIconMaterial"],
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] > span:first-child,
      section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] > span:first-child span {
        font-size: var(--ag-icon-nav) !important;
        width: var(--ag-icon-nav) !important;
        height: var(--ag-icon-nav) !important;
      }

      /* Minimized rail carries only the app logo + nav glyphs. Hide the whole
         ticker picker (sign-in button, "Valores" title, search + sort row and
         the watchlist itself) — it slides back in with the panel on hover. */
      section[data-testid="stSidebar"][aria-expanded="false"]:not(:hover) [data-testid="stSidebarUserContent"] {
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

# Resolve the run's language (Profile pref > browser locale > English) before
# the nav is built and any page runs, so page titles and page bodies read one
# stable value. A Profile change lands on its rerun, which re-runs this first.
i18n.set_active_language()

# First load only: nudge anonymous visitors to sign in with a dismissible
# modal (skippable in one click). No-op when [auth] is unset or already logged
# in; runs after the language is resolved so its text is localized.
auth.maybe_prompt_login()

# Signed-in first load: nudge the user to set up their investor profile so the
# assistant tailors its analysis. Skippable; nags again next session until set
# (or filled from the Profile page). No-op for guests — mutually exclusive with
# the login modal, which only fires when logged out.
auth.maybe_prompt_profile()

ticker_page = st.Page(
    "app_pages/ticker.py",
    title=tr("nav.ticker"),
    icon=":material/query_stats:",
    url_path="ticker",
)

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
        tr("nav.section_portfolio"): [
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
        ],
        tr("nav.section_market"): [
            ticker_page,
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
        on_click=st.login,
        width="stretch",
    )

# Deep link: ?ticker=SYM selects that symbol (applied once per new URL value,
# so it doesn't fight the picker). Away from the Ticker page it also jumps
# there — keeps pre-refactor /?ticker= bookmarks and table links working.
_qp = (st.query_params.get("ticker") or "").strip().upper()
if _qp and st.session_state.get("_url_ticker") != _qp:
    st.session_state["picker_selected"] = _qp
    st.session_state["_url_ticker"] = _qp
    if page.url_path != ticker_page.url_path:
        st.switch_page(ticker_page)

# The ticker picker lives here, above page.run(), so every page carries it:
# sidebar searchbar + watchlist on desktop, popover on phones. Clicking any
# ticker row navigates to the Ticker page (the picker's on_click sets the
# flag); on the Ticker page itself the rerun just redraws the selection.
ticker_picker(key="nav")
_clicked = st.session_state.pop("picker_clicked", False)
if _clicked and page.url_path != ticker_page.url_path:
    st.switch_page(ticker_page)

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

# Assistant overlay: a top-right launcher icon + slide-in chat panel, reachable
# from every page and carrying the current view (page + focused ticker) as
# context. The panel is fully self-contained (provider choice, key entry, chat),
# so there is no separate Chat page in the nav. Signed-in only — it reads the
# account's real book. Rendered BEFORE page.run(): the launcher is position:
# fixed (DOM order irrelevant) and must survive pages that crash or st.stop()
# mid-run — an uncaught page exception used to eat the button entirely.
if auth.is_logged_in():
    chat_core.render_side_panel(page.title)

# Yahoo throttles Streamlit Cloud's shared egress IPs; when the fetch layer's
# backoff (stocks.data.fetch._retry) is exhausted the error would otherwise
# surface as Streamlit's opaque crash page. Degrade to a banner instead —
# st.cache_data never caches exceptions, so a rerun retries the failed fetches
# while every cached section keeps rendering.
try:
    page.run()
except (YFRateLimitError, URLError) as exc:
    # Backstop only: the fetching sections catch this pair themselves and
    # degrade in place (they must — a fragment rerun never re-enters this
    # file, see stocks.web.notices). What still lands here is a fetch in the
    # non-fragment page body; the toast explains the gap in what did render.
    notices.data_toast(exc)
