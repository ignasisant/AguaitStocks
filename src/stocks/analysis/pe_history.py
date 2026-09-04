"""P/E relative to its own history — is today's multiple rich or cheap?

No free source publishes a clean historical P/E series, so we reconstruct it:

    P/E(t) = split-adjusted price(t) / TTM diluted EPS known at t

Price comes from yfinance (split-adjusted). TTM EPS is rebuilt from per-quarter
diluted EPS (SEC EDGAR primary; FMP fallback for non-US), summed over a trailing
four quarters and stamped at each quarter's *filing* date to avoid look-ahead
bias. As-reported EPS is split-adjusted to match the price series — without this
a split (e.g. NVDA 10:1) destroys the historical window.

Pure functions (split_factors / discrete_quarters / reconstruct_ttm_eps /
pe_series / window_stats) take their inputs explicitly and run offline; only
pe_vs_history touches the network.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

import pandas as pd

# Trailing windows to average the multiple over, in calendar days.
WINDOWS: dict[str, int] = {"6mo": 182, "1y": 365, "2y": 730, "5y": 1825}


def split_factors(
    splits: Mapping[date, float] | pd.Series, ends: Iterable[date]
) -> dict[date, float]:
    """Cumulative split ratio applied *after* each period end.

    Divide as-reported EPS for a period by this factor to express it in
    today's share basis (matching split-adjusted prices). A 10:1 split after
    a quarter gives that quarter a factor of 10.
    """
    items: list[tuple[date, float]]
    if isinstance(splits, pd.Series):
        # A yfinance splits Series is keyed by Timestamp; the comparison
        # below is against plain dates.
        items = [
            (ts.date(), float(v))
            for ts, v in zip(pd.DatetimeIndex(splits.index), splits, strict=True)
        ]
    else:
        items = [(k, float(v)) for k, v in splits.items()]
    out: dict[date, float] = {}
    for end in ends:
        factor = 1.0
        for sdate, ratio in items:
            if sdate > end and ratio:
                factor *= ratio
        out[end] = factor
    return out


def discrete_quarters(
    facts: pd.DataFrame, factors: Mapping[date, float] | None = None
) -> pd.DataFrame:
    """Split-adjusted discrete quarterly EPS rows, with Q4 derived from FY − 3Q.

    `facts` has columns end/filed/eps/kind as returned by edgar/fmp
    diluted_eps_facts: 'Q' rows are discrete quarters, 'FY' rows full fiscal
    years (10-Ks never report a discrete Q4). Split adjustment happens FIRST so
    the FY and its three quarters share one per-share basis — subtracting
    as-reported values across a mid-year split would corrupt Q4.

    Returns columns end/filed/eps, oldest-first. A FY row only yields a Q4 when
    exactly three discrete quarters fall inside its year window.
    """
    cols = ["end", "filed", "eps"]
    if facts is None or facts.empty:
        return pd.DataFrame(columns=cols)
    df = facts.copy()
    if factors:
        df["eps"] = [
            eps / factors.get(end, 1.0)
            for end, eps in zip(df["end"], df["eps"], strict=True)
        ]
    if "kind" not in df.columns:
        return df[cols].sort_values("end").reset_index(drop=True)

    q = df[df["kind"] == "Q"]
    q_ends = set(q["end"])
    derived = []
    for _, fy in df[df["kind"] == "FY"].iterrows():
        if fy["end"] in q_ends:
            continue  # discrete Q4 already reported
        year_start = fy["end"] - timedelta(days=370)
        inside = q[(q["end"] > year_start) & (q["end"] < fy["end"])]
        if len(inside) != 3:
            continue  # missing quarters — can't isolate Q4
        derived.append(
            {"end": fy["end"], "filed": fy["filed"],
             "eps": float(fy["eps"]) - float(inside["eps"].sum())}
        )
    out = pd.concat([q[cols], pd.DataFrame(derived, columns=cols)], ignore_index=True)
    return out.sort_values("end").reset_index(drop=True)


def reconstruct_ttm_eps(
    quarters: pd.DataFrame, factors: Mapping[date, float] | None = None
) -> pd.Series:
    """Trailing-twelve-month EPS stamped at each quarter's filing date.

    `quarters` has columns end/filed/eps (oldest-first). `factors` split-adjusts
    each quarter's EPS; missing/None means factor 1.0. Returns a Series indexed
    by the filing date (when the market learned that TTM figure), duplicates
    resolved to the latest filing.
    """
    if quarters is None or quarters.empty or len(quarters) < 4:
        return pd.Series(dtype=float)
    q = quarters.sort_values("end").copy()
    if factors:
        q["eps"] = [
            eps / factors.get(end, 1.0)
            for end, eps in zip(q["end"], q["eps"], strict=True)
        ]
    q["ttm"] = q["eps"].rolling(4).sum()
    q = q.dropna(subset=["ttm"])
    ttm = pd.Series(q["ttm"].values, index=pd.to_datetime(q["filed"])).sort_index()
    return ttm[~ttm.index.duplicated(keep="last")]


def pe_series(close: pd.Series, ttm_known: pd.Series) -> pd.Series:
    """Daily P/E from split-adjusted close and filing-stamped TTM EPS.

    TTM EPS is forward-filled from each filing date onto the price index, so
    each day uses the most recent figure that was actually public that day.
    Non-positive or non-finite ratios (loss-making TTM) are dropped.
    """
    if close is None or close.empty or ttm_known.empty:
        return pd.Series(dtype=float)
    close = close.copy()
    close.index = pd.DatetimeIndex(close.index).tz_localize(None)
    idx = close.index.union(ttm_known.index)
    ttm_aligned = ttm_known.reindex(idx).ffill().reindex(close.index)
    pe = (close / ttm_aligned).replace([float("inf"), float("-inf")], pd.NA)
    pe = pe.dropna()
    return pe[pe > 0]


def window_stats(
    pe: pd.Series,
    current: float | None = None,
    windows: dict[str, int] = WINDOWS,
) -> pd.DataFrame:
    """Per-window mean/median/range, current percentile, z-score, premium.

    `premium` is current / mean − 1: positive means today's P/E sits above its
    trailing average (richer than usual), negative means cheaper. `percentile`
    is where today's P/E falls within the window's distribution (0–100).
    """
    if pe is None or pe.empty:
        return pd.DataFrame()
    current = float(pe.iloc[-1]) if current is None else float(current)
    end = pe.index.max()
    rows = []
    for label, days in windows.items():
        w = pe[pe.index >= end - pd.Timedelta(days=days)]
        if w.empty:
            continue
        mean, std = float(w.mean()), float(w.std())
        rows.append(
            {
                "window": label,
                "n": int(w.size),
                "current": current,
                "mean": mean,
                "median": float(w.median()),
                "min": float(w.min()),
                "max": float(w.max()),
                "percentile": float((w <= current).mean() * 100),
                "zscore": (current - mean) / std if std else float("nan"),
                "premium": current / mean - 1 if mean else float("nan"),
            }
        )
    return pd.DataFrame(rows).set_index("window")


def interpret(percentile: float) -> str:
    """One-word read of where the current multiple sits vs its own history."""
    if percentile >= 80:
        return "expensive vs own history"
    if percentile <= 20:
        return "cheap vs own history"
    return "in line with own history"


def pe_vs_history(
    ticker: str,
    close: pd.Series | None = None,
    current_pe: float | None = None,
) -> dict:
    """Orchestrate the full reconstruction. Network-touching.

    Returns {source, pe, stats, current}. `source` is 'SEC EDGAR', 'FMP', or
    None when neither yields quarterly EPS (e.g. non-US filer without FMP key).
    """
    import yfinance as yf

    from stocks.data import edgar, fmp
    from stocks.data.fetch import fetch_history

    facts = edgar.diluted_eps_facts(ticker)
    source = "SEC EDGAR"
    if facts.empty and fmp.has_key():
        facts = fmp.diluted_eps_facts(ticker)
        source = "FMP"
    if facts.empty:
        return {"source": None, "pe": pd.Series(dtype=float), "stats": pd.DataFrame(),
                "current": current_pe}

    if close is None:
        close = fetch_history(ticker, period="5y")["Close"]
    try:
        splits = yf.Ticker(ticker).splits
    except Exception:
        splits = pd.Series(dtype=float)
    factors = split_factors(splits, list(facts["end"]))
    quarters = discrete_quarters(facts, factors)  # split-adjust, then Q4 = FY − 3Q
    ttm = reconstruct_ttm_eps(quarters)
    pe = pe_series(close, ttm)
    stats = window_stats(pe, current=current_pe)
    current = current_pe if current_pe is not None else (
        float(pe.iloc[-1]) if not pe.empty else None
    )
    return {"source": source, "pe": pe, "stats": stats, "current": current}
