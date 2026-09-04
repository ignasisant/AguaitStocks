"""The y-axis refit on the portfolio history chart.

Drag-zooming narrows x only; Plotly keeps the y range the full span set, so a
small early window would draw flat on the floor of a big axis. The refit is
client-side (Streamlit sees no relayout), computed from the band extents the
page ships in a hidden slot. Both halves are covered end to end:
`yfit_slot.render` builds the markup, and its data attributes are fed to the
module's own JS under node with a DOM shim.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from stocks.web import yfit_slot

PAGE = Path("src/stocks/web/app_pages/portfolio.py")

# The page's JS wires document-level listeners, reads the graph div's layout
# and calls Plotly.relayout, so the shim fakes all three: a slot carrying the
# extents, a graph div carrying the zoomed range, a Plotly that records the
# relayouts (and applies them, so the "same window" guard sees them), and the
# mouseup that ends a drag.
SHIM = """
const slot = {dataset: __DATA__};
const listeners = {};
const gd = {data: [{}, {meta: 'history'}], layout: __LAYOUT__};
const calls = [];
globalThis.window = globalThis;
globalThis.Plotly = {
  relayout: (g, patch) => {
    calls.push(patch);
    g.layout.yaxis = patch['yaxis.autorange']
      ? {autorange: true}
      : {range: patch['yaxis.range']};
  },
};
globalThis.document = {
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
  querySelector: (s) => (s === '.ts-yfit' ? slot : null),
  querySelectorAll: (s) => (s === '.js-plotly-plot' ? [gd] : []),
};
__SCRIPT__
__EVENTS__
setTimeout(() => console.log(JSON.stringify(calls)), 900);
"""

# Two years of book: a small 2022 leg, then a big 2024 one. The whole-span y
# axis is set by the second; a window over the first must not inherit it.
FRAME = pd.DataFrame(
    {
        "injected": [1000.0, 4476.0, 4476.0, 90000.0, 90000.0],
        "value": [980.0, 3414.0, 5200.0, 84000.0, 120000.0],
    },
    index=pd.to_datetime(
        ["2022-05-02", "2022-12-05", "2023-09-01", "2024-06-03", "2024-12-02"]
    ),
)


def _markup(frame: pd.DataFrame) -> str:
    captured = {}

    class Box:
        def html(self, body, **kwargs):
            captured["body"] = body
            captured["kwargs"] = kwargs

    yfit_slot.render(Box(), frame)
    assert captured["kwargs"] == {"unsafe_allow_javascript": True}
    return captured["body"]


def _dataset(markup: str) -> dict:
    """The slot's data-* attributes, as the browser would see them."""
    div = markup[: markup.index("</div>")]
    return dict(re.findall(r'data-([a-z]+)="([^"]*)"', div))


def _run(
    tmp_path: Path,
    layout: dict,
    *,
    events: str = "listeners.mouseup({});",
    frame: pd.DataFrame = FRAME,
) -> list[dict]:
    harness = tmp_path / "harness.js"
    harness.write_text(
        SHIM.replace("__DATA__", json.dumps(_dataset(_markup(frame))))
        .replace("__LAYOUT__", json.dumps(layout))
        .replace("__EVENTS__", events)
        .replace(
            "__SCRIPT__",
            yfit_slot.YFIT_JS.replace("<script>", "").replace("</script>", ""),
        )
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _xaxis(lo: str, hi: str) -> dict:
    return {"xaxis": {"range": [lo, hi]}, "yaxis": {"range": [0, 127000]}}


# ------------------------------------------------------------ the emitted slot


def test_the_slot_ships_the_band_extents_and_stays_hidden():
    markup = _markup(FRAME)
    data = _dataset(markup)
    assert "display:none" in markup
    assert data["x"].split(",")[1] == "2022-12-05"
    # Per day: the lower and upper edge of the injected/value band.
    assert data["lo"].split(",")[1] == "3414.00"
    assert data["hi"].split(",")[1] == "4476.00"


def test_the_hover_trace_is_tagged_for_the_bridge():
    """The JS finds the history chart by its meta="history" trace, and the
    slot is emitted next to it on desktop."""
    src = PAGE.read_text()
    assert 'meta="history", customdata=customdata,' in src
    assert "yfit_slot.render(st, hist)" in src


# --------------------------------------------------------- the browser reading

pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the chart JS"
)


