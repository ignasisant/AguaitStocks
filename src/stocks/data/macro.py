"""Macro series without an API key — FRED's chart CSV and Eurostat's HICP API.

Two open endpoints carry everything the market-pulse page needs beyond price
data, and neither wants a registration:

* **FRED** serves any series its charts can draw as CSV —
  ``fredgraph.csv?id=DGS10`` — so the whole US rates/credit/inflation side
  arrives without the keyed `fred/series/observations` API: nominal and real
  yields, the 2s10s and 3m10y curves, high-yield and investment-grade OAS,
  breakevens, both policy rates, CPI. `cosd` trims the download to the window
  actually plotted (DGS10's full history is 16k rows).
* **Eurostat** serves HICP per country. The dataset code matters more than it
  looks: ``prc_hicp_manr`` (ECOICOP v1) froze at 2025-12 when the February
  2026 methodology change landed, and the live series is ``prc_hicp_minr``
  (ECOICOP v2), whose item dimension is named ``coicop18`` — ``TOTAL`` for
  headline, ``TOT_X_NRG_FOOD`` for core. Query the old code and you get a
  200 with data that looks current and is nine months stale, which is why both
  codes are pinned here next to this note.

Euro-area HICP and US CPI do not publish on the same calendar (a flash
estimate lands weeks before the US print), so every row this module returns
carries its own reference period. Nothing here aligns them into one column.

Responses are cached to disk under ``data/macro/`` for six hours — these are
daily-at-best series and a Streamlit page refetches on every widget click. A
failed refresh falls back to the stale file rather than raising: a throttled
host should show yesterday's yield curve, not an empty card.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from stocks import obs
from stocks.config import DATA_DIR
from stocks.data.http import get_bytes

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
EUROSTAT_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}"
)
# ECOICOP v2 — the live HICP dataset. See the module docstring before changing.
HICP_DATASET = "prc_hicp_minr"
HICP_HEADLINE = "TOTAL"
HICP_CORE = "TOT_X_NRG_FOOD"

CACHE_DIR = DATA_DIR / "macro"
NA = float("nan")

# FRED's edge blocks the shared default User-Agent outright: a request headed
# `stocks-toolkit` is not refused, it is tarpitted — the connection is accepted
# and then never answered, so it surfaces as a read timeout and looks like a
# network fault rather than a block. Verified deterministic: alternating
# `stocks-toolkit` with `curl/8` against the same URL fails and succeeds every
# other request. Any UA carrying a contact URL passes, so this identifies the
# app properly and is the one header that must not be dropped back to the
# default. Eurostat and the ECB are indifferent to it.
USER_AGENT = "TopStocks/1.0 (+https://topstocks.app)"
TTL_S = 6 * 3600

# Every FRED series the app reads, with what it is. Ids are opaque, so the
# comment is the documentation — a reader should never have to look one up.
FRED_SERIES = {
    "DGS10": "US 10y nominal yield",
    "DFII10": "US 10y real (TIPS) yield",
    "T10Y2Y": "US 10y minus 2y — curve slope",
    "T10Y3M": "US 10y minus 3m — curve slope, recession-signal variant",
    "BAMLH0A0HYM2": "US high-yield OAS — the credit-stress gauge",
    "BAMLC0A0CM": "US investment-grade OAS",
    "T5YIE": "US 5y inflation breakeven",
    "DFEDTARU": "Fed funds target, upper bound",
    "ECBDFR": "ECB deposit facility rate",
    "DTWEXBGS": "Broad trade-weighted dollar index",
    "NFCI": "Chicago Fed national financial conditions index",
    "CPIAUCNS": "US CPI, all items, not seasonally adjusted",
    "CPILFENS": "US core CPI, not seasonally adjusted",
}


# ------------------------------------------------------------------ disk cache
def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _cached(name: str, fetch, *, ttl: float = TTL_S) -> bytes | None:
    """`fetch()`'s bytes, memoized on disk for `ttl` seconds.

    A refresh that fails serves the stale file when there is one. That is the
    difference between "Yahoo/FRED is unreachable from this host right now"
    and "this card has no data" — for a daily series the stale answer is
    almost always the right one to show.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_slug(name)}.cache"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    try:
        body = fetch()
    except Exception as exc:
        if path.exists():
            obs.warn("macro.stale", source=name, error=type(exc).__name__)
            return path.read_bytes()
        obs.warn("macro.failed", source=name, error=type(exc).__name__)
        return None
    path.write_bytes(body)
    return body


