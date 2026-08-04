"""Analyst consensus estimates: fetch (network) + normalize (pure).

yfinance is the *loading* source (free, no key). Everything here is
**consensus** level — an analyst aggregate, never a fact. Consensus drifts, is
often stale, and clusters around the sell-side's incentives, so cross-check any
price target or growth figure on Koyfin/TIKR before acting on it (see the
`consensus` reliability level in stocks.analysis.fundamentals.KPI_SOURCES).

`fetch_estimates` touches the network; `consensus` is pure over an already
pulled RawEstimates so tests run offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# yfinance period labels: "0q"/"+1q" = current/next quarter, "0y"/"+1y" = current/next FY.
CURRENT_FY = "0y"
NEXT_FY = "+1y"

# strongBuy..strongSell mapped to 1..5, matching yfinance recommendationMean.
_RATING_SCORE = {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5}


@dataclass
class RawEstimates:
    """One ticker's raw analyst data as yfinance returns it."""

    ticker: str
    price_targets: dict = field(default_factory=dict)
    earnings_estimate: pd.DataFrame = field(default_factory=pd.DataFrame)
    revenue_estimate: pd.DataFrame = field(default_factory=pd.DataFrame)
    recommendations: pd.DataFrame = field(default_factory=pd.DataFrame)
    growth_estimates: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass(frozen=True)
class Consensus:
    """Normalized consensus view — all figures are consensus-level, not fact."""

    ticker: str
    price: float | None = None  # current price per the price-target payload
    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    target_median: float | None = None
    rating: str | None = None  # label derived from the recommendation split
    rating_mean: float | None = None  # 1 (strong buy) .. 5 (strong sell)
    rating_counts: dict = field(default_factory=dict)
    eps_cy: float | None = None  # current-FY consensus EPS
    eps_next_fy: float | None = None  # next-FY consensus EPS
    eps_growth_next_fy: float | None = None
    rev_cy: float | None = None
    rev_next_fy: float | None = None
    rev_growth_next_fy: float | None = None

    @property
    def target_upside(self) -> float | None:
        """Mean-target implied upside vs current price; None if unavailable."""
        if not self.price or self.target_mean is None:
            return None
        return self.target_mean / self.price - 1


def fetch_estimates(ticker: str) -> RawEstimates:
    """Download price targets, EPS/revenue estimates, and ratings for one ticker.

    Each field degrades to empty on error — a name with no analyst coverage
    yields an all-empty RawEstimates rather than raising.
    """
    import yfinance as yf

    t = yf.Ticker(ticker)

    def _safe(getter, default):
        try:
            value = getter()
        except Exception:
            return default
        return default if value is None else value

    return RawEstimates(
        ticker=ticker.upper(),
        price_targets=_safe(lambda: t.analyst_price_targets, {}) or {},
        earnings_estimate=_safe(lambda: t.earnings_estimate, pd.DataFrame()),
        revenue_estimate=_safe(lambda: t.revenue_estimate, pd.DataFrame()),
        recommendations=_safe(lambda: t.recommendations, pd.DataFrame()),
        growth_estimates=_safe(lambda: t.growth_estimates, pd.DataFrame()),
    )


def _pick(df: pd.DataFrame, period: str, col: str) -> float | None:
    """Value at (period, col) of an estimate frame; None when absent/NaN."""
    if df is None or df.empty or period not in df.index or col not in df.columns:
        return None
    value = df.loc[period, col]
    return float(value) if pd.notna(value) else None


def _latest_recommendation(df: pd.DataFrame) -> dict[str, int]:
    """strongBuy..strongSell counts for the most recent period ('0m')."""
    if df is None or df.empty:
        return {}
    if "period" in df.columns:
        current = df[df["period"] == "0m"]
        row = current.iloc[0] if not current.empty else df.iloc[0]
    else:
        row = df.iloc[0]
    return {
        k: int(row[k]) for k in _RATING_SCORE if k in row and pd.notna(row[k])
    }


def rating_from_counts(counts: dict[str, int]) -> tuple[str | None, float | None]:
    """(label, mean) from a recommendation split; (None, None) if no votes.

    Mean is the 1..5 sell-side scale (1 = strong buy); the label bins it the
    way yfinance's recommendationKey does.
    """
    total = sum(counts.values())
    if not total:
        return None, None
    mean = sum(_RATING_SCORE[k] * n for k, n in counts.items()) / total
    for cutoff, label in (
        (1.5, "strong buy"),
        (2.5, "buy"),
        (3.5, "hold"),
        (4.5, "sell"),
    ):
        if mean <= cutoff:
            return label, mean
    return "strong sell", mean


