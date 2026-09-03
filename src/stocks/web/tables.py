"""HTML tables, KPI grids and the cell formatters they share.

Streamlit's own dataframe can't carry a delta chip inside a cell or sort on a
key that isn't the displayed string, so every dense list in the app (Positions,
Realized & tax, screener, earnings, import previews) is hand-built markup with
one stylesheet. Keeping that in one module is what keeps the tables identical:
the copies drifted the last time they lived apart.

`data_table` is the escape hatch back to `st.dataframe` for the cases that
don't need any of this.
"""

from __future__ import annotations

import html
from urllib.parse import quote

import pandas as pd
import streamlit as st

from stocks.web.ds import (
    BORDER,
    BRAND_ACCENT,
    DOWN_COLOR,
    FS_2XS,
    FS_BASE,
    FS_MD,
    FS_SM,
    FS_XL,
    FS_XS,
    LOSS_BAND,
    LOSS_COLOR,
    LOSS_COLOR_MUTED,
    PROFIT_BAND,
    PROFIT_COLOR,
    PROFIT_COLOR_MUTED,
    RADIUS_MD,
    RADIUS_PILL,
    RADIUS_XS,
    RULE_SOFT,
    SURFACE_HOVER,
    SURFACE_PAGE,
    SURFACE_SUNKEN,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    UP_COLOR,
    WARN_BAND,
    WARN_COLOR,
    is_mobile,
)
from stocks.web.logos import company_name, logo

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