# ------------------------------------------------------------------------ FRED
def fred(sid: str, *, years: int = 3) -> pd.Series:
    """One FRED series as a date-indexed float Series (empty if unavailable).

    Blank cells are FRED's "no observation" (market holidays in the daily
    yield series) and are dropped, so the index is trading days only.
    """
    start = (date.today() - timedelta(days=365 * years + 30)).isoformat()
    body = _cached(
        f"fred_{sid}_{years}y",
        lambda: get_bytes(
            FRED_URL.format(sid=sid, start=start),
            user_agent=USER_AGENT,
            timeout=20,
        ),
    )
    if not body:
        return pd.Series(dtype=float, name=sid)
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    if len(rows) < 2:
        return pd.Series(dtype=float, name=sid)
    idx, vals = [], []
    for row in rows[1:]:
        if len(row) < 2 or not row[1].strip():
            continue
        try:
            vals.append(float(row[1]))
        except ValueError:
            continue
        idx.append(row[0])
    s = pd.Series(vals, index=pd.to_datetime(idx), dtype=float, name=sid)
    return s.sort_index()


def fred_many(
    sids: list[str], *, years: int = 3, max_workers: int = 8
) -> dict[str, pd.Series]:
    """Several FRED series concurrently; a series that fails comes back empty.

    One card asks for six ids and each is its own HTTP round trip, so these go
    out in parallel — serially they cost the page a visible second or two on a
    cold cache.
    """
    if not sids:
        return {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return dict(pool.map(lambda s: (s, fred(s, years=years)), sids))


def yoy(levels: pd.Series, periods: int = 12) -> pd.Series:
    """Year-over-year percent change of an index-level series (monthly CPI).

    FRED publishes CPI as an index, not a rate; `periods=12` is the twelve
    monthly observations that make a year. Percent, not a fraction, to match
    how Eurostat publishes HICP.
    """
    if levels.empty:
        return pd.Series(dtype=float, name=levels.name)
    return (levels / levels.shift(periods) - 1.0).dropna() * 100.0


# -------------------------------------------------------------------- Eurostat
def _jsonstat_frame(payload: dict, value_dim: str, row_dim: str) -> pd.DataFrame:
    """A JSON-stat 2.0 cube flattened to a frame: `row_dim` index, `value_dim` columns.

    JSON-stat stores the cube as ``value``: a sparse map from a single
    row-major offset into the dimension sizes. Decoding it means walking the
    declared dimension order (``id`` / ``size``) and reversing the offset —
    every other dimension in the query has to be a single selection, which is
    what the callers here send.
    """
    dims, sizes = payload["id"], payload["size"]
    # Category index maps are declared as {code: position}; sort by position so
    # the codes line up with the offsets rather than with JSON key order.
    order = {
        d: [
            code
            for code, _ in sorted(
                payload["dimension"][d]["category"]["index"].items(),
                key=lambda kv: kv[1],
            )
        ]
        for d in dims
    }
    strides = {}
    step = 1
    for d, n in zip(reversed(dims), reversed(sizes), strict=True):
        strides[d] = step
        step *= n
    out = pd.DataFrame(
        index=pd.Index(order[row_dim], name=row_dim),
        columns=order[value_dim],
        dtype=float,
    )
    for offset, value in payload["value"].items():
        pos = int(offset)
        coords = {}
        for d in dims:
            coords[d] = order[d][(pos // strides[d]) % len(order[d])]
        out.loc[coords[row_dim], coords[value_dim]] = value
    return out


def hicp(
    geos: tuple[str, ...],
    *,
    item: str = HICP_HEADLINE,
    periods: int = 14,
) -> pd.DataFrame:
    """Annual HICP inflation rate (percent) per country: months × geo.

    `geos` are Eurostat area codes — two-letter countries plus the aggregates
    ``EA`` (euro area) and ``EU``. Empty frame when the API is unreachable and
    nothing is cached.
    """
    if not geos:
        return pd.DataFrame()
    query = (
        f"?format=JSON&unit=RCH_A&coicop18={item}&lastTimePeriod={periods}"
        + "".join(f"&geo={g}" for g in geos)
    )
    url = EUROSTAT_URL.format(dataset=HICP_DATASET) + query
    body = _cached(
        f"hicp_{item}_{'-'.join(geos)}_{periods}",
        lambda: get_bytes(url, user_agent=USER_AGENT, timeout=25),
    )
    if not body:
        return pd.DataFrame()
    try:
        payload = json.loads(body)
        if "error" in payload:
            obs.warn("macro.eurostat_error", detail=str(payload["error"])[:200])
            return pd.DataFrame()
        frame = _jsonstat_frame(payload, value_dim="geo", row_dim="time")
    except (ValueError, KeyError, IndexError) as exc:
        obs.warn("macro.eurostat_parse", error=type(exc).__name__)
        return pd.DataFrame()
    return frame.sort_index()


def hicp_updated(
    geos: tuple[str, ...], *, item: str = HICP_HEADLINE, periods: int = 14
) -> str | None:
    """The `updated` stamp Eurostat put on the cached HICP response, if any."""
    path = CACHE_DIR / f"{_slug(f'hicp_{item}_{"-".join(geos)}_{periods}')}.cache"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_bytes()).get("updated")
    except ValueError:
        return None


# --------------------------------------------------------------- combined view
# The areas the inflation card covers: the euro-area aggregate plus the member
# states a EUR investor's costs and holdings actually sit in. Two absences are
# deliberate. The UK is gone from Eurostat's HICP entirely — every observation
# comes back NaN post-Brexit, so asking for it only buys an empty row. The US
# is not in this dataset at all and rides in from FRED below, on its own
# reference month: non-euro EU members publish a month behind the euro-area
# flash estimate, and the US CPI print is later still, which is why every row
# carries its own `period` instead of sharing a column header.
INFLATION_AREAS = ("EA", "ES", "DE", "FR", "IT", "NL", "PT")


def inflation(
    areas: tuple[str, ...] = INFLATION_AREAS, *, periods: int = 14
) -> pd.DataFrame:
    """Headline and core inflation per area, with the trend behind the print.

    One row per area:
        area, period, headline, core, prior, six_months, momentum, path

    `period` is that area's own reference month — the euro-area flash estimate
    and the US CPI print are weeks apart, and a single "latest month" column
    would silently misdate one of them.

    `momentum` is the annual rate now minus the annual rate six months ago, in
    percentage points, and it is the number that carries the story: a 4.5%
    print is a level, a 4.5% that was 2.5% two quarters ago is a
    re-acceleration. Deliberately *not* a three-month annualised rate, which is
    the usual way to read inflation momentum — Eurostat publishes HICP without
    seasonal adjustment only, so annualising a three-month stretch of raw data
    compounds January sales and summer travel into what looks like a trend.
    A year-over-year rate is already free of seasonality, so comparing two of
    them is too.
    """
    MOMENTUM_MONTHS = 6
    head = hicp(areas, item=HICP_HEADLINE, periods=periods)
    core = hicp(areas, item=HICP_CORE, periods=periods)
    us = fred_many(["CPIAUCNS", "CPILFENS"], years=3)
    us_head, us_core = yoy(us["CPIAUCNS"]), yoy(us["CPILFENS"])

    rows = []
    for area in areas:
        if area not in head.columns:
            continue
        series = head[area].dropna()
        if series.empty:
            continue
        has_core = area in core.columns
        core_series = core[area].dropna() if has_core else pd.Series(dtype=float)
        core_last = float(core_series.iloc[-1]) if not core_series.empty else NA
        back = (
            float(series.iloc[-MOMENTUM_MONTHS - 1])
            if len(series) > MOMENTUM_MONTHS
            else NA
        )
        rows.append(
            {
                "area": area,
                "period": str(series.index[-1]),
                "headline": float(series.iloc[-1]),
                "core": core_last,
                "prior": float(series.iloc[-2]) if len(series) > 1 else NA,
                "six_months": back,
                "momentum": float(series.iloc[-1]) - back,
                "path": [float(v) for v in series.iloc[-periods:]],
            }
        )
    if not us_head.empty:
        us_back = (
            float(us_head.iloc[-MOMENTUM_MONTHS - 1])
            if len(us_head) > MOMENTUM_MONTHS
            else NA
        )
        rows.append(
            {
                "area": "US",
                "period": us_head.index[-1].strftime("%Y-%m"),
                "headline": float(us_head.iloc[-1]),
                "core": float(us_core.iloc[-1]) if not us_core.empty else NA,
                "prior": float(us_head.iloc[-2]) if len(us_head) > 1 else NA,
                "six_months": us_back,
                "momentum": float(us_head.iloc[-1]) - us_back,
                "path": [float(v) for v in us_head.iloc[-periods:]],
            }
        )
    return pd.DataFrame(rows)


def as_of() -> str:
    """UTC timestamp for the "data as of" caption, minute precision."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
