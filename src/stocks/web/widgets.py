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
from stocks.web import auth
from stocks.web.i18n import t as tr

# One hover-label look for every chart — the DS card, echoed onto Plotly's SVG
# tooltip: neutral-900 surface (#28262D, same as .aguait-card), neutral-800
# border (#3B3942), Instrument Sans body face, neutral-50 text. namelength=-1 so
# trace names never truncate to 15 chars. Radius + elevation (which Plotly's
# hoverlabel can't set) come from CSS in app.py. Font size drops on mobile (see
# show_chart) so a multi-row unified box fits a ~390px phone screen.
HOVER_FONT_DESKTOP = 15
HOVER_FONT_MOBILE = 11
# Axis tick labels shrink too: at Plotly's default 12px the y-axis gutter eats
# ~40px of a ~390px screen; 10px narrows the gutter and lightens the frame.
TICK_FONT_MOBILE = 10
HOVERLABEL = dict(
    bgcolor="rgba(40,38,45,0.97)",  # neutral-900 — matches .aguait-card surface
    bordercolor="#3B3942",          # neutral-800 — DS border
    font=dict(
        family="'Instrument Sans', sans-serif",
        size=HOVER_FONT_DESKTOP,
        color="#F9F9FA",            # neutral-50 — primary text
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
    # .aguait-card neutral-900, #28262D) instead of Streamlit's opaque
    # page-background paper — otherwise the plot reads as a darker box inset
    # in the card. Card-less contexts inherit the page bg, still correct.
    fig.update_layout(
        hoverlabel=hoverlabel,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        modebar={"bgcolor": "rgba(0,0,0,0)"},
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
        layout["legend"] = dict(orientation="h", yanchor="bottom", y=1.0, x=0)
    layout["margin"] = dict(l=0, r=0, t=top, b=0)
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
    """Same-origin URL for a bundled image from web/assets/ (e.g. the Aguait
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


# Aguait palette — single source of truth for every chart/HTML color the theme
# (config.toml) can't reach. Amphora DS tokens; market up/down are reserved for
# price change only (success/critical fills — the light variants read on dark).
UP_COLOR = "#DBFFD2"    # alza — market gain (DS success fill)
DOWN_COLOR = "#FFD2CB"  # baja — market loss (DS critical fill)
WARN_COLOR = "#F4C600"  # aviso — caution (DS caution highlight)
INFO_COLOR = "#7290F0"  # info — chart lines, informational (DS blue 500)
BRAND_ACCENT = "#A98EF7"  # brand purple 500 — accents, active iconography
BRAND_CTA = "#7F3FE8"   # brand purple 600 — CTAs
TEXT_MUTED = "#827F8C"  # secondary text / chart axes (DS neutral 500)
# Back-compat aliases — every green/red profit-loss cue routes through these.
PROFIT_COLOR, LOSS_COLOR = UP_COLOR, DOWN_COLOR
# Price-chart series hues (the "Aguait Valor" design): candle bodies and the
# moving-average overlays carry their own softer tones so the UP/DOWN fills
# above stay reserved for text and badges.
CANDLE_UP = "#7ED28C"    # bullish candles
CANDLE_DOWN = "#F0897E"  # bearish candles
SMA_FAST = "#F2A33C"     # SMA20 overlay — orange
SMA_SLOW = "#6E8FF0"     # SMA50 overlay + results markers — blue
EVENT_LINE = "#5A5766"   # dashed corporate-event verticals
# Dimmed profit/loss for off-session day changes: same hue at ~45% so the cell
# reads as "market closed — last completed session's move", not a live tick.
PROFIT_COLOR_MUTED = "rgba(219,255,210,0.45)"  # UP_COLOR @ 45%
LOSS_COLOR_MUTED = "rgba(255,210,203,0.45)"    # DOWN_COLOR @ 45%

# One look for every HTML-rendered ticker table (Positions, Realized & tax,
# earnings lists, screener, import previews) — keep them identical.
_TABLE_STYLES = [
    {"selector": "", "props": [
        ("width", "100%"), ("border-collapse", "collapse"),
        ("font-size", "0.9rem"),
    ]},
    {"selector": "th", "props": [
        ("text-align", "left"), ("padding", "8px 12px"),
        ("border-bottom", "1px solid #3B3942"),
        ("font-weight", "500"), ("font-size", "0.82rem"),
        ("color", "#827F8C"),
    ]},
    {"selector": "td", "props": [
        ("padding", "7px 12px"), ("white-space", "nowrap"),
        ("border-bottom", "1px solid rgba(59,57,66,.5)"),
    ]},
    {"selector": "td a:hover b", "props": [
        ("text-decoration", "underline"),
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
        'border-radius:4px;vertical-align:-6px;margin-right:8px">'
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
# fought. Declared once at import (never inside a function — re-registering the
# name misbehaves). Styled in its own shadow root to match the old field.
_LIVE_SEARCH = st.components.v2.component(
    "aguait_live_search",
    html='<input id="q" class="lsi" type="text" autocomplete="off" spellcheck="false" />',
    css="""
    .lsi {
      width: 100%; box-sizing: border-box; height: 36px; padding: 0 0.75rem;
      background: #28262D; color: #F9F9FA; border: 1px solid #3B3942;
      border-radius: 8px; font-size: 0.85rem; outline: none;
    }
    .lsi::placeholder { color: #827F8C; }
    .lsi:focus { border-color: #7F3FE8; }
    """,
    js="""
export default function (component) {
  const { parentElement, data, setStateValue } = component
  const input = parentElement.querySelector("#q")
  if (!input) return
  input.placeholder = (data && data.placeholder) || ""
  const nextValue = (data && data.value) ?? ""
  // Only overwrite the field when the user isn't typing in it — a render whose
  // run started before the last keystroke echoes the stale value and would
  // wipe the in-progress query.
  if (input.value !== nextValue && !input.matches(":focus")) input.value = nextValue
  if (input.value === nextValue) {
    // Python echoed exactly what the field shows: the keystroke landed, so
    // stop re-asserting it.
    clearTimeout(input._retry)
    input._retryN = 0
  }
  if (data && data.blur) {
    // A row click navigated. Clearing only the DOM input is not enough: the
    // frontend widget manager re-sends its stored "value" with every rerun,
    // so the old query would resurrect the dropdown. Sync the clear into
    // widget state and drop any pending debounce/retry still holding it.
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    input.value = ""
    setStateValue("value", "")
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
    // 12 × 800ms outlasts the slowest throttled-Yahoo page run.
    const send = (v) => {
      setStateValue("value", v)
      clearTimeout(input._retry)
      if ((input._retryN = (input._retryN || 0) + 1) > 12) return
      input._retry = setTimeout(() => {
        setStateValue("nonce", (input._nonce = (input._nonce || 0) + 1))
        send(input.value)
      }, 800)
    }
    input.addEventListener("input", (e) => {
      clearTimeout(input._timer)
      input._retryN = 0
      const v = e.target.value
      input._timer = setTimeout(() => send(v), 160)
    })
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { clearTimeout(input._timer); input._retryN = 0; send(e.target.value) }
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
    const results = doc.querySelector(".st-key-topbar_results")
    if (!results || !results.contains(e.target)) return
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    clearTimeout(input._blurTimer)
    results.style.display = "none"
    input.value = ""
    setStateValue("value", "")
    input.blur()
  }
  doc.addEventListener("click", doc.__lsRowCloser)
}
""",
)


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
    result = _LIVE_SEARCH(
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
    plus an "Analyze <SYMBOL>" fallback for a symbol none of them know. Returns
    `(watch, crypto, sec, analyze)` where watch rows carry their ⭐/💼 mark.
    """
    q = raw.strip().upper()
    if not q:
        return [], [], [], None
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
            mark = "⭐" if t in fav_set else ("💼" if t in held_set else "")
            watch.append((t, labels[t], mark))
    if not watch:
        # Typo fallback ("oracel"): fuzzy over the same fields, best first.
        # Only when exact substring found nothing, so it never dilutes results.
        for t in _fuzzy_order(q, order, labels, tag_map):
            mark = "⭐" if t in fav_set else ("💼" if t in held_set else "")
            watch.append((t, labels[t], mark))

    from stocks.data.crypto import search_crypto

    crypto = [(t, n) for t, n in search_crypto(q) if t not in labels]
    sec = [(t, n) for t, n in sec_matches(q) if t not in labels]
    known = set(labels) | {t for t, _ in crypto} | {t for t, _ in sec}
    analyze = q if (q not in known and re.fullmatch(r"[A-Z0-9.\-]{1,12}", q)) else None
    return watch[:8], crypto[:4], sec[:6], analyze


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
        mark = "⭐" if t in fav_set else ("💼" if t in held_set else "")
        rows.append((t, name if name != t else "", mark))
    return rows


def _go_ticker(ticker: str) -> None:
    """Navigate to a ticker from the top-bar dropdown.

    Reuses the picker's contract — set the shared selection and raise
    "picker_clicked" so app.py switches to the Ticker page on the rerun — then
    clear the query so the dropdown closes.
    """
    st.session_state.pop("topbar_q", None)  # reset the live input (its state is a dict)
    st.session_state["topbar_q_blur"] = True  # blur the field so recents don't re-open
    auth.push_recent_search(ticker)  # remember it for the empty-field dropdown
    st.session_state["picker_selected"] = ticker.strip().upper()
    st.session_state["picker_clicked"] = True


def _search_row(t: str, label: str, key: str) -> None:
    """One dropdown row: a full-width button that navigates to ticker `t`."""
    st.button(label, key=key, on_click=_go_ticker, args=(t,), width="stretch")


def _render_ticker_rows(rows: list[tuple[str, str, str]], *, key_prefix: str = "tbres") -> None:
    """Render `(symbol, name, mark)` rows as logo'd buttons in the dropdown.

    Each carries its watchlist logo (CSS background, like the picker) and its
    ⭐/💼 mark. Logo rules are scoped under .st-key-topbar_results so they beat
    the base row rule's specificity — otherwise its `background: transparent`
    shorthand wipes the logo back to none.
    """
    logo_rules = [
        f".st-key-topbar_results .st-key-{key_prefix}_{_slug(t)} button {{"
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
        if q:
            # Typed query: live matches. Recents never show here — searching
            # something else replaces them (they only stand in for an empty field).
            watch, crypto, sec, analyze = _topbar_matches(q)
            if watch or crypto or sec or analyze:
                with st.container(key="topbar_results"):
                    _render_ticker_rows(watch)
                    if crypto:
                        st.caption(tr("widgets.crypto"))
                        for t, name in crypto:
                            _search_row(t, f"🪙 **{t}**  {name}", f"tbrescx_{_slug(t)}")
                    if sec:
                        st.caption(tr("widgets.from_sec_search"))
                        for t, name in sec:
                            _search_row(t, f"🔎 **{t}**  {name}", f"tbressec_{_slug(t)}")
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
            with st.container(key="topbar_results"):
                st.caption(tr("widgets.recent"))
                _render_ticker_rows(recent, key_prefix="tbrec")


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
        '<span class="tb-brand">Aguait</span>',
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
    #     full-height main block), singled out with `:has(.aguait-topbar)`;
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
        [data-testid="stElementContainer"]:has(.aguait-topbar) {
          position: sticky !important; top: 0; z-index: 100000;
          /* -1.2rem swallows the block-container top padding; the extra
             0.55rem swallows the vertical-block gap the hidden st.html
             style/script elements above the bar still contribute. */
          margin: calc(-1.2rem - 0.55rem) -2.5rem 0.4rem;
          width: calc(100% + 5rem) !important;
          max-width: calc(100% + 5rem) !important;
        }
        /* 64px tall like the design header (14px padding + 36px controls). */
        .aguait-topbar {
          padding: 0 2.5rem; min-height: 64px;
          display: flex; align-items: center; gap: 0.5rem;
          background: rgba(24,22,28,0.92); backdrop-filter: blur(7px);
          border-bottom: 1px solid #3B3942;
          font-size: 0.92rem; line-height: 1.2;
          white-space: nowrap; overflow: hidden;
        }
        .aguait-topbar .tb-brand { color: #827F8C; font-weight: 400; }
        .aguait-topbar .tb-sep { color: #696673; }
        .aguait-topbar .tb-page { color: #F9F9FA; font-weight: 600; }
        .aguait-topbar .tb-ticker {
          color: #F9F9FA; font-weight: 600;
          display: inline-flex; align-items: center; gap: 6px;
          min-width: 0; overflow: hidden; text-overflow: ellipsis;
        }
        .aguait-topbar .tb-name { color: #827F8C; font-weight: 400; }
        .aguait-topbar .tb-logo {
          height: 18px; width: 18px; object-fit: contain; border-radius: 4px;
        }

        /* Fixed global search: top strip, right side, clearing the chat FAB so
           menu + search + chat read as one row. Shifts left when the FAB is
           present (signed-in); sits at the edge otherwise. */
        /* Phones: centered in the native header row. Desktop overrides the
           top below to center in the 4rem breadcrumb bar. */
        .st-key-topbar_search {
          position: fixed; top: 0.45rem; right: 1rem; z-index: 999999;
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
        .st-key-topbar_results {
          background: #28262D; border: 1px solid #3B3942; border-radius: 8px;
          padding: 4px; max-height: 60vh; overflow-y: auto;
          box-shadow: 0 10px 28px rgba(0,0,0,0.5);
        }
        .st-key-topbar_results [data-testid="stVerticalBlock"] { gap: 0.1rem; }
        .st-key-topbar_results button {
          justify-content: flex-start; text-align: left;
          border: 0; background: transparent; color: #F9F9FA;
          padding: 0.3rem 0.5rem; font-size: 0.85rem; min-height: 0;
        }
        .st-key-topbar_results button:hover {
          background: #3B3942; color: #F9F9FA;
        }
        /* The button's inner flex wrapper centers its label; pin it left so the
           text sits right after the logo/emoji instead of mid-row. */
        .st-key-topbar_results button > div { justify-content: flex-start; }
        /* Streamlit centers button labels; force them left so the text sits
           flush after the logo/emoji instead of floating mid-row. */
        .st-key-topbar_results button [data-testid="stMarkdownContainer"] {
          width: 100%; text-align: left;
        }
        .st-key-topbar_results button p {
          font-weight: 400; text-align: left;
        }
        .st-key-topbar_results button strong { color: #C6B7FB; }
        /* Section captions ("crypto" / "SEC search") — small, dim, tight. */
        .st-key-topbar_results [data-testid="stCaptionContainer"] {
          padding: 0.25rem 0.5rem 0.1rem; margin: 0;
        }
        .st-key-topbar_results [data-testid="stCaptionContainer"] p {
          font-size: 0.7rem; color: #827F8C; margin: 0;
        }
        /* Analyze-new fallback keeps the brand primary fill to read as an action. */
        .st-key-topbar_results button[kind="primary"] {
          background: #301263; border-color: #4E2092; color: #DED7FD;
        }

        /* Desktop: the search overlays the bar's right, so reserve room and keep
           long breadcrumbs from sliding under it. Phones put the search up in
           the header row instead, so the bar keeps its full width there. */
        @media (min-width: 641px) {
          /* Room for the 300px search + 36px launcher riding the bar's right. */
          .aguait-topbar { padding-right: 26rem; }
        }
        @media (max-width: 640px) {
          [data-testid="stElementContainer"]:has(.aguait-topbar) {
            margin-left: -0.75rem; margin-right: -0.75rem;
            width: calc(100% + 1.5rem) !important;
            max-width: calc(100% + 1.5rem) !important;
          }
          .aguait-topbar { padding-left: 0.75rem; padding-right: 0.75rem; }
        }
        </style>
        """
    )
    # Breadcrumb strip: desktop only. Phones carry the native header + page
    # heading, so a third bar there just clutters the top — but the search below
    # still renders, so the menu toggle + search + chat button share the header
    # row on phones.
    if not is_mobile():
        st.html(f'<div class="aguait-topbar">{"".join(crumbs)}</div>')
    # Live search + dropdown, in a fragment so typing reruns only the panel.
    _topbar_search_panel()


# Revolut-style dense rows for phones: shared look for every mobile ticker
# list. One style block per list is idempotent — several lists per page fine.
_ROWS_CSS = f"""<style>
.agr-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 8px 2px;
  border-bottom: 1px solid rgba(59,57,66,.5);
  text-decoration: none; color: inherit;
}}
.agr-logo {{
  width: 30px; height: 30px; object-fit: contain;
  border-radius: 6px; flex: none; display: inline-block;
}}
.agr-main {{ flex: 1 1 auto; min-width: 0; }}
.agr-side {{ flex: none; text-align: right; max-width: 45%; }}
.agr-l1 {{ font-size: .95rem; font-weight: 600; line-height: 1.4; }}
.agr-l2 {{
  font-size: .8rem; color: {TEXT_MUTED}; line-height: 1.4;
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

        [logo]  TICKER                     €6,345
                Company · 9% · P/L +54%     +1.2%

    `spec` maps columns onto the row slots (see ticker_table_html's `mobile`
    arg); fmt/signed/muted keep the exact semantics of the table renderer, so
    a phone row and its desktop cell always print the same string and color.
    """
    value_col = spec.get("value")
    delta_col = spec.get("delta")
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
        left = f'<div class="agr-l1">{html.escape(tick)}</div>'
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
        mobile: when set and the request comes from a phone, render dense
            Revolut-style rows (no horizontal panning) instead of a table.
            Maps columns onto row slots: {"value": col (line-1 right),
            "delta": col (line-2 right, signed-colored), "sub": (cols,) for
            the dim line under the ticker, "sub_labels": {col: prefix},
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
    right = [c for c in frame.columns if c != ticker_col and c not in left_cols]
    fmt_map = {k: v for k, v in (fmt or {}).items() if k in frame.columns}
    # Signed columns drop the "+" on an exact 0 so market-closed rows read
    # "0.0%"/"€0" (neutral), matching signed_color's grey.
    for c in signed:
        if c in fmt_map:
            fmt_map[c] = _neutral_zero_formatter(fmt_map[c])
    sty = frame.style.format(fmt_map or None, na_rep="n/a")
    if colored := [c for c in signed if c in frame.columns]:
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
                    signed_color(v, muted=m) for v, m in zip(col, muted_mask)
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
    return f'<div style="overflow-x:auto">{sty.to_html()}</div>'


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
    # every open position is reachable; 💼 marks them in the row.
    _db = str(auth.db_path())
    held_set = set(held_tickers(_db, db_mtime(_db)))
    for t in sorted(held_set - set(labels)):
        labels[t] = sec_title(t) or t
    # Favorites float to the top of the list; ⭐ marks them in the row.
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

    # The picker renders before page.run(), outside the app-level rate-limit
    # guard — a throttled Yahoo or dead network must dim the change chips, not
    # crash the app (same exception pair the app-level guard catches). The miss
    # isn't cached (st.cache_data skips exceptions), so a rerun retries.
    try:
        changes = daily_changes(tuple(tickers)) if show_changes else {}
    except (YFRateLimitError, URLError):
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
            key=lambda t: (changes.get(t) if changes.get(t) is not None else float("inf")),
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
        " {background-color:#301263 !important; border-color:#4E2092 !important;"
        " color:#DED7FD !important;}"
        f"{_sel(primary + ' *')}"
        " {color:#DED7FD !important;}",
    ]
    for t in shown:
        src = logo(t)
        if src:
            bg = f'background-image:url("{src}");'
            rules.append(f".st-key-{_btn_key(t)} button {{{bg}}}")
    box.html("<style>" + "".join(rules) + "</style>")

    # Beyond the watchlist: the query also searches the whole SEC ticker map
    # (symbol or company name), so typing "airbnb" surfaces ABNB even when it
    # isn't listed or held. A raw "Analyze <SYMBOL>" fallback stays for exact
    # symbols the map doesn't know (non-US listings like ASML.AS).
    matches: list[tuple[str, str]] = []
    crypto_hits: list[tuple[str, str]] = []
    if allow_custom and q:
        matches = [(t, n) for t, n in sec_matches(q) if t not in labels]
        # Coin codes and names too, so "bitcoin" or "btc" offers BTC-USD.
        from stocks.data.crypto import search_crypto

        crypto_hits = [(t, n) for t, n in search_crypto(q) if t not in labels]
        known = set(labels) | {t for t, _ in matches} | {t for t, _ in crypto_hits}
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
    with box.container(height=list_height):
        if not shown and not q:
            st.caption(tr("widgets.no_tickers"))
        for t in shown:
            star = "⭐ " if t in fav_set else ("💼 " if t in held_set else "")
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
        # SEC-map hits below the watchlist rows. No logo background — resolving
        # a logo hits the network per uncached ticker, too costly per keystroke;
        # 🔎 marks these as searched, not listed. Picking one selects it like
        # any row (and the favorite star can then pin it to the watchlist).
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
                on_click=st.login,
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
