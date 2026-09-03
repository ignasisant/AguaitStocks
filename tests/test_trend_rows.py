"""Sparklines and trend rows — the markup half of the Pulse page.

These render inside `st.html`, which sanitises with DOMPurify, so the tests
here pin both the geometry (a sparkline that misreads its own scale draws a
plausible-looking wrong shape, which no exception would catch) and the fact
that the output stays inside the tag set that survives sanitising.
"""

import re

import pytest

from stocks.web import trend_ui
from stocks.web.spark import HEIGHT, PAD, WIDTH, flat_rule, sparkline
from stocks.web.trend_ui import TrendRow

# Everything DOMPurify keeps by default and these modules are allowed to emit.
# A `foreignObject`, a `use` or a gradient `defs` would be dropped silently and
# the tile would just look empty, so the allowlist is asserted rather than
# assumed.
ALLOWED_TAGS = {"svg", "polyline", "line", "div", "span"}


def _tags(markup: str) -> set[str]:
    return set(re.findall(r"<([a-zA-Z]+)", markup))


def _points(markup: str) -> list[tuple[float, float]]:
    raw = re.search(r'points="([^"]+)"', markup).group(1)
    return [tuple(float(n) for n in pair.split(",")) for pair in raw.split()]


# ------------------------------------------------------------------ sparkline
def test_sparkline_spans_its_box_and_orders_oldest_first():
    points = _points(sparkline([1.0, 2.0, 3.0]))
    xs = [x for x, _ in points]
    assert xs == sorted(xs)
    assert xs[0] == pytest.approx(PAD)
    assert xs[-1] == pytest.approx(WIDTH - PAD)


def test_sparkline_puts_high_values_at_the_top():
    """SVG's y axis grows downward, so this is an easy sign error to ship."""
    (_, y_low), _, (_, y_high) = _points(sparkline([1.0, 2.0, 3.0]))
    assert y_high < y_low
    assert y_high == pytest.approx(PAD)
    assert y_low == pytest.approx(HEIGHT - PAD)


def test_sparkline_scales_to_its_own_range():
    """Each line fills its own box: these are shapes, not comparable heights."""
    small = _points(sparkline([100.0, 101.0]))
    large = _points(sparkline([100.0, 200.0]))
    assert [y for _, y in small] == [y for _, y in large]


def test_a_flat_series_draws_down_the_middle():
    """min == max would divide by zero."""
    points = _points(sparkline([5.0, 5.0, 5.0]))
    assert all(y == pytest.approx(HEIGHT / 2) for _, y in points)


def test_sparkline_colours_by_the_whole_window_not_the_last_tick():
    """The line's own direction is the reading; a final uptick is noise."""
    from stocks.web.ds import CANDLE_DOWN, CANDLE_UP

    assert CANDLE_DOWN in sparkline([10.0, 5.0, 4.0, 4.5])
    assert CANDLE_UP in sparkline([4.0, 9.0, 10.0, 9.5])


def test_sparkline_of_one_point_draws_nothing():
    """A dot would imply a trend the caller does not have."""
    assert sparkline([1.0]) == ""
    assert sparkline([]) == ""


def test_sparkline_drops_nan_before_measuring():
    assert _points(sparkline([1.0, float("nan"), 3.0])) == _points(
        sparkline([1.0, 3.0])
    )


def test_baseline_is_drawn_only_when_it_falls_inside_the_range():
    assert "<line" in sparkline([-1.0, 0.5, 1.0], baseline=0.0)
    assert "<line" not in sparkline([1.0, 2.0, 3.0], baseline=0.0)


def test_sparkline_and_flat_rule_stay_inside_the_sanitiser_allowlist():
    assert _tags(sparkline([1.0, 2.0, 3.0], baseline=1.5)) <= ALLOWED_TAGS
    assert _tags(flat_rule()) <= ALLOWED_TAGS


def test_flat_rule_holds_the_column_width():
    """One unreadable row must not shift every sparkline beside it."""
    assert f'width="{WIDTH}"' in flat_rule()


