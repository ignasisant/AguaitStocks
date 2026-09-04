"""The zoom-range change readout under the ticker price chart.

Drag-zooming the price chart picks a custom window, but Plotly reports that as
a client-side relayout and Streamlit surfaces no relayout event (only box/lasso
*selections*, which would cost the drag-to-zoom gesture and a server round-trip
per drag). So the change over the picked window is computed in the browser: the
price series rides along in the readout slot's data attributes — Plotly
serializes numeric arrays as base64 ({"dtype", "bdata"}), so gd.data is not
readable from JS without decoding it — and the picked window is read off the
chart's own layout once the drag ends. No rerun, so the line lands as the mouse
comes up. Desktop only: phones pin both axes.

Lives outside app_pages/ so it can be imported: the page module runs its auth
gate at import time, which is why this used to be lifted out of its AST to be
tested at all.
"""

from __future__ import annotations

import html

import pandas as pd

from stocks.web.ds import DOWN_COLOR, FS_XS, UP_COLOR
from stocks.web.frames import dates
from stocks.web.i18n import active_language
from stocks.web.i18n import t as tr

RANGE_JS = r"""
<script>
(function () {
  if (window.__topstocksRangeChange) return;  /* wire once per session */
  window.__topstocksRangeChange = true;
  /* Axis range endpoints arrive as "2026-06-28 15:07:03.5225" (space, and a
     sub-millisecond fraction outside the ISO grammar Safari holds to); our own
     stamps are ISO. Neither carries a zone — the frame is exchange-local wall
     time, as the chart's — so both parse alike and only their span is used. */
  const ms = (v) => {
    if (typeof v === "number") return v;
    return Date.parse(String(v).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  };
  /* The price chart is the one carrying a meta="price" trace; every other
     chart on the page keeps its own zoom to itself. */
  const chart = () =>
    Array.prototype.find.call(
      document.querySelectorAll(".js-plotly-plot"),
      (gd) => (gd.data || []).some((t) => t.meta === "price")
    );
  const clear = (b) => {
    if (b) { b.textContent = ""; b.style.display = "none"; }
  };
  const read = () => {
    /* Both nodes are looked up per event, never captured: Streamlit replaces
       them on every rerun (period or chart-type switch), and the fresh slot
       starts out empty. */
    const gd = chart();
    const b = document.querySelector(".ts-range-change");
    if (!gd || !b) return;
    const ax = (gd.layout || {}).xaxis || {};
    if (ax.autorange || !ax.range) { clear(b); return; }  /* back to the window */
    const xs = (b.dataset.x || "").split(",");
    const ys = (b.dataset.y || "").split(",");
    const lo = Math.min(ms(ax.range[0]), ms(ax.range[1]));
    const hi = Math.max(ms(ax.range[0]), ms(ax.range[1]));
    let i0 = -1, i1 = -1;
    for (let i = 0; i < xs.length; i++) {
      if (!ys[i]) continue;  /* gap in the series */
      const t = ms(xs[i]);
      if (!(t >= lo && t <= hi)) continue;
      if (i0 < 0) i0 = i;
      i1 = i;
    }
    if (i0 < 0 || i1 === i0) { clear(b); return; }  /* under two bars in view */
    const a = parseFloat(ys[i0]), z = parseFloat(ys[i1]);
    if (!a || isNaN(z)) { clear(b); return; }
    const pct = (z / a - 1) * 100;
    /* Intraday windows land inside one date: label those with the clock. */
    const fmt = new Intl.DateTimeFormat(b.dataset.locale || undefined,
      ms(xs[i1]) - ms(xs[i0]) < 2 * 864e5
        ? {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}
        : {day: "numeric", month: "short", year: "2-digit"});
    b.textContent = (b.dataset.tmpl || "{pct} {start} {end}")
      .replace("{pct}", (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%")
      .replace("{start}", fmt.format(new Date(ms(xs[i0]))))
      .replace("{end}", fmt.format(new Date(ms(xs[i1]))));
    b.style.color = pct >= 0 ? b.dataset.up : b.dataset.down;
    b.style.display = "";
  };
  /* Deliberately not gd.on("plotly_relayout"): Streamlit re-plots the same
     div on a fragment rerun, and Plotly.newPlot purges the handlers off it
     while the element (so any "already wired" mark on it) survives — the
     readout then goes dead for the rest of the session. Document-level
     listeners outlive every remount. The drag ends on mouseup, the reset on
     dblclick, and the modebar buttons on their own click; the timeout lets
     Plotly land the relayout before the layout is read.
     The listeners take no target filter: Plotly covers the whole document
     with a .dragcover while a drag is live, so the mouseup that ends a zoom
     lands outside the chart. read() is a no-op whenever the axis is not
     zoomed, which is every other click on the page. */
  const after = () => setTimeout(read, 80);
  document.addEventListener("mouseup", after, true);
  document.addEventListener("dblclick", after, true);
})();
</script>
"""

def render(box, df: pd.DataFrame) -> None:
    """Empty slot under the period change, filled by RANGE_JS on zoom."""
    xs = ",".join(dates(df).strftime("%Y-%m-%dT%H:%M"))
    ys = ",".join("" if pd.isna(v) else f"{v:.4f}" for v in df["Close"])
    # The catalog string formatted with its own placeholders back in, so the
    # browser substitutes the numbers into a translated template.
    tmpl = tr("ticker.range_change", pct="{pct}", start="{start}", end="{end}")
    box.html(
        '<div class="ts-range-change"'
        f' style="display:none;font-size:{FS_XS};line-height:1.6"'
        f' data-x="{xs}" data-y="{ys}"'
        f' data-locale="{html.escape(active_language(), quote=True)}"'
        f' data-up="{UP_COLOR}" data-down="{DOWN_COLOR}"'
        f' data-tmpl="{html.escape(tmpl, quote=True)}"></div>' + RANGE_JS,
        unsafe_allow_javascript=True,
    )
