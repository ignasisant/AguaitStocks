"""Loading skeletons — the placeholder every fetching section paints first.

A spinner says "something is happening" and nothing more: the section it
covers is zero-height until the data lands, so the page collapses and then
jerks back open, and on a cold cache several spinners stack up with no hint of
what each one is building. Skeletons replace them. Each fetching section
reserves its slot up front and fills it with a gray shimmer shaped like what is
coming — the KPI row, the chart, the ticker table — so the layout holds its
geometry from first paint and the reader can read the page's structure while
the numbers are still in flight.

Usage wraps the fetch and the render that follows it:

    with skeletons.slot("metrics", n=4) as box:
        data = load()             # shimmer is on screen for this call
        with box.container():     # replaces the shimmer
            render(data)

`box.container()` swaps the skeleton for the real content; leaving the block
without calling it — an early `return`, a caught fetch failure, a raised
exception — clears the shimmer instead, so a slot never keeps shimmering over
a section that gave up.

Shapes are plain HTML because no native element draws a placeholder and a bare
`st.empty()` is an invisible gap. `CSS` is injected once by app.py; the colors
are the same DS tokens as the cards these sit inside (see widgets.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import streamlit as st

# Injected once per session by app.py, before any page body runs.
#
# NOTE: never write a left angle bracket anywhere inside this style block, not
# even in a comment — DOMPurify silently drops the WHOLE block when its text
# contains one (the same trap documented on app.py's base style block).
CSS = """
<style>
  /* Base sheen: two alphas of the neutral-600 token, so the trough and crest
     composite over whatever sits behind them (the neutral-900 .topstocks-card,
     or the page tone in a card-less section) instead of pinning two opaque
     greys that only look right on one of the two surfaces. */
  .topstocks-sk {
    --sk-base: var(--ag-skeleton-base);
    --sk-hi: var(--ag-skeleton-hi);
    width: 100%;
  }
  .topstocks-sk .skb {
    display: block;
    border-radius: var(--ag-radius-xs);
    background: linear-gradient(90deg,
      var(--sk-base) 25%, var(--sk-hi) 50%, var(--sk-base) 75%);
    background-size: 200% 100%;
    animation: topstocks-sk-sheen 1.5s linear infinite;
  }
  @keyframes topstocks-sk-sheen {
    from {background-position: 200% 0;}
    to {background-position: -200% 0;}
  }
  /* Vestibular safety: keep the shape, drop the sweep. */
  @media (prefers-reduced-motion: reduce) {
    .topstocks-sk .skb {animation: none; background: var(--sk-base);}
  }

  /* Stat tiles — the 12px label / Epilogue value / delta pill stack that
     st.metric renders (app.py styles the real thing to match). */
  .sk-metrics {display: flex; flex-direction: column; gap: 0.9rem;}
  .sk-mrow {display: flex; flex-wrap: wrap; gap: 0.6rem 1.5rem;}
  .sk-metric {flex: 1 1 96px; min-width: 84px; max-width: 240px;}
  .sk-metric .sk-lbl {height: 9px; width: 62%; margin-bottom: 10px;}
  .sk-metric .sk-val {height: 19px; width: 86%; margin-bottom: 10px;}
  .sk-metric .sk-dlt {height: 15px; width: 50%;
                      border-radius: var(--ag-radius-pill);}

  /* Charts: y ticks, plot body, x ticks — the frame stays put while Plotly
     builds, so the card does not resize under the reader. */
  .sk-chart {display: flex; flex-direction: column;}
  .sk-legend {display: flex; gap: 10px; margin-bottom: 10px; flex: none;}
  .sk-legend span {height: 9px; width: 46px;
                   border-radius: var(--ag-radius-pill);}
  .sk-frame {flex: 1 1 auto; display: flex; gap: 8px; min-height: 0;}
  .sk-yax {flex: none; width: 28px; display: flex; flex-direction: column;
           justify-content: space-between; padding: 2px 0;}
  .sk-yax span {height: 8px; width: 100%;}
  .sk-col {flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0;}
  .sk-plot {flex: 1 1 auto; position: relative; min-height: 0;}
  .sk-xax {flex: none; display: flex; justify-content: space-between;
           margin-top: 9px;}
  .sk-xax span {height: 8px; width: 36px;}
  /* Area silhouette: one shimmering block clipped to a plausible price path
     rather than a flat rectangle, so the slot reads as a chart at a glance. */
  .sk-area {
    position: absolute; inset: 0; border-radius: 0;
    clip-path: polygon(0% 76%, 7% 61%, 14% 69%, 21% 48%, 28% 57%, 35% 37%,
      42% 46%, 49% 28%, 56% 39%, 63% 23%, 70% 34%, 77% 17%, 84% 27%,
      92% 15%, 100% 21%, 100% 100%, 0% 100%);
  }
  .sk-bars {position: absolute; inset: 0; display: flex; align-items: flex-end;
            gap: 4%;}
  /* Bars are rounded on the top corners only — they rise off the axis. */
  .sk-bars span {flex: 1 1 auto;
                 border-radius: var(--ag-radius-xs) var(--ag-radius-xs) 0 0;}
  .sk-bars.sk-thin {gap: 2.4%;}
  .sk-pie {position: absolute; inset: 0; display: flex; align-items: center;
           justify-content: center;}
  .sk-donut {
    aspect-ratio: 1; height: 100%; max-width: 100%; border-radius: 50%;
    /* Mask alpha channel, not a palette color: in a mask, opaque black means
       "keep this pixel". Deliberately outside the token ramp. */
    -webkit-mask: radial-gradient(circle, transparent 45%, #000 46%);
    mask: radial-gradient(circle, transparent 45%, #000 46%);
  }
  .sk-heat {position: absolute; inset: 0; display: grid; gap: 4px;}
  .sk-heat span {border-radius: var(--ag-radius-xs);}

  /* Wide table — the desktop rendering of widgets.ticker_table_html: a
     logo + name cell on the left, numeric columns right-aligned. */
  .sk-table {display: flex; flex-direction: column;}
  .sk-trow {display: grid; gap: 12px; align-items: center; padding: 10px 0;
            border-bottom: 1px solid var(--ag-rule-soft);}
  .sk-trow span.skb {height: 11px; width: 62%; justify-self: end;}
  .sk-thead span.skb {height: 8px; width: 52%;}
  .sk-tick {display: flex; align-items: center; gap: 9px; min-width: 0;}
  .sk-tick .sk-logo {height: 22px; width: 22px; flex: none;
                     border-radius: var(--ag-radius-xs);}
  .sk-tick .sk-name {height: 11px; flex: 1 1 auto; max-width: 150px;}

  /* Dense rows — the phone rendering of the same table (.agr-row). */
  .sk-rows {display: flex; flex-direction: column;}
  .sk-row {display: flex; align-items: center; gap: 10px; padding: 8px 2px;
           border-bottom: 1px solid var(--ag-rule-soft);}
  .sk-row .sk-rlogo {height: 30px; width: 30px; flex: none;
                     border-radius: var(--ag-radius-xs);}
  .sk-rmain {flex: 1 1 auto; min-width: 0;}
  .sk-rside {flex: none; width: 30%; max-width: 120px;}
  .sk-rside span.skb {margin-left: auto;}
  .sk-l1 {height: 12px; width: 46%; margin-bottom: 6px;}
  .sk-l2 {height: 9px; width: 72%;}
  .sk-rside .sk-l1 {width: 78%;}
  .sk-rside .sk-l2 {width: 52%;}

  /* Stacked cards — the phone rendering of a table that isn't a ticker
     list (widgets.stacked_table_html's .ags-card). */
  .sk-cards {display: flex; flex-direction: column;}
  .sk-kcard {padding: 9px 2px; border-bottom: 1px solid var(--ag-rule-soft);}
  .sk-kcard .sk-ktitle {height: 12px; width: 38%; margin-bottom: 7px;}
  .sk-kv {display: flex; justify-content: space-between; gap: 12px;
          padding: 3px 0;}
  .sk-kv .sk-k {height: 9px; width: 32%;}
  .sk-kv .sk-v {height: 9px; width: 20%;}

  /* Month / week grid — the earnings calendars. */
  .sk-cal {display: grid; gap: 4px;}
  .sk-cell {border: 1px solid var(--ag-border);
            border-radius: var(--ag-radius-sm); padding: 6px;
            display: flex; flex-direction: column; gap: 6px;}
  .sk-cell .sk-day {height: 8px; width: 40%;}
  .sk-cell .sk-chip {height: 14px; width: 100%;
                     border-radius: var(--ag-radius-xs);}

  /* Prose: paragraphs, captions, chat answers. */
  .sk-text {display: flex; flex-direction: column; gap: 9px;}
  .sk-text span {height: 11px;}
  .sk-card .sk-ctitle {height: 14px; width: 38%; max-width: 240px;
                       margin-bottom: 14px;}

  /* Chip rows — segmented controls and pill groups that only exist once
     their options are known. */
  .sk-pills {display: flex; flex-wrap: wrap; gap: 4px;}
  .sk-pills span {height: 30px; width: 62px;
                  border-radius: var(--ag-radius-sm);}
</style>
"""

# Deterministic bar heights: a fixed cycle, not a random draw, so a rerun
# repaints the same silhouette instead of reshuffling it under the reader.
_BARS = (46, 68, 34, 82, 57, 91, 43, 74, 62, 87, 50, 70, 38, 79, 55, 66)
# Line widths for prose blocks — the last line always runs short, the way a
# real paragraph ends.
_LINES = (100, 94, 88, 97, 91)


def _blocks(n: int) -> str:
    """`n` bare shimmer blocks; the parent shape's CSS sizes them."""
    return '<span class="skb"></span>' * n


def _mobile() -> bool:
    # Imported lazily: widgets pulls in yfinance and the watchlist config, and
    # this module is otherwise dependency-free (and unit-testable as such).
    from stocks.web.widgets import is_mobile

    return is_mobile()


# ------------------------------------------------------------------- shapes


def _text(*, lines: int = 3, width: str = "100%") -> str:
    widths = [_LINES[i % len(_LINES)] for i in range(lines - 1)] + [58]
    body = "".join(
        f'<span class="skb" style="width:{w}%"></span>' for w in widths[:lines]
    )
    return f'<div class="topstocks-sk sk-text" style="max-width:{width}">{body}</div>'


def _metrics(*, n: int | tuple[int, ...] = 4, delta: bool = True) -> str:
    rows = (n,) if isinstance(n, int) else tuple(n)
    tile = (
        '<div class="sk-metric"><span class="skb sk-lbl"></span>'
        '<span class="skb sk-val"></span>'
        + ('<span class="skb sk-dlt"></span>' if delta else "")
        + "</div>"
    )
    body = "".join(f'<div class="sk-mrow">{tile * count}</div>' for count in rows)
    return f'<div class="topstocks-sk sk-metrics">{body}</div>'


def _plot(shape: str, *, bars: int, cells: int) -> str:
    if shape == "bars":
        heights = "".join(
            f'<span class="skb" style="height:{_BARS[i % len(_BARS)]}%"></span>'
            for i in range(bars)
        )
        thin = " sk-thin" if bars > 20 else ""
        return f'<div class="sk-bars{thin}">{heights}</div>'
    if shape == "pie":
        return '<div class="sk-pie"><div class="skb sk-donut"></div></div>'
    if shape == "heatmap":
        grid = (f"grid-template-columns:repeat({cells},1fr);"
                f"grid-template-rows:repeat({cells},1fr)")
        return f'<div class="sk-heat" style="{grid}">{_blocks(cells * cells)}</div>'
    return '<div class="skb sk-area"></div>'


def _chart(
    *,
    height: int = 260,
    shape: str = "area",
    legend: bool = False,
    bars: int = 14,
    cells: int = 8,
    axes: bool | None = None,
) -> str:
    """One chart-shaped slot, sized to the chart that will replace it.

    `height` must match the Plotly figure's own height so the card does not
    resize on the swap. `shape` picks the silhouette: area (line/area),
    bars (bar and candlestick charts), pie (donut allocations), heatmap
    (the correlation matrix).
    """
    # Axes belong to cartesian plots only — a donut or a matrix with tick
    # marks beside it reads as a different chart than the one arriving.
    if axes is None:
        axes = shape in ("area", "bars")
    head = f'<div class="sk-legend">{_blocks(2)}</div>' if legend else ""
    plot = f'<div class="sk-plot">{_plot(shape, bars=bars, cells=cells)}</div>'
    if axes:
        body = (
            f'<div class="sk-yax">{_blocks(3)}</div>'
            f'<div class="sk-col">{plot}'
            f'<div class="sk-xax">{_blocks(3)}</div></div>'
        )
    else:
        body = f'<div class="sk-col">{plot}</div>'
    return (
        f'<div class="topstocks-sk sk-chart" style="height:{height}px">'
        f'{head}<div class="sk-frame">{body}</div></div>'
    )


def _rows(*, rows: int = 5) -> str:
    row = (
        '<div class="sk-row"><span class="skb sk-rlogo"></span>'
        '<div class="sk-rmain"><span class="skb sk-l1"></span>'
        '<span class="skb sk-l2"></span></div>'
        '<div class="sk-rside"><span class="skb sk-l1"></span>'
        '<span class="skb sk-l2"></span></div></div>'
    )
    return f'<div class="topstocks-sk sk-rows">{row * rows}</div>'


def _table(*, rows: int = 5, cols: int = 4, header: bool = True) -> str:
    """Ticker-table slot, in whichever form the real table will take.

    widgets.ticker_table_html renders a wide grid on desktop and dense
    two-line rows on phones; the skeleton follows the same fork so the swap
    is a fill, not a relayout.
    """
    if _mobile():
        return _rows(rows=rows)
    grid = f"grid-template-columns:1.7fr {'1fr ' * max(cols - 1, 1)}"
    head = (
        f'<div class="sk-trow sk-thead" style="{grid}">{_blocks(cols)}</div>'
        if header
        else ""
    )
    body = (
        f'<div class="sk-trow" style="{grid}">'
        '<div class="sk-tick"><span class="skb sk-logo"></span>'
        '<span class="skb sk-name"></span></div>'
        f'{_blocks(max(cols - 1, 1))}</div>'
    )
    return f'<div class="topstocks-sk sk-table">{head}{body * rows}</div>'


def _cards(*, n: int = 3, lines: int = 4) -> str:
    """Stacked label/value cards — the phone form of a non-ticker table.

    Its desktop counterpart is `_table`; the two are picked by the caller
    (not forked in here) because the frames that stack are the ones with no
    ticker column, and only the page knows how many lines a card will hold.
    """
    kv = (
        '<div class="sk-kv"><span class="skb sk-k"></span>'
        '<span class="skb sk-v"></span></div>'
    ) * lines
    card = f'<div class="sk-kcard"><span class="skb sk-ktitle"></span>{kv}</div>'
    return f'<div class="topstocks-sk sk-cards">{card * n}</div>'


def _calendar(*, weeks: int = 4, cols: int = 5, cell: int = 96) -> str:
    grid = f"grid-template-columns:repeat({cols},1fr)"
    body = (
        f'<div class="sk-cell" style="height:{cell}px">'
        '<span class="skb sk-day"></span><span class="skb sk-chip"></span></div>'
    ) * (weeks * cols)
    return f'<div class="topstocks-sk sk-cal" style="{grid}">{body}</div>'


def _pills(*, n: int = 5) -> str:
    return f'<div class="topstocks-sk sk-pills">{_blocks(n)}</div>'


_SHAPES = {
    "text": _text,
    "metrics": _metrics,
    "chart": _chart,
    "table": _table,
    "rows": _rows,
    "cards": _cards,
    "calendar": _calendar,
    "pills": _pills,
}


def html(kind: str = "text", *, title: bool = False, **kw) -> str:
    """Markup for one skeleton, for callers that place it themselves.

    `title` prepends a heading bar, for the cards that open with an
    `st.subheader` / `st.markdown` line above the block being fetched.
    """
    try:
        build = _SHAPES[kind]
    except KeyError:
        raise ValueError(
            f"unknown skeleton {kind!r} (have: {', '.join(sorted(_SHAPES))})"
        ) from None
    body = build(**kw)
    if title:
        body = (
            '<div class="topstocks-sk sk-card">'
            f'<span class="skb sk-ctitle"></span>{body}</div>'
        )
    return body


# --------------------------------------------------------------------- slot


class _Slot:
    """A reserved page position holding a skeleton until content replaces it.

    Wraps the `st.empty()` the skeleton was drawn into: `container()` swaps in
    a real container for the loaded content, `clear()` drops the skeleton and
    leaves nothing. Both mark the slot resolved, which is what `slot()` checks
    before clearing an abandoned shimmer on the way out.
    """

    def __init__(self, placeholder) -> None:
        self._placeholder = placeholder
        self.resolved = False

    def container(self, **kw):
        """Replace the skeleton with a container for the real content."""
        self.resolved = True
        return self._placeholder.container(**kw)

    def clear(self) -> None:
        """Drop the skeleton, leaving the slot empty."""
        self.resolved = True
        self._placeholder.empty()


def reserve_html(markup: str, *, container=None, border: bool = False) -> _Slot:
    """Reserve a slot holding caller-supplied placeholder markup.

    Same contract as `reserve()` — the caller still owes the slot a
    `container()` or a `clear()` — for the few sections whose placeholder is
    not one of the shapes above: a card that names what it is building while
    it builds it (web/daily_ui.py) rather than shimmering anonymously.
    """
    box = _Slot((container or st).empty())
    target = box._placeholder.container(border=True) if border else box._placeholder
    target.html(markup)
    return box


def reserve(
    kind: str = "text", *, container=None, border: bool = False, **kw
) -> _Slot:
    """Draw a skeleton now and hand back its slot, to be filled later.

    For the deferred-slot pattern: a card that holds its place near the top of
    the page while its fetch runs at the bottom, so the cheap blocks in between
    paint first. The caller owns the outcome — every path out must end in
    `container()` or `clear()`, or the shimmer outlives the load. Prefer
    `slot()` whenever the fetch and the render sit together; it guarantees that
    for you.

    Args:
        container: parent to draw into (a column, a reserved container);
            defaults to wherever the call sits.
        border: wrap the shimmer in a bordered card, for slots whose content
            arrives as `st.container(border=True)` — the card outline is then
            there from first paint. Filling or clearing the slot replaces the
            whole card, borders included.
    """
    return reserve_html(html(kind, **kw), container=container, border=border)


@contextmanager
def slot(kind: str = "text", **kw) -> Iterator[_Slot]:
    """Reserve a slot, shimmer it, and yield it for the loaded content.

        with skeletons.slot("chart", height=380) as box:
            df = fetch()                  # shimmer is on screen
            with box.container():         # replaces it
                show_chart(build(df))

    Takes everything `reserve()` does. Leaving the block without calling
    `container()` clears the skeleton, so a fetch that raises or returns early
    never strands a shimmer on the page.
    """
    box = reserve(kind, **kw)
    try:
        yield box
    finally:
        if not box.resolved:
            box.clear()
