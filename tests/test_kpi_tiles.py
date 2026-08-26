"""Fundamentals KPI tiles: each verdict must stay bound to its own number.

Two Streamlit layouts failed at this before the grid. A bare caption under
st.metric printed directly above the NEXT tile's label ("expensive" read as a
header for the following KPI); wrapping each KPI in a bordered container did
not help either, because Streamlit under-sizes those fixed-width flex boxes
and the caption escaped below its own tile's edge. The block is now one HTML
grid with the verdict chip on the value's line — these tests pin that shape.
"""

import re
from pathlib import Path

from stocks.analysis.fundamentals import verdict
from stocks.web.widgets import (
    DOWN_COLOR,
    LOSS_BAND,
    PROFIT_BAND,
    SURFACE_SUNKEN,
    UP_COLOR,
    WARN_BAND,
    WARN_COLOR,
    kpi_grid_html,
)

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"
TICKER = (WEB / "app_pages" / "ticker.py").read_text()
APP = (WEB / "app.py").read_text()

TILES = [
    ("P/E (TTM)", "32.3x", ("expensive", "red"), "Price over trailing EPS"),
    ("ROIC", "72.6%", ("strong", "green"), None),
    ("EV/EBITDA", "16.2x", ("fair", "orange"), "Enterprise value over EBITDA"),
    ("Dilución (CAGR)", "0.1%", ("flat", "gray"), None),
    ("Market cap", "5.12T USD", None, None),
]


def _tile_bodies(html: str) -> list[str]:
    return re.findall(r'<div class="ag-kpi">(.*?)</div></div>', html, re.S)


def test_verdict_rides_the_value_line_inside_its_own_tile():
    tiles = _tile_bodies(kpi_grid_html(TILES))
    assert len(tiles) == len(TILES)
    row = re.search(r'<div class="ag-kpi-r">(.*)', tiles[0], re.S).group(1)
    assert "32.3x" in row and "expensive" in row  # same row, same tile
    # The next tile's label can never be mistaken for this tile's verdict.
    assert "ROIC" not in tiles[0]


def test_every_band_color_maps_to_a_ds_token():
    html = kpi_grid_html(TILES)
    for band, ink in (
        (LOSS_BAND, DOWN_COLOR), (PROFIT_BAND, UP_COLOR),
        (WARN_BAND, WARN_COLOR), (SURFACE_SUNKEN, "#827F8C"),
    ):
        assert f"background:{band};color:{ink}" in html


def test_bandless_kpi_gets_no_chip():
    cap = _tile_bodies(kpi_grid_html(TILES))[-1]
    assert "5.12T USD" in cap and "ag-kpi-c" not in cap


def test_tooltip_survives_as_a_native_title():
    """No Streamlit help popover inside raw HTML, so the KPI definition rides
    a title attribute — losing it would strip the block's only explanation."""
    html = kpi_grid_html(TILES)
    assert 'title="Price over trailing EPS"' in html
    markup = html.split("</style>", 1)[1]
    assert markup.count("ag-kpi-q") == 2  # only the two tiles that have a tip


def test_markup_is_escaped_and_self_contained():
    html = kpi_grid_html([("P/E <b>", "1<2", ("cheap", "green"), 'a "quote" <i>')])
    assert "<b>" not in html and "1<2" not in html and "<i>" not in html
    assert html.startswith("<style>")  # ships its own CSS, no app.py dependency


def test_verdict_tuples_feed_straight_in():
    """kpi_grid_html takes analysis.fundamentals.verdict output verbatim; a
    reshaped tuple would silently drop every chip."""
    v = verdict("pe_ttm", 32.3)
    assert v == ("expensive", "red")
    assert "expensive" in kpi_grid_html([("P/E", "32.3x", v, None)])


def test_page_renders_the_grid_and_drops_the_metric_row():
    body = TICKER.split("with _fund_card.container(border=True):", 1)[1]
    body = body.split("# ---", 1)[0]
    assert "st.html(kpi_grid_html([" in body
    assert "metric_cells(" not in body      # no wrapping metric row left
    assert "st.caption(verdict" not in body  # no stray caption under a tile
    assert "st-key-kpi_" not in APP          # …and its CSS crutch is gone
