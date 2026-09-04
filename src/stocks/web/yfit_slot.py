"""The y-axis refit that follows a zoom on the portfolio value chart.

Zooming the value chart picks a window, but the y axis is fixed (so a drag
never squashes the money scale) and Plotly leaves the y range where the full
span put it: a 2022 window worth 4k draws as a flat line on the floor of a 120k
axis. Streamlit surfaces no relayout event (only box/lasso selections, which
would cost the drag-to-zoom gesture and a server round trip per drag), so the
refit runs in the browser: the plotted band extents ride along in a hidden
slot's data attributes — Plotly serializes numeric arrays as base64 ({"dtype",
"bdata"}), so gd.data is not readable from JS — and the picked window is read
off the chart's own layout once the drag ends. Desktop only: phones pin both
axes, so there is no zoom to follow.

Lives outside app_pages/ so it can be imported: the page module reads the
ledger and calls st.stop at import time, which is why this used to be lifted
out of its AST to be tested at all.
"""

from __future__ import annotations

import pandas as pd

from stocks.web.frames import dates

YFIT_JS = r"""
<script>
(function () {
  if (window.__topstocksYFit) return;  /* wire once per session */
  window.__topstocksYFit = true;
  /* Axis endpoints come back as "2026-06-28 15:07:03.5225" (space, and a
     sub-millisecond fraction outside the ISO grammar Safari holds to); the
     shipped stamps are ISO dates. Normalise both before parsing. */
  const ms = (v) => {
    if (typeof v === "number") return v;
    return Date.parse(String(v).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  };
  /* The history chart is the one carrying a meta="history" trace; every other
     chart on the page keeps its own axes to itself. */
  const chart = () =>
    Array.prototype.find.call(
      document.querySelectorAll(".js-plotly-plot"),
      (gd) => (gd.data || []).some((t) => t.meta === "history")
    );
  const fit = () => {
    /* Both nodes are looked up per event, never captured: Streamlit replaces
       them whenever the range buttons rerun the fragment. */
    const gd = chart();
    const b = document.querySelector(".ts-yfit");
    if (!gd || !b || !window.Plotly) return;
    const ax = (gd.layout || {}).xaxis || {};
    const ya = (gd.layout || {}).yaxis || {};
    if (ax.autorange || !ax.range) {  /* zoom reset: back to the whole span */
      gd.__tsYFit = "";
      if (!ya.autorange) window.Plotly.relayout(gd, {"yaxis.autorange": true});
      return;
    }
    const xs = (b.dataset.x || "").split(",");
    const los = (b.dataset.lo || "").split(",");
    const his = (b.dataset.hi || "").split(",");
    const x0 = Math.min(ms(ax.range[0]), ms(ax.range[1]));
    const x1 = Math.max(ms(ax.range[0]), ms(ax.range[1]));
    let i0 = -1, i1 = -1;
    for (let i = 0; i < xs.length; i++) {
      const t = ms(xs[i]);
      if (t < x0 || t > x1) continue;
      if (i0 < 0) i0 = i;
      i1 = i;
    }
    if (i0 < 0) return;  /* the window fell between two daily points */
    /* The line enters and leaves the window mid-segment: the points just
       outside each edge are drawn too, so they count towards the extents. */
    i0 = Math.max(0, i0 - 1);
    i1 = Math.min(xs.length - 1, i1 + 1);
    let lo = Infinity, hi = -Infinity;
    for (let i = i0; i <= i1; i++) {
      const a = parseFloat(los[i]), z = parseFloat(his[i]);
      if (a < lo) lo = a;
      if (z > hi) hi = z;
    }
    if (!isFinite(lo) || !isFinite(hi)) return;
    const pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.06 || 1;
    /* A book is never worth less than nothing: pad down to zero, not past it. */
    lo = lo >= 0 && lo - pad < 0 ? 0 : lo - pad;
    hi = hi + pad;
    const key = lo.toFixed(2) + ":" + hi.toFixed(2);
    if (gd.__tsYFit === key) return;  /* same window as the last drag */
    gd.__tsYFit = key;
    window.Plotly.relayout(gd, {"yaxis.range": [lo, hi]});
  };
  /* Deliberately not gd.on("plotly_relayout"): Streamlit re-plots the same
     div on a fragment rerun, and Plotly.newPlot purges the handlers off it
     while the element (so any "already wired" mark on it) survives — the
     refit would go dead for the rest of the session. Document-level listeners
     outlive every remount. The drag ends on mouseup, the reset on dblclick,
     and the modebar buttons on their own click; the timeout lets Plotly land
     its own relayout before the layout is read.
     The listeners take no target filter: Plotly covers the whole document
     with a .dragcover while a drag is live, so the mouseup that ends a zoom
     lands outside the chart. fit() is a no-op whenever the axis is not
     zoomed, which is every other click on the page. */
  const after = () => setTimeout(fit, 80);
  document.addEventListener("mouseup", after, true);
  document.addEventListener("dblclick", after, true);
})();
</script>
"""

def render(box, hist: pd.DataFrame) -> None:
    """Hidden slot carrying the plotted band extents, read by `YFIT_JS`."""
    pair = hist[["value", "injected"]]
    xs = ",".join(dates(hist).strftime("%Y-%m-%d"))
    los = ",".join(f"{v:.2f}" for v in pair.min(axis=1))
    his = ",".join(f"{v:.2f}" for v in pair.max(axis=1))
    box.html(
        '<div class="ts-yfit" style="display:none"'
        f' data-x="{xs}" data-lo="{los}" data-hi="{his}"></div>' + YFIT_JS,
        unsafe_allow_javascript=True,
    )
