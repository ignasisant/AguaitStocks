"""The zoom-range change readout on the ticker price chart.

Drag-zooming the chart is a client-side Plotly relayout Streamlit never sees,
so the change over the picked window is computed in the browser from the
series the page ships in the readout slot's data attributes. Both halves are
covered end to end: `range_readout.render` builds the markup, and its data
attributes are fed to the module's own JS under node with a DOM shim.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from stocks.web import range_readout
from stocks.web.i18n import translate

PAGE = Path("src/stocks/web/app_pages/ticker.py")
TMPL = "{pct} from {start} to {end}"

# The page's JS wires document-level listeners and reads the graph div's
# layout, so the shim fakes both: a readout slot carrying the series, a graph
# div carrying the zoomed range, and the mouseup that ends a drag.
SHIM = """
const slot = {dataset: __DATA__, style: {}, textContent: ''};
const listeners = {};
const gd = {data: [{meta: 'price'}, {name: 'SMA20'}], layout: __LAYOUT__};
globalThis.window = globalThis;
globalThis.document = {
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
  querySelector: (s) => (s === '.ts-range-change' ? slot : null),
  querySelectorAll: (s) => (s === '.js-plotly-plot' ? [gd] : []),
};
__SCRIPT__
listeners.mouseup({});
setTimeout(() => console.log(JSON.stringify({
  text: slot.textContent, display: slot.style.display, color: slot.style.color,
})), 300);
"""


FRAME = pd.DataFrame(
    {"Close": [300.0, 330.0, 340.0, 360.0, 345.0]},
    index=pd.to_datetime(
        [
            "2026-06-26",
            "2026-06-29",
            "2026-06-30",
            "2026-07-01",
            "2026-07-02",
        ]
    ),
)


@pytest.fixture(autouse=True)
def _stub_chrome(monkeypatch):
    """Fixed colours and a placeholder-preserving template.

    The assertions below are about the slot's wiring — which series, which
    attributes — not about today's design tokens or catalog wording, so the
    module's own imports of both are pinned here.
    """
    monkeypatch.setattr(range_readout, "FS_XS", "12px")
    monkeypatch.setattr(range_readout, "UP_COLOR", "#0f0")
    monkeypatch.setattr(range_readout, "DOWN_COLOR", "#f00")
    monkeypatch.setattr(range_readout, "active_language", lambda: "en")
    monkeypatch.setattr(range_readout, "tr", lambda key, **kw: TMPL.format(**kw))


def _markup(frame: pd.DataFrame) -> str:
    captured = {}

    class Box:
        def html(self, body, **kwargs):
            captured["body"] = body
            captured["kwargs"] = kwargs

    range_readout.render(Box(), frame)
    assert captured["kwargs"] == {"unsafe_allow_javascript": True}
    return captured["body"]


def _dataset(markup: str) -> dict:
    """The slot's data-* attributes, as the browser would see them."""
    div = markup[: markup.index("</div>")]
    return {
        k: html_mod.unescape(v)
        for k, v in re.findall(r'data-([a-z]+)="([^"]*)"', div)
    }


def _script() -> str:
    return range_readout.RANGE_JS.replace("<script>", "").replace("</script>", "")


def _run(tmp_path: Path, layout: dict, frame: pd.DataFrame = FRAME) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(
        SHIM.replace("__DATA__", json.dumps(_dataset(_markup(frame))))
        .replace("__LAYOUT__", json.dumps(layout))
        .replace("__SCRIPT__", _script())
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _xaxis(lo: str, hi: str) -> dict:
    return {"xaxis": {"range": [lo, hi]}}


# ------------------------------------------------------------ the emitted slot


def test_the_slot_ships_the_series_and_starts_hidden():
    markup = _markup(FRAME)
    data = _dataset(markup)
    assert "display:none" in markup
    assert data["x"].split(",")[0] == "2026-06-26T00:00"
    assert data["y"].split(",") == [f"{v:.4f}" for v in FRAME["Close"]]
    # The catalog string keeps its placeholders: the browser fills them in.
    assert data["tmpl"] == TMPL
    assert data["up"] == "#0f0" and data["down"] == "#f00"


def test_a_gap_in_the_series_ships_as_an_empty_field():
    frame = FRAME.copy()
    frame.iloc[1, 0] = float("nan")
    assert _dataset(_markup(frame))["y"].split(",")[1] == ""


def test_the_template_ships_in_both_catalogs():
    for lang in ("en", "es"):
        s = translate("ticker.range_change", lang, pct="P", start="S", end="E")
        assert "P" in s and "S" in s and "E" in s


def test_the_price_traces_are_tagged_for_the_bridge():
    """The JS finds the price chart by its meta="price" trace — both the line
    and the candlestick rendering must carry it."""
    src = PAGE.read_text()
    assert src.count('meta="price",') == 2
    assert "range_readout.render(c1, df)" in src


# --------------------------------------------------------- the browser reading

pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the chart JS"
)


@pytestmark_node
def test_a_zoomed_window_reports_its_own_change(tmp_path):
    """First to last close inside the picked window, not the whole period."""
    got = _run(tmp_path, _xaxis("2026-06-29 00:00:00", "2026-07-02 23:59:00"))
    assert got["text"] == "+4.55% from Jun 29, 26 to Jul 2, 26"  # 330 -> 345
    assert got["display"] == "" and got["color"] == "#0f0"


@pytestmark_node
def test_a_losing_window_reads_red_and_negative(tmp_path):
    got = _run(tmp_path, _xaxis("2026-07-01 00:00:00", "2026-07-02 23:59:00"))
    assert got["text"].startswith("-4.17%")  # 360 -> 345
    assert got["color"] == "#f00"


@pytestmark_node
def test_plotlys_own_range_stamps_parse(tmp_path):
    """A drag hands back "2026-06-25 15:07:03.5225": space-separated (Safari's
    Date.parse refuses that) with a sub-millisecond fraction (outside the ISO
    grammar). Both are normalised before parsing."""
    got = _run(tmp_path, _xaxis("2026-06-25 15:07:03.5225", "2026-07-02 09:12:44.1"))
    assert got["text"].startswith("+15.00%")  # 300 -> 345, whole series in view


@pytestmark_node
def test_a_gap_at_the_edge_starts_from_the_next_real_bar(tmp_path):
    frame = FRAME.copy()
    frame.iloc[1, 0] = float("nan")
    got = _run(tmp_path, _xaxis("2026-06-29 00:00:00", "2026-07-02 23:59:00"), frame)
    assert got["text"].startswith("+1.47%")  # 340 -> 345, the NaN skipped


@pytestmark_node
def test_an_intraday_window_labels_the_clock(tmp_path):
    """Under two days apart, both endpoints would print the same date."""
    frame = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-07-01 09:30", "2026-07-01 16:00"]),
    )
    got = _run(tmp_path, _xaxis("2026-07-01 00:00:00", "2026-07-01 23:00:00"), frame)
    assert "09:30" in got["text"] and "04:00 PM" in got["text"]


@pytestmark_node
def test_resetting_the_zoom_clears_the_readout(tmp_path):
    got = _run(tmp_path, {"xaxis": {"autorange": True, "range": [0, 1]}})
    assert got["text"] == "" and got["display"] == "none"


@pytestmark_node
def test_a_window_holding_one_bar_says_nothing(tmp_path):
    got = _run(tmp_path, _xaxis("2026-06-30 06:00:00", "2026-06-30 18:00:00"))
    assert got["text"] == "" and got["display"] == "none"