@pytestmark_node
def test_a_zoomed_window_refits_the_axis_to_what_it_holds(tmp_path):
    """2022 alone tops out at 4,476 — not at the 120k the full span reaches."""
    calls = _run(tmp_path, _xaxis("2022-04-01 00:00:00", "2023-01-31 00:00:00"))
    assert len(calls) == 1
    lo, hi = calls[0]["yaxis.range"]
    # 980 .. 5,200 (the point past the right edge, whose segment is drawn)
    # plus 6% padding — three orders of magnitude under the full-span 120k.
    assert hi == pytest.approx(5200 + 0.06 * (5200 - 980))
    assert lo == pytest.approx(980 - 0.06 * (5200 - 980))


@pytestmark_node
def test_the_floor_never_pads_below_zero(tmp_path):
    """A window whose low sits near zero pads down to zero, not past it."""
    frame = pd.DataFrame(
        {"injected": [0.0, 100.0], "value": [0.0, 90.0]},
        index=pd.to_datetime(["2022-05-02", "2022-05-03"]),
    )
    calls = _run(
        tmp_path,
        _xaxis("2022-05-01 00:00:00", "2022-05-04 00:00:00"),
        frame=frame,
    )
    assert calls[0]["yaxis.range"][0] == 0


@pytestmark_node
def test_the_segments_crossing_each_edge_count(tmp_path):
    """The window holds one point, but the lines run to both borders: the
    neighbours just outside are on screen, so they set the extents."""
    calls = _run(tmp_path, _xaxis("2023-06-01 00:00:00", "2023-12-01 00:00:00"))
    lo, hi = calls[0]["yaxis.range"]
    assert hi > 90000  # the 2024-06 neighbour, not the 5,200 point in view


@pytestmark_node
def test_resetting_the_zoom_hands_the_axis_back_to_plotly(tmp_path):
    calls = _run(tmp_path, {"xaxis": {"autorange": True, "range": [0, 1]}})
    assert calls == [{"yaxis.autorange": True}]


@pytestmark_node
def test_an_axis_already_autoranged_is_left_alone(tmp_path):
    """Every other click on the page reaches the listener; none may relayout."""
    calls = _run(
        tmp_path,
        {"xaxis": {"autorange": True}, "yaxis": {"autorange": True}},
    )
    assert calls == []


@pytestmark_node
def test_a_second_click_in_the_same_window_does_not_relayout(tmp_path):
    calls = _run(
        tmp_path,
        _xaxis("2022-04-01 00:00:00", "2023-01-31 00:00:00"),
        events="listeners.mouseup({}); setTimeout(() => listeners.mouseup({}), 300);",
    )
    assert len(calls) == 1


@pytestmark_node
def test_zooming_back_into_a_window_after_a_reset_refits_again(tmp_path):
    """The reset clears the guard: the same window must be re-applied."""
    calls = _run(
        tmp_path,
        _xaxis("2022-04-01 00:00:00", "2023-01-31 00:00:00"),
        events=(
            "listeners.mouseup({});"
            "setTimeout(() => { gd.layout.xaxis = {autorange: true};"
            " listeners.dblclick({}); }, 200);"
            "setTimeout(() => { gd.layout.xaxis ="
            " {range: ['2022-04-01 00:00:00', '2023-01-31 00:00:00']};"
            " listeners.mouseup({}); }, 500);"
        ),
    )
    assert [list(c)[0] for c in calls] == [
        "yaxis.range", "yaxis.autorange", "yaxis.range",
    ]
