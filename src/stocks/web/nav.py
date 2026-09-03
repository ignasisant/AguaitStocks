"""The two chrome bars: the desktop top bar and the phone tab bar.

`render_topbar` is what every page calls first — breadcrumb, ticker search
panel and the account corner. `render_bottom_nav` is its mobile counterpart,
the DS spec's fixed 4-destination bar that stands in for the sidebar; the
drawer behind Streamlit's own header stays reachable and the bar deliberately
does not remove it.
"""

from __future__ import annotations

import html

import streamlit as st

from stocks.web import css as css_util
from stocks.web.ds import (
    is_mobile,
)
from stocks.web.i18n import t as tr
from stocks.web.logos import asset_logo, company_name, logo
from stocks.web.search import topbar_search_panel

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
    css_util.inject(
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
        /* background-COLOR, never the `background` shorthand: the shorthand
           resets background-image, and these rows carry their logo as one
           (see _render_ticker_rows). The hover rule ties the per-row logo rule
           on specificity, so a shorthand there wiped the hovered row's logo. */
        [class*="st-key-topbar_results"] button {
          justify-content: flex-start; text-align: left;
          border: 0; background-color: transparent; color: var(--ag-text-primary);
          padding: 0.3rem 0.5rem; font-size: var(--ag-fs-sm); min-height: 0;
        }
        [class*="st-key-topbar_results"] button:hover,
        [class*="st-key-topbar_results"] button:focus,
        [class*="st-key-topbar_results"] button:active {
          background-color: var(--ag-surface-hover); color: var(--ag-text-primary);
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
          background-color: var(--ag-purple-900); border-color: var(--ag-purple-800);
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
          /* Streamlit paints its own app logo (st.logo) inside the
             collapsed-sidebar control, which on phones lands right next to
             our mark and shows the bull twice. The DS header owns the mark
             here, so drop Streamlit's; the sidebar's own logo
             (stSidebarLogo) is a different testid and stays.
             The header copy's testid is `stHeaderLogo` (1.60 renamed it —
             `stLogo` is now only the img's CLASS, shared with the sidebar
             logo, so matching on that would blank the sidebar mark too). */
          [data-testid="stHeaderLogo"] { display: none; }
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
    topbar_search_panel()