# ------------------------------------------------------------------ trend rows
def _row(**kw) -> str:
    defaults = dict(
        chip_labels=["1w", "1m"],
        label_label="Name",
        value_label="Level",
        spark_label="90d",
        state_label="Trend",
        state_names={"up": "Uptrend", "down": "Downtrend"},
    )
    return trend_ui.rows_html(**{**defaults, **kw})


def test_short_chip_lists_are_padded_so_the_columns_stay_aligned():
    """A series too young for the longest horizon must not shift the row.

    The grid has one column per chip label; a row supplying fewer would slide
    its sparkline and state pill left into the change columns.
    """
    labels = ["1w", "1m", "3m", "12m"]
    markup = _row(
        chip_labels=labels,
        rows=[TrendRow(label="X", value="1", chips=[("+1%", 1)])],
    )
    row = markup.split('class="ag-trend-row"')[1]
    assert row.count('class="ag-trend-c"') == len(labels)


def test_chip_direction_colours_by_welcome_not_by_sign():
    """A tightening credit spread and a rising index are both good news."""
    from stocks.web.ds import CANDLE_DOWN, CANDLE_UP

    good = _row(rows=[TrendRow(label="X", value="1", chips=[("-13bp", 1)])])
    bad = _row(rows=[TrendRow(label="X", value="1", chips=[("-13bp", -1)])])
    assert CANDLE_UP in good and CANDLE_DOWN not in good
    assert CANDLE_DOWN in bad and CANDLE_UP not in bad


def test_a_neutral_chip_is_muted():
    from stocks.web.ds import TEXT_MUTED

    assert TEXT_MUTED in _row(
        rows=[TrendRow(label="X", value="1", chips=[("2.4%", 0)])]
    )


def test_the_hint_becomes_the_label_tooltip():
    """The rows replaced KPI tiles that each carried a help tooltip."""
    markup = _row(
        rows=[TrendRow(label="US 10y real", value="2.44%", hint="TIPS yield")]
    )
    assert 'title="TIPS yield"' in markup


def test_the_label_is_its_own_tooltip_without_a_hint():
    assert 'title="US 10y"' in _row(rows=[TrendRow(label="US 10y", value="4.79%")])


def test_a_row_without_a_state_still_fills_the_state_cell():
    markup = _row(rows=[TrendRow(label="Spain", value="4.5%")])
    assert markup.count('class="ag-trend-st"') == 2  # header plus the empty cell


def test_a_row_with_no_sparkline_gets_the_placeholder_rule():
    markup = _row(rows=[TrendRow(label="X", value="1", spark=[1.0])])
    assert 'stroke-dasharray="2 3"' in markup
    assert "<polyline" not in markup


def test_labels_and_values_are_escaped():
    markup = _row(rows=[TrendRow(label="S&P 500", value="1")])
    assert "S&amp;P 500" in markup
    assert "S&P 500" not in markup


def test_rows_html_stays_inside_the_sanitiser_allowlist():
    markup = _row(
        rows=[
            TrendRow(
                label="X", value="1", chips=[("+1%", 1)], spark=[1.0, 2.0, 3.0],
                state="up", note="stale",
            )
        ]
    )
    assert _tags(markup) <= ALLOWED_TAGS


def test_css_declares_one_column_per_chip_label():
    """The grid template cannot count the markup, so the count is passed in."""
    sheet = trend_ui.css(chip_labels=["a", "b", "c"])
    template = re.search(r"grid-template-columns:([^;]+);", sheet).group(1)
    assert template.count("4.6rem") == 3


def test_the_stylesheet_carries_no_angle_bracket():
    """DOMPurify drops an entire style block containing one, silently."""
    assert "<" not in trend_ui.css(chip_labels=["a", "b", "c", "d"])


def test_quad_html_escapes_and_renders_one_card_per_entry():
    markup = trend_ui.quad_html(
        [("Breadth", "12/13", "above their 200-session average"), ("A&B", "1", "n")]
    )
    assert markup.count('class="ag-quad"') == 2
    assert "A&amp;B" in markup
    assert _tags(markup) <= ALLOWED_TAGS
