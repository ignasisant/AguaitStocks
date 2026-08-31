"""Earnings-result breakdown: the visuals and the labels behind them.

The dialog's five sections (revenue, margins, GAAP, EPS, next-quarter
consensus) are built as self-contained HTML with inline styles, so these tests
pin the two things that silently go wrong: bar/marker geometry computed from
the wrong denominator, and a section shipping a label that only exists in one
catalog.
"""

import json
import re
from pathlib import Path

from stocks.web.earnings_ui import (
    _bars_html,
    _bps_delta,
    _meters_html,
    _money,
    _pct,
    _range_html,
    _signed_pct,
    _tone,
)
from stocks.web.widgets import BRAND_ACCENT, CRITICAL_FILL, INFO_DEEP, SUCCESS_FILL

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"


def _widths(html: str) -> list[float]:
    return [float(w) for w in re.findall(r"width:([\d.]+)%", html)]


def test_money_compacts_and_prefixes_the_currency():
    assert _money(81_615_000_000, "USD") == "$81.61B"
    assert _money(1_243_000_000_000, "EUR") == "€1.24T"
    assert _money(9_400_000, "SEK") == "SEK 9.40M"  # no symbol -> the code
    assert _money(24_391_000_000) == "24.39B"       # share counts: no prefix
    assert _money(None) == "—"


def test_percent_formatters_keep_the_sign_where_it_matters():
    assert _signed_pct(0.8523) == "+85.2%"
    assert _signed_pct(-0.061) == "-6.1%"
    assert _pct(0.7493) == "74.9%"   # a margin is a level, not a move
    assert _signed_pct(None) == "—" and _pct(None) == "—"


def test_tone_maps_direction_to_a_verdict_color():
    assert _tone(0.1) == "green" and _tone(0.0) == "green"
    assert _tone(-0.1) == "red"
    assert _tone(None) == "gray"


def test_bps_delta_is_the_margin_move_in_basis_points():
    assert round(_bps_delta(0.7493, 0.6053), 0) == 1440
    assert _bps_delta(0.7493, None) is None


def test_revenue_bars_scale_against_the_largest_quarter():
    rows = [
        ("Apr 26", 81615e6, "$81.62B", BRAND_ACCENT),
        ("Jan 26", 68127e6, "$68.13B", INFO_DEEP),
        ("Apr 25", 44062e6, "$44.06B", INFO_DEEP),
    ]
    html = _bars_html(rows)
    widths = _widths(html)
    assert widths[0] == 100.0                    # the top bar sets the scale
    assert round(widths[1], 1) == 83.5
    assert round(widths[2], 1) == 54.0
    # The quarter being reported on is the accented bar; the rest are the trend.
    assert html.count(f"background:{BRAND_ACCENT}") == 1
    assert html.count(f"background:{INFO_DEEP}") == 2


def test_bars_survive_a_missing_value_and_an_all_empty_set():
    html = _bars_html(
        [("Apr 26", None, "—", INFO_DEEP), ("Jan 26", 10.0, "10", INFO_DEEP)]
    )
    assert _widths(html) == [0.0, 100.0]
    assert _widths(_bars_html([("Apr 26", None, "—", INFO_DEEP)])) == [0.0]


def test_surprise_bars_color_by_sign():
    html = _bars_html(
        [
            ("Feb 26", 5.3, "+5.3%", SUCCESS_FILL),
            ("Aug 25", -6.1, "-6.1%", CRITICAL_FILL),
        ]
    )
    # A miss is scaled on its magnitude, not signed into a negative width.
    assert _widths(html) == [86.9, 100.0]
    assert f"background:{CRITICAL_FILL}" in html


def test_margin_gauges_are_a_share_of_one_hundred_percent():
    html = _meters_html(
        [
            ("Gross margin", 0.7493, "+1440 bps YoY", "green"),
            ("Operating margin", 0.656, "+1500 bps YoY", "green"),
            ("Net margin", None, "", "gray"),
        ]
    )
    assert _widths(html) == [74.9, 65.6, 0.0]  # not rescaled against each other
    assert "74.9%" in html and "—" in html
    assert "+1440 bps YoY" in html


def test_margin_gauge_clamps_a_figure_above_full_revenue():
    # A one-off gain can push net income past revenue; the track cannot exceed
    # its own width.
    assert _widths(_meters_html([("Net margin", 1.4, "", "gray")])) == [100.0]


def test_consensus_range_marks_the_mean_inside_the_band():
    html = _range_html("Revenue consensus", 100.0, 125.0, 200.0, str, note="26 analysts")
    assert _widths(html) == [25.0]  # the spacer before the marker
    assert "26 analysts" in html


def test_consensus_range_centers_the_marker_without_a_band():
    assert _widths(_range_html("EPS consensus", None, 2.65, None, str)) == [50.0]
    assert _widths(_range_html("EPS consensus", 2.6, 2.6, 2.6, str)) == [50.0]


def test_every_breakdown_label_exists_in_both_catalogs():
    src = (WEB / "earnings_ui.py").read_text()
    keys = set(re.findall(r'tr\(\s*"(earnings\.[a-z0-9_]+)"', src))
    keys |= set(re.findall(r'"(earnings\.period_[01][qy])"', src))
    assert len(keys) > 40, "the breakdown lost its labels"
    for lang in ("en", "es"):
        catalog = json.loads(
            (WEB / "locales" / lang / "earnings.json").read_text(encoding="utf-8")
        )
        assert keys <= set(catalog), (lang, sorted(keys - set(catalog)))
