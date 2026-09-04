"""Shared value and display helpers (pure).

KPI-table formatting with explicit units stays in
stocks.analysis.fundamentals.format_value (driven by KPI_SOURCES); these are
the small generic helpers the rest of the tree kept reinventing.
"""

from __future__ import annotations

import math


def finite(value) -> float | None:
    """`value` as a finite float, or None for anything else — NaN, ±inf, pd.NA,
    text, missing.

    Both halves matter where the result is serialized: json.dumps writes NaN
    and Infinity as bare literals that strict parsers reject, so an LLM prompt
    or a cached payload must never carry one.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def pct(x: float | None, signed: bool = False, na: str = "n/a") -> str:
    """Percent string for a fraction (0.15 -> '15.0%'); `na` when missing."""
    if x is None:
        return na
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def compact_money(v: float, symbol: str = "$") -> str:
    """Compact currency label, e.g. $394.3B / -$1.2M / $950."""
    a = abs(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{symbol}{v / div:.1f}{suffix}"
    return f"{symbol}{v:,.0f}"
