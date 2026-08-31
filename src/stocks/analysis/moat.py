"""Quantitative moat proxies from filed statement history [derived].

A durable competitive advantage leaves numeric traces: returns on capital
that stay above the cost of capital, gross margins that hold under attack,
growth that persists, profits that convert to cash, a share count that
shrinks instead of bloating. This module scores those traces 0-100 from
the same yfinance statements the KPI block uses, so tests run offline.

What it cannot see: brand, network effects, switching costs, regulation —
the *causes* of a moat. Treat the score as a screen that asks "do the
numbers look like a moat exists?", never as the qualitative verdict itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stocks.data.fundamentals import RawFundamentals

# Pillar weights — returns on capital dominate (the moat's bottom line),
# margin durability second (pricing power), the rest split evenly.
PILLAR_WEIGHTS: dict[str, float] = {
    "roic": 0.30,
    "gross_margin": 0.25,
    "growth": 0.15,
    "fcf": 0.15,
    "dilution": 0.15,
}

# Composite bands: >=70 wide, >=45 narrow, below no moat.
WIDE_THRESHOLD = 70.0
NARROW_THRESHOLD = 45.0

# Fewer scored pillars than this and the composite is meaningless.
_MIN_PILLARS = 3

# ROIC above this clears any sane cost of capital.
_ROIC_HURDLE = 0.10


@dataclass(frozen=True)
class MoatPillar:
    key: str
    label: str
    score: float | None  # 0-100, None when the inputs are missing
    detail: str  # what the score was computed from, for tooltips/reports


@dataclass(frozen=True)
class MoatScore:
    ticker: str
    pillars: tuple[MoatPillar, ...]
    score: float | None  # weighted composite 0-100, None when < _MIN_PILLARS
    rating: str | None  # "wide" | "narrow" | "no moat" | None
    years: int  # annual statement years backing the score


def _row(df: pd.DataFrame, label: str) -> pd.Series | None:
    """Numeric annual series for one statement row, or None when absent."""
    if df is None or df.empty or label not in df.index:
        return None
    row = pd.to_numeric(df.loc[label], errors="coerce").dropna()
    return row if not row.empty else None


def _scale(value: float, lo: float, hi: float) -> float:
    """Linear map of value onto 0-100 between lo and hi, clamped."""
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def _series_cagr(series: pd.Series) -> float | None:
    """CAGR oldest->newest across annual columns; None when undefined."""
    if len(series) < 2:
        return None
    seq = series.sort_index()
    oldest, newest = float(seq.iloc[0]), float(seq.iloc[-1])
    if oldest <= 0 or newest <= 0:
        return None
    return (newest / oldest) ** (1 / (len(seq) - 1)) - 1


def _roic_pillar(raw: RawFundamentals) -> MoatPillar:
    """Level + persistence of ROIC: 60% median level (5%..25% -> 0..100),
    40% share of years clearing the 10% hurdle."""
    ebit = _row(raw.income, "EBIT")
    tax = _row(raw.income, "Tax Rate For Calcs")
    invested = _row(raw.balance, "Invested Capital")
    if ebit is None or tax is None or invested is None:
        return MoatPillar(
            "roic", "ROIC", None, "EBIT / tax rate / invested capital rows missing"
        )
    roic = (ebit * (1 - tax) / invested[invested > 0]).dropna()
    if roic.empty:
        return MoatPillar("roic", "ROIC", None, "no overlapping statement years")
    median = float(roic.median())
    above = int((roic >= _ROIC_HURDLE).sum())
    score = 0.6 * _scale(median, 0.05, 0.25) + 0.4 * (above / len(roic)) * 100
    detail = f"median ROIC {median:.0%}, ≥10% in {above}/{len(roic)} years"
    return MoatPillar("roic", "ROIC", score, detail)


def _margin_pillar(raw: RawFundamentals) -> MoatPillar:
    """Gross margin level (20%..60% -> 0..100) minus a stability penalty:
    each percentage point of yearly σ costs 4 points, capped at 40."""
    rev = _row(raw.income, "Total Revenue")
    gp = _row(raw.income, "Gross Profit")
    if rev is not None and gp is not None:
        gm = (gp / rev[rev > 0]).dropna()
        if not gm.empty:
            level = float(gm.median())
            std = float(gm.std()) if len(gm) >= 2 else 0.0
            score = max(0.0, _scale(level, 0.20, 0.60) - min(40.0, std * 400))
            detail = (
                f"median gross margin {level:.0%}, "
                f"σ {std * 100:.1f}pp over {len(gm)} years"
            )
            return MoatPillar("gross_margin", "Margins", score, detail)
    snapshot = raw.info.get("grossMargins")
    if isinstance(snapshot, (int, float)) and not isinstance(snapshot, bool):
        return MoatPillar(
            "gross_margin",
            "Margins",
            _scale(float(snapshot), 0.20, 0.60),
            f"gross margin {snapshot:.0%} (snapshot only — no history for stability)",
        )
    return MoatPillar("gross_margin", "Margins", None, "gross margin unavailable")


def _growth_pillar(raw: RawFundamentals) -> MoatPillar:
    """Revenue durability: 50% CAGR level (0%..15% -> 0..100), 50% share of
    up years — steady single-digit growth outscores one lucky spike."""
    rev = _row(raw.income, "Total Revenue")
    if rev is None or len(rev) < 2:
        return MoatPillar("growth", "Growth", None, "needs ≥2 revenue years")
    changes = rev.sort_index().pct_change().dropna()
    up = float((changes > 0).mean())
    growth = _series_cagr(rev)
    if growth is None:
        score = up * 100
        detail = (
            f"revenue up in {int((changes > 0).sum())}/{len(changes)} years "
            "(CAGR undefined)"
        )
    else:
        score = 0.5 * _scale(growth, 0.0, 0.15) + 0.5 * up * 100
        detail = (
            f"revenue CAGR {growth:.0%}, up in "
            f"{int((changes > 0).sum())}/{len(changes)} years"
        )
    return MoatPillar("growth", "Growth", score, detail)


def _fcf_pillar(raw: RawFundamentals) -> MoatPillar:
    """Cash generation: 60% median FCF margin (0%..20% -> 0..100), 40% share
    of FCF-positive years."""
    fcf = _row(raw.cashflow, "Free Cash Flow")
    rev = _row(raw.income, "Total Revenue")
    if fcf is None or rev is None:
        return MoatPillar("fcf", "FCF", None, "free cash flow or revenue rows missing")
    margin = (fcf / rev[rev > 0]).dropna()
    if margin.empty:
        return MoatPillar("fcf", "FCF", None, "no overlapping statement years")
    median = float(margin.median())
    positive = float((margin > 0).mean())
    score = 0.6 * _scale(median, 0.0, 0.20) + 0.4 * positive * 100
    detail = (
        f"median FCF margin {median:.0%}, positive in "
        f"{int((margin > 0).sum())}/{len(margin)} years"
    )
    return MoatPillar("fcf", "FCF", score, detail)


def _dilution_pillar(raw: RawFundamentals) -> MoatPillar:
    """Share-count discipline: -2%/y buybacks -> 100, +3%/y dilution -> 0."""
    shares = _row(raw.income, "Diluted Average Shares")
    growth = _series_cagr(shares) if shares is not None else None
    if growth is None:
        return MoatPillar("dilution", "Dilution", None, "diluted share history missing")
    return MoatPillar(
        "dilution",
        "Dilution",
        _scale(-growth, -0.03, 0.02),
        f"share count {growth:+.1%}/year",
    )


def moat_rating(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= WIDE_THRESHOLD:
        return "wide"
    if score >= NARROW_THRESHOLD:
        return "narrow"
    return "no moat"


def moat_score(raw: RawFundamentals) -> MoatScore:
    """Score one ticker's moat evidence. Missing pillars are dropped and the
    rest reweighted; fewer than _MIN_PILLARS scored -> composite None."""
    pillars = (
        _roic_pillar(raw),
        _margin_pillar(raw),
        _growth_pillar(raw),
        _fcf_pillar(raw),
        _dilution_pillar(raw),
    )
    scored = [(p.score, PILLAR_WEIGHTS[p.key]) for p in pillars if p.score is not None]
    if len(scored) >= _MIN_PILLARS:
        total_weight = sum(w for _, w in scored)
        composite = sum(s * w for s, w in scored) / total_weight
    else:
        composite = None
    rev = _row(raw.income, "Total Revenue")
    return MoatScore(
        ticker=raw.ticker,
        pillars=pillars,
        score=composite,
        rating=moat_rating(composite),
        years=len(rev) if rev is not None else 0,
    )