def consensus(raw: RawEstimates) -> Consensus:
    """Fold raw yfinance payloads into a normalized Consensus (pure)."""
    pt = raw.price_targets or {}
    counts = _latest_recommendation(raw.recommendations)
    rating, rating_mean = rating_from_counts(counts)

    eps = raw.earnings_estimate
    rev = raw.revenue_estimate
    return Consensus(
        ticker=raw.ticker,
        price=pt.get("current"),
        target_mean=pt.get("mean"),
        target_high=pt.get("high"),
        target_low=pt.get("low"),
        target_median=pt.get("median"),
        rating=rating,
        rating_mean=rating_mean,
        rating_counts=counts,
        eps_cy=_pick(eps, CURRENT_FY, "avg"),
        eps_next_fy=_pick(eps, NEXT_FY, "avg"),
        eps_growth_next_fy=_pick(eps, NEXT_FY, "growth"),
        rev_cy=_pick(rev, CURRENT_FY, "avg"),
        rev_next_fy=_pick(rev, NEXT_FY, "avg"),
        rev_growth_next_fy=_pick(rev, NEXT_FY, "growth"),
    )


def estimate_currency(df: pd.DataFrame) -> str | None:
    """Currency label of an estimate frame; None when absent."""
    if df is None or df.empty or "currency" not in df.columns:
        return None
    vals = df["currency"].dropna()
    return str(vals.iloc[0]) if not vals.empty else None


def long_term_growth(df: pd.DataFrame) -> float | None:
    """Sell-side long-term (per-annum) EPS growth from growth_estimates.

    yfinance publishes it on the "LTG" row; often NaN. Column name changed
    across yfinance versions (stockTrend vs stock), so try both.
    """
    if df is None or df.empty or "LTG" not in df.index:
        return None
    for col in ("stockTrend", "stock"):
        if col in df.columns and pd.notna(df.loc["LTG", col]):
            return float(df.loc["LTG", col])
    return None


def _metric_path(df: pd.DataFrame, years: int, ltg: float | None) -> list[dict]:
    """Forward avg/low/high per year for one estimate frame.

    Years 1-2 come straight from the 0y/+1y consensus; later years extrapolate
    the last consensus avg at `ltg` (when given) else the +1y consensus growth
    rate, flagged extrapolated. Extrapolation stops on non-positive bases —
    a growth rate applied to a loss is meaningless.
    """
    path: list[dict] = []
    for period in (CURRENT_FY, NEXT_FY):
        avg = _pick(df, period, "avg")
        if avg is None:
            break
        path.append(
            {
                "avg": avg,
                "low": _pick(df, period, "low"),
                "high": _pick(df, period, "high"),
                "extrapolated": False,
            }
        )
    if not path:
        return []
    growth = ltg if ltg is not None else _pick(df, NEXT_FY, "growth")
    if growth is None and len(path) == 2 and path[0]["avg"] > 0:
        growth = path[1]["avg"] / path[0]["avg"] - 1
    while len(path) < years:
        if growth is None or path[-1]["avg"] <= 0:
            break
        path.append(
            {
                "avg": path[-1]["avg"] * (1 + growth),
                "low": None,
                "high": None,
                "extrapolated": True,
            }
        )
    return path


def projection(raw: RawEstimates, last_fy: int, years: int = 3) -> pd.DataFrame:
    """Forward revenue / EPS consensus path for charting.

    Index is "2027E"-style labels relative to `last_fy` (the last *reported*
    fiscal year — yfinance's 0y period is the first unreported one). Columns:
    Revenue/EPS avg, *Low/*High consensus range (NaN once extrapolated) and
    *Ext extrapolation flags. Everything here is consensus or derived from it,
    never a fact. Empty frame when there is no forward consensus at all.
    """
    ltg = long_term_growth(raw.growth_estimates)
    rev_path = _metric_path(raw.revenue_estimate, years, None)
    eps_path = _metric_path(raw.earnings_estimate, years, ltg)
    n = max(len(rev_path), len(eps_path))
    if n == 0:
        return pd.DataFrame()

    def col(path: list[dict], key: str) -> list:
        return [path[j][key] if j < len(path) else None for j in range(n)]

    return pd.DataFrame(
        {
            "Revenue": col(rev_path, "avg"),
            "RevenueLow": col(rev_path, "low"),
            "RevenueHigh": col(rev_path, "high"),
            "RevenueExt": [bool(v) for v in col(rev_path, "extrapolated")],
            "EPS": col(eps_path, "avg"),
            "EPSLow": col(eps_path, "low"),
            "EPSHigh": col(eps_path, "high"),
            "EPSExt": [bool(v) for v in col(eps_path, "extrapolated")],
        },
        index=[f"{last_fy + j + 1}E" for j in range(n)],
    ).astype(
        {
            c: float
            for c in ("Revenue", "RevenueLow", "RevenueHigh", "EPS", "EPSLow", "EPSHigh")
        }
    )
