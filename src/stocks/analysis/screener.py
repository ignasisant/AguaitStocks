"""Cross-sectional screener: rank and filter the whole watchlist by KPIs.

Turns the per-ticker fundamentals (stocks.analysis.fundamentals) into a screen
across every name you follow — cheap P/E, high ROIC, high FCF yield, low
leverage. Pure ranking/filtering functions run offline; only fetch_metrics_many
touches the network.

Percent KPIs are stored as fractions (ROIC 0.15 == 15%), so filter thresholds
use the same scale: --min roic=0.15.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

from stocks.analysis.fundamentals import (
    METRIC_ORDER,
    compute_metrics,
    format_value,
)
from stocks.data.fundamentals import fetch_fundamentals

# Compact valuation + quality columns for the default screen view.
DEFAULT_COLUMNS = [
    "price",
    "pe_ttm",
    "pe_fwd",
    "ev_ebitda",
    "roic",
    "fcf_yield",
    "net_debt_ebitda",
    "revenue_cagr",
    "net_margin",
]

# Lower value is "better" for these (cheaper / less levered / less dilutive);
# everything else in METRIC_ORDER ranks best high-to-low.
LOWER_IS_BETTER = frozenset(
    {
        "pe_ttm",
        "pe_fwd",
        "peg",
        "pb",
        "ev_ebitda",
        "ev_sales",
        "net_debt_ebitda",
        "share_dilution",
    }
)


@dataclass(frozen=True)
class Filter:
    """One screen constraint. kind is 'min' (keep >= value) or 'max' (<=)."""

    metric: str
    kind: str
    value: float

    def mask(self, df: pd.DataFrame) -> pd.Series:
        if self.metric not in df.columns:
            raise ValueError(f"unknown metric: {self.metric!r}")
        col = df[self.metric]
        if self.kind == "min":
            return col >= self.value
        if self.kind == "max":
            return col <= self.value
        raise ValueError(f"filter kind must be 'min' or 'max', got {self.kind!r}")


def metrics_frame(metrics: list[dict]) -> pd.DataFrame:
    """Raw numeric KPIs: one row per ticker, one column per metric key.

    Complements comparables_table (which is formatted strings): this frame is
    numeric so it can be sorted and filtered. Missing values become NaN.
    """
    rows = {str(m["ticker"]): {k: m.get(k) for k in METRIC_ORDER} for m in metrics}
    df = pd.DataFrame.from_dict(rows, orient="index")
    df = df.reindex(columns=METRIC_ORDER)
    return df.apply(pd.to_numeric, errors="coerce")


def apply_filters(df: pd.DataFrame, filters: list[Filter]) -> pd.DataFrame:
    """Rows passing every filter. Rows with NaN in a filtered column drop out."""
    if not filters:
        return df
    mask = pd.Series(True, index=df.index)
    for f in filters:
        mask &= f.mask(df).fillna(False)
    return df[mask]


def rank(df: pd.DataFrame, by: str, ascending: bool | None = None) -> pd.DataFrame:
    """Sort by a metric. Default direction follows LOWER_IS_BETTER so the top
    row is always the most attractive on that metric; NaNs sink to the bottom.
    """
    if by not in df.columns:
        raise ValueError(f"unknown metric: {by!r}")
    if ascending is None:
        ascending = by in LOWER_IS_BETTER
    return df.sort_values(by, ascending=ascending, na_position="last")


def format_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Numeric screen frame -> display strings with units (reuses format_value)."""
    out = pd.DataFrame(index=df.index)
    for col in df.columns:
        out[col] = [format_value(col, None if pd.isna(v) else v) for v in df[col]]
    return out


def _safe_metrics(ticker: str) -> dict:
    """compute_metrics for one ticker; a bad/dead ticker yields an all-n/a row
    rather than aborting the whole screen."""
    try:
        return compute_metrics(fetch_fundamentals(ticker))
    except Exception:
        return {"ticker": ticker.upper()}


def fetch_metrics_many(tickers: list[str], max_workers: int = 8) -> list[dict]:
    """Compute metrics for many tickers concurrently (yfinance calls are IO-bound)."""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_safe_metrics, tickers))
