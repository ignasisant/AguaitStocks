"""Markup for the Pulse page's trend rows — level, direction, shape, one line each.

The page this serves started as levels in KPI tiles and percentile captions,
and the levels turned out to be the least useful half of it: a 10-year yield
at 4.79% is a fact, and "+33bp over three months while the curve went nowhere"
is the reading. So every row here carries four things in one line —

    label            what it is
    value            the level, in its own units
    chips            the change over each horizon, coloured by whether that
                     direction is the welcome one for THIS series
    sparkline        the shape the numbers can't show (smooth drift versus a
                     spike and a round trip land on the same 3-month delta)
    state            where it sits in its own trend (up / turning / down)

— and the same row renders a yield, an index, a volatility gauge, a commodity
and a country's inflation, so the reader learns one layout instead of five.

`st.dataframe` cannot do this: it renders no HTML, so a sparkline column would
have to become a base64 data URI in an ImageColumn, and the sign colouring
would be lost. These are read-only rows on a page nobody sorts, so plain HTML
is both simpler and better here — sorting is what the Screener is for.

One CSS block for the whole page, injected via `stocks.web.css` (never
`st.html` directly — DOMPurify drops a style block containing a "<", so no
comment in the sheet below may contain one either).
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass, field

from stocks.web.ds import (
    BORDER,
    CANDLE_DOWN,
    CANDLE_UP,
    FS_2XS,
    FS_SM,
    FS_XS,
    RADIUS_PILL,
    RADIUS_SM,
    SURFACE_SUNKEN,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARN_COLOR,
)
from stocks.web.spark import flat_rule, sparkline

# Trend-state pill colours. `turning_*` deliberately share the caution tint
# rather than getting a green and a red of their own: a turn is a turn, and
# painting "broke down but recovering" green would read as a buy signal from a
# page that does not give them.
STATE_TINT = {
    "up": CANDLE_UP,
    "turning_up": WARN_COLOR,
    "turning_down": WARN_COLOR,
    "down": CANDLE_DOWN,
    "unknown": TEXT_MUTED,
}


@dataclass(frozen=True)
class TrendRow:
    """One line of the trend table.

    `chips` are (text, direction) pairs already formatted by the caller — the
    units differ per block (basis points on a yield, percent on an index,
    percentage points on an inflation rate) and only the caller knows which.
    `direction` is +1 for a welcome move, -1 for an unwelcome one and 0 for
    neutral, NOT the sign of the number: a falling credit spread and a rising
    index are both good news and must both be green.
    """

    label: str
    value: str
    # Hover text for the label — what this series actually is. The rows
    # replaced KPI tiles that carried a help tooltip each, and a 10-year TIPS
    # yield is not self-explanatory from its name, so the explanation rides
    # here rather than being dropped.
    hint: str | None = None
    chips: Sequence[tuple[str, int]] = ()
    spark: Sequence[float] = ()
    state: str | None = None
    note: str | None = None
    # Draws the sparkline's zero line — for a series of changes rather than
    # levels, where "which side of zero" is the whole point.
    spark_baseline: float | None = None
    extras: Sequence[str] = field(default_factory=tuple)


def css(*, chip_labels: Sequence[str]) -> str:
    """The stylesheet for these rows, sized for `chip_labels` change columns.

    The column count is baked into the grid template because a CSS grid cannot
    read it from the markup, and every block on the page passes the same number
    so the columns line up down the whole page.
    """
    chips = " ".join(["4.6rem"] * len(chip_labels))
    return f"""
    .ag-trend {{ display: flex; flex-direction: column; }}
    .ag-trend-head, .ag-trend-row {{
      display: grid;
      grid-template-columns: minmax(7rem, 1.6fr) 5.4rem {chips} 5.6rem 6.4rem;
      align-items: center; gap: 0.4rem;
    }}
    .ag-trend-head {{
      font-size: {FS_2XS}; color: {TEXT_MUTED}; text-transform: uppercase;
      letter-spacing: 0.05em; padding: 0 0 0.3rem 0;
      border-bottom: 1px solid {BORDER};
    }}
    .ag-trend-row {{ padding: 0.34rem 0; border-bottom: 1px solid {SURFACE_SUNKEN}; }}
    .ag-trend-row:last-child {{ border-bottom: 0; }}
    .ag-trend-l {{ font-size: {FS_SM}; color: {TEXT_SECONDARY};
                   overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; }}
    .ag-trend-v {{ font-size: {FS_SM}; color: {TEXT_PRIMARY}; text-align: right;
                   font-variant-numeric: tabular-nums; }}
    .ag-trend-c {{ font-size: {FS_XS}; text-align: right;
                   font-variant-numeric: tabular-nums; }}
    .ag-trend-s {{ display: flex; justify-content: flex-end; }}
    .ag-spark {{ display: block; }}
    .ag-trend-st {{ font-size: {FS_2XS}; font-weight: 600; text-align: center;
                    padding: 0.1rem 0.3rem; border-radius: {RADIUS_PILL};
                    white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; }}
    .ag-trend-note {{ grid-column: 1 / -1; font-size: {FS_2XS};
                      color: {TEXT_MUTED}; padding: 0 0 0.1rem 0; }}
    .ag-quad {{ border: 1px solid {BORDER}; border-radius: {RADIUS_SM};
                padding: 0.55rem 0.7rem; display: flex; flex-direction: column;
                gap: 0.25rem; }}
    .ag-quad-l {{ font-size: {FS_XS}; color: {TEXT_MUTED}; font-weight: 600;
                  letter-spacing: 0.04em; }}
    .ag-quad-v {{ font-size: {FS_SM}; font-weight: 700; color: {TEXT_PRIMARY}; }}
    .ag-quad-n {{ font-size: {FS_2XS}; color: {TEXT_MUTED}; }}
    .ag-quads {{ display: grid; gap: 0.5rem;
                 grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); }}
    @media (max-width: 640px) {{
      /* Eight columns will not fit a 390px screen, so the row becomes two
         lines: name and level, then the changes with the sparkline and the
         state pill under them. */
      .ag-trend-head {{ display: none; }}
      .ag-trend-row {{
        grid-template-columns: repeat({max(len(chip_labels), 2)}, 1fr) auto;
        gap: 0.3rem 0.4rem; padding: 0.5rem 0;
      }}
      .ag-trend-l {{ grid-column: 1 / -2; font-size: {FS_XS}; }}
      .ag-trend-v {{ grid-column: -2 / -1; text-align: right; }}
      .ag-trend-st {{ grid-column: -2 / -1; }}
    }}
    """


def _chip(text: str, direction: int) -> str:
    color = (
        CANDLE_UP if direction > 0 else CANDLE_DOWN if direction < 0 else TEXT_MUTED
    )
    return f'<span class="ag-trend-c" style="color:{color}">{html.escape(text)}</span>'


def rows_html(
    rows: Sequence[TrendRow],
    *,
    chip_labels: Sequence[str],
    value_label: str,
    label_label: str,
    spark_label: str,
    state_label: str,
    state_names: dict[str, str],
) -> str:
    """The rows as one self-contained HTML block, header included.

    `state_names` maps a `trend_state` key to its translated pill text; a row
    with `state=None` gets an empty cell, which is what the blocks that have no
    meaningful trend label (an inflation print) pass.
    """
    head = (
        f'<div class="ag-trend-head">'
        f'<span class="ag-trend-l">{html.escape(label_label)}</span>'
        f'<span class="ag-trend-v">{html.escape(value_label)}</span>'
        + "".join(
            f'<span class="ag-trend-c">{html.escape(c)}</span>' for c in chip_labels
        )
        + f'<span class="ag-trend-c">{html.escape(spark_label)}</span>'
        f'<span class="ag-trend-st">{html.escape(state_label)}</span>'
        "</div>"
    )
    body = []
    for row in rows:
        tip = row.hint or row.label
        cells = [
            f'<span class="ag-trend-l" title="{html.escape(tip, quote=True)}">'
            f"{html.escape(row.label)}</span>",
            f'<span class="ag-trend-v">{html.escape(row.value)}</span>',
        ]
        cells += [_chip(text, direction) for text, direction in row.chips]
        # Pad short chip lists so the sparkline and state columns stay aligned
        # when one series is too young for the longest horizon.
        cells += ['<span class="ag-trend-c"></span>'] * (
            len(chip_labels) - len(row.chips)
        )
        line = sparkline(row.spark, baseline=row.spark_baseline)
        cells.append(f'<span class="ag-trend-s">{line or flat_rule()}</span>')
        if row.state:
            tint = STATE_TINT.get(row.state, TEXT_MUTED)
            cells.append(
                f'<span class="ag-trend-st" style="background:{tint}22;'
                f'color:{tint}">{html.escape(state_names.get(row.state, ""))}</span>'
            )
        else:
            cells.append('<span class="ag-trend-st"></span>')
        cells += list(row.extras)
        if row.note:
            cells.append(
                f'<span class="ag-trend-note">{html.escape(row.note)}</span>'
            )
        body.append(f'<div class="ag-trend-row">{"".join(cells)}</div>')
    return f'<div class="ag-trend">{head}{"".join(body)}</div>'


def quad_html(cards: Sequence[tuple[str, str, str]]) -> str:
    """A row of small (label, value, note) cards — the derived trend readings.

    Trend breadth, the stock/bond correlation and the rates quadrant are each
    one sentence's worth of number, and none of them fits the row layout above:
    they have no level, no horizons and no shape of their own.
    """
    tiles = [
        '<div class="ag-quad">'
        f'<span class="ag-quad-l">{html.escape(label)}</span>'
        f'<span class="ag-quad-v">{html.escape(value)}</span>'
        f'<span class="ag-quad-n">{html.escape(note)}</span>'
        "</div>"
        for label, value, note in cards
    ]
    return f'<div class="ag-quads">{"".join(tiles)}</div>'
