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
from urllib.parse import quote

import pandas as pd
import streamlit as st

from stocks.config import load_watchlist
from stocks.data.logo import logo_url
from stocks.web import auth

# One hover-label look for every chart: bigger type than Plotly's ~13px
# default, dark slate box with light text (readable on both Streamlit themes
# and calmer than the per-trace colored boxes), and namelength=-1 so trace
# names are never truncated to 15 chars.
HOVERLABEL = dict(
    bgcolor="rgba(15,23,42,0.95)",
    bordercolor="rgba(148,163,184,0.5)",
    font=dict(size=15, color="#e2e8f0"),
    namelength=-1,
    align="left",
)

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
    fig.update_layout(hoverlabel=HOVERLABEL)
    if is_mobile():
        fig.update_xaxes(fixedrange=True)
        fig.update_yaxes(fixedrange=True)
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
    top = 8
    layout: dict = {"height": height}
    if title:
        top += 34
        layout["title"] = dict(
            text=title, x=0, xanchor="left", y=1, yanchor="top", pad=dict(t=8)
        )
    if top_legend:
        top += 26
        layout["legend"] = dict(orientation="h", yanchor="bottom", y=1.0, x=0)
    layout["margin"] = dict(l=0, r=0, t=top, b=0)
    return layout


@st.cache_data(ttl=86400, show_spinner=False)
def logo(ticker: str) -> str | None:
    """Company logo URL for a ticker (cached a day — logos rarely change)."""
    return logo_url(ticker)


@st.cache_data(ttl=86400, show_spinner=False)
def company_name(ticker: str) -> str | None:
    """Human name: curated watchlist name first, then the coin map for crypto
    pairs, then the SEC ticker map (offline once cached). None for symbols no
    source knows."""
    for h in load_watchlist():
        if h.ticker.upper() == ticker.upper() and h.name:
            return h.name
    from stocks.data.crypto import crypto_name

    if name := crypto_name(ticker):
        return name
    from stocks.data.edgar import title_for

    return title_for(ticker)


# Semantic P/L colors — every green/red profit-loss cue in the app uses these.
PROFIT_COLOR, LOSS_COLOR = "#09ab3b", "#ff4b4b"

# One look for every HTML-rendered ticker table (Positions, Realized & tax,
# earnings lists, screener, import previews) — keep them identical.
_TABLE_STYLES = [
    {"selector": "", "props": [
        ("width", "100%"), ("border-collapse", "collapse"),
        ("font-size", "0.9rem"),
    ]},
    {"selector": "th", "props": [
        ("text-align", "left"), ("padding", "8px 12px"),
        ("border-bottom", "1px solid rgba(128,128,128,.35)"),
        ("font-weight", "600"), ("opacity", ".7"),
    ]},
    {"selector": "td", "props": [
        ("padding", "7px 12px"), ("white-space", "nowrap"),
        ("border-bottom", "1px solid rgba(128,128,128,.12)"),
    ]},
    {"selector": "td a:hover b", "props": [
        ("text-decoration", "underline"),
    ]},
]


def signed_color(v) -> str:
    """CSS for a signed number: profit green when ≥ 0, loss red below,
    nothing for NaN/non-numbers (Styler .map callback)."""
    try:
        if pd.isna(v):
            return ""
        return f"color: {PROFIT_COLOR}" if float(v) >= 0 else f"color: {LOSS_COLOR}"
    except (TypeError, ValueError):
        return ""


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
    """
    frame = frame.copy()
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
    sty = frame.style.format(
        {k: v for k, v in (fmt or {}).items() if k in frame.columns} or None,
        na_rep="n/a",
    )
    if colored := [c for c in signed if c in frame.columns]:
        sty = sty.map(signed_color, subset=colored)
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
    title: str = "Tickers",
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
        title: bold label above the searchbar.
        allow_custom: show an "Analyze <SYMBOL>" button when the query is a
            symbol not on the watchlist, so any ticker can be analyzed.
        show_changes: render the per-ticker daily-change chip (one bulk
            download, cached and shared across pages).
        list_height: pixel height of the scroll region holding the buttons.

    Returns the selected ticker (upper-cased, stripped) or None when the
    watchlist is empty and nothing has been typed.
    """
    mobile = is_mobile()
    if container is not None:
        box = container
    elif mobile:
        # Phones: the sidebar starts collapsed, so a sidebar-only picker is
        # unreachable without hunting for the toggle. Render as a popover in
        # the main area instead — the button shows the current selection.
        current = st.session_state.get("picker_selected")
        box = st.popover(f":material/search: {current or title}", width="stretch")
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
        labels[t] = t
    # Favorites float to the top of the list; ⭐ marks them in the row.
    tickers = [t for t in labels if t in fav_set]
    tickers += [t for t in labels if t not in fav_set]

    # Shared across pickers (not keyed by `key`) so the picked ticker follows
    # you between pages.
    sel_key = "picker_selected"
    if tickers:
        st.session_state.setdefault(sel_key, tickers[0])

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
    sort_opts = ["Watchlist", "A–Z"]
    if show_changes:
        sort_opts += ["Change ▲", "Change ▼"]
    if weights:
        sort_opts.append("Portfolio %")
    sc1, sc2 = box.columns([5, 1], vertical_alignment="bottom")
    query = sc1.text_input(
        "Search",
        key=f"{key}_search",
        placeholder="Search ticker or name…",
        label_visibility="collapsed",
    ).strip()
    # The sort choice mirrors into a shared session key: a page switch drops
    # the radio's widget state (and each picker has its own widget key), so the
    # radio is re-seeded from "picker_sort_by" here and writes back on change.
    stored_sort = st.session_state.get("picker_sort_by")
    if stored_sort not in sort_opts:
        stored_sort = sort_opts[0]

    def _set_sort() -> None:
        st.session_state["picker_sort_by"] = st.session_state[f"{key}_sort"]

    with sc2.popover(":material/sort:", help="Sort the list", width="stretch"):
        sort_by = st.radio(
            "Sort by",
            sort_opts,
            index=sort_opts.index(stored_sort),
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

    changes = daily_changes(tuple(tickers)) if show_changes else {}

    # Apply the sort chosen in the popover. "Watchlist" keeps the favorites-first
    # source order; the rest reorder `shown`. Missing changes sink to the bottom.
    if sort_by == "A–Z":
        shown = sorted(shown)
    elif sort_by in ("Change ▲", "Change ▼"):
        desc = sort_by == "Change ▼"
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
    elif sort_by == "Portfolio %":
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
        # Selected pick highlighted white with black text (overrides the red
        # Streamlit primary fill). The `*` rule beats the inline color on the
        # :green/:red change chip so the whole label reads black on white.
        f"{_sel(primary)},"
        f"{_sel(primary + ':hover')},"
        f"{_sel(primary + ':active')},"
        f"{_sel(primary + ':focus')} "
        " {background-color:#fff !important; border-color:#fff !important;"
        " color:#000 !important;}"
        f"{_sel(primary + ' *')}"
        " {color:#000 !important;}",
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
                f"Analyze **{q}**",
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
            st.caption("No tickers.")
        for t in shown:
            star = "⭐ " if t in fav_set else ("💼 " if t in held_set else "")
            chip = f"  {_change_md(changes.get(t))}" if show_changes else ""
            if sort_by == "Portfolio %":
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
            st.caption("Crypto")
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
            st.caption("From SEC ticker search")
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
                "Sign in to favorite & tag",
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

    def _toggle_fav() -> None:
        now = auth.toggle_favorite(ticker)
        st.toast(
            f"{ticker} {'added to' if now else 'removed from'} favorites",
            icon=":material/star:",
        )

    c1, c2 = box.columns([1, 4], vertical_alignment="center")
    c1.button(
        ":material/star:",
        key=f"{key}_fav_{_slug(ticker)}",
        on_click=_toggle_fav,
        help="Remove from favorites" if fav else "Add to favorites",
        # Primary fill marks the favorited state (label is the same star).
        type="primary" if fav else "secondary",
        width="stretch",
    )

    ms_key = f"{key}_tags_{_slug(ticker)}"

    def _save_tags() -> None:
        auth.set_tags(ticker, st.session_state[ms_key])

    with c2.popover(":material/label: Tags", width="stretch"):
        st.multiselect(
            "Tag groups",
            options=auth.all_tags(),
            default=tags,
            key=ms_key,
            accept_new_options=True,
            on_change=_save_tags,
            placeholder="Pick or type a new tag…",
            help="Free-form groups, saved to your watchlist as you edit. "
            "Search the ticker list by tag to filter a group.",
        )

    if tags:
        box.markdown(" ".join(f":gray-badge[{t}]" for t in tags))
