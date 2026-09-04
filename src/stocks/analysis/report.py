"""Analysis report scaffold: the mandatory 7-section structure as Markdown.

The toolkit fills the *quantitative* sections it can verify — fundamentals,
current multiples, technicals — with every number tagged fact / consensus /
derived (see stocks.analysis.fundamentals.KPI_SOURCES). The *judgment*
sections (thesis, moat, management, valuation scenarios, risks, catalysts,
conclusion) are emitted as prompts to complete, never as invented prose or
fabricated targets: web search is mandatory for prices/ratios/news and an
honest "no clear edge" is a valid conclusion.

`render_report` is pure over already-fetched data so tests run offline; the
network gathering lives in `gather` and the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stocks.analysis.fundamentals import (
    KPI_SOURCES,
    comparables_table,
    format_value,
    sources_table,
)
from stocks.analysis.indicators import rsi
from stocks.formatting import pct

DISCLAIMER = (
    "Not investment advice and not produced by a registered investment "
    "advisor. Auto-generated scaffold: verify every consensus/derived number "
    "against a primary source before acting. Past performance does not "
    "guarantee future results."
)

# Reliability level per KPI, for inline labelling in prose.
_LEVELS = {k: s.level for k, s in KPI_SOURCES.items()}

# KPI keys grouped for the fundamentals narrative.
_GROWTH_KEYS = ["revenue_cagr", "net_income_cagr", "fcf_cagr", "share_dilution"]
_QUALITY_KEYS = [
    "roe",
    "roic",
    "gross_margin",
    "op_margin",
    "net_margin",
    "fcf_yield",
    "net_debt_ebitda",
    "cash_conversion",
]


# --------------------------------------------------------------------------- #
# Technicals — derived from a price history frame (Close column).
# --------------------------------------------------------------------------- #
def technical_snapshot(df: pd.DataFrame, price_col: str = "Close") -> dict:
    """Trend / S-R / momentum snapshot from OHLCV history. {} if no usable data.

    Everything here is *derived* from price and should be read as a mechanical
    starting point for the technicals section, not a signal to act on.
    """
    if df is None or df.empty or price_col not in df.columns:
        return {}
    close = df[price_col].dropna()
    if close.empty:
        return {}
    last = float(close.iloc[-1])
    out: dict[str, float | str | None] = {"price": last}
    smas: dict[int, float | None] = {}
    for w in (20, 50, 200):
        sma = close.rolling(w).mean().iloc[-1] if len(close) >= w else None
        smas[w] = float(sma) if sma is not None else None
        out[f"sma{w}"] = smas[w]
    out["rsi14"] = float(rsi(close, 14).iloc[-1]) if len(close) > 14 else None

    win = close.tail(252)  # ~1 trading year
    hi, lo = float(win.max()), float(win.min())
    out["high_52w"], out["low_52w"] = hi, lo
    out["pct_from_high"] = last / hi - 1 if hi else None
    out["pct_from_low"] = last / lo - 1 if lo else None

    recent = close.tail(60)
    out["support"] = float(recent.min())
    out["resistance"] = float(recent.max())
    out["trend"] = _trend_label(last, smas[50], smas[200])
    return out


def _trend_label(price: float, sma50: float | None, sma200: float | None) -> str:
    if sma50 is None or sma200 is None:
        return "insufficient history"
    if price > sma50 > sma200:
        return "uptrend (price > SMA50 > SMA200)"
    if price < sma50 < sma200:
        return "downtrend (price < SMA50 < SMA200)"
    return "range / mixed"


def staggered_entries(tech: dict) -> list[tuple[str, float]]:
    """Three mechanical entry tranches from the technical snapshot.

    Suggestions to *refine*, not advice: market, pullback-to-support, and a
    deeper capitulation level. Empty if the snapshot lacks a price.
    """
    price = tech.get("price")
    if not price:
        return []
    support = tech.get("support") or price
    low = tech.get("low_52w") or support
    tranches = [
        ("market", float(price)),
        ("pullback to recent support", float(support)),
        ("deep / near 52w low", float(min(support, low))),
    ]
    # De-duplicate collapsed levels while preserving order.
    seen: set[float] = set()
    out = []
    for label, lvl in tranches:
        r = round(lvl, 2)
        if r not in seen:
            seen.add(r)
            out.append((label, lvl))
    return out


def technical_stop(tech: dict, buffer: float = 0.05) -> float | None:
    """Stop a `buffer` fraction below recent support (derived, refine by hand)."""
    support = tech.get("support")
    return support * (1 - buffer) if support else None


# --------------------------------------------------------------------------- #
# Valuation — scenario arithmetic (pure). The report emits a blank template;
# this helper is for programmatic use once the analyst supplies scenarios.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    name: str
    prob: float  # 0..1
    target: float  # fair-value price
    years: float = 1.0


def scenario_table(
    price: float, scenarios: list[Scenario]
) -> tuple[list[dict], float, float]:
    """Per-scenario and probability-weighted returns.

    Returns (rows, pw_total_return, pw_annualized). Raises if probabilities do
    not sum to ~1 — the framework wants explicit, exhaustive scenarios. The
    arithmetic is stocks.analysis.valuation.blend; this adapts the row shape.
    """
    from stocks.analysis.valuation import ValuationScenario, blend

    raw_rows, weighted = blend(
        price,
        [ValuationScenario(s.name, s.prob, s.target, s.years) for s in scenarios],
    )
    rows = [
        {
            "name": r["name"],
            "prob": r["prob"],
            "target": r["fair_value"],
            "years": r["years"],
            "total_return": r["total"],
            "annualized": r["annualized"],
        }
        for r in raw_rows
    ]
    return rows, weighted["total"], weighted["annualized"]


# --------------------------------------------------------------------------- #
# Markdown rendering (pure).
# --------------------------------------------------------------------------- #
def _money(x: float | None) -> str:
    return format_value("market_cap", x) if x is not None else "n/a"


def _labelled(key: str, metrics: dict) -> str:
    """`Label: value [level]` line for one KPI."""
    src = KPI_SOURCES[key]
    return f"- **{src.label}:** {format_value(key, metrics.get(key))} _[{src.level}]_"


def _section_thesis(ticker: str) -> list[str]:
    return [
        "## 1. Thesis — why NOW",
        "",
        "> _Complete: 3–5 lines. What makes "
        f"{ticker} actionable *today* rather than a year ago or a year from "
        "now? An honest \"no clear edge\" is a valid conclusion._",
        "",
        "- Catalyst / mispricing that is live right now:",
        "- What the market is getting wrong (or your variant perception):",
        "- Why the risk/reward is asymmetric here:",
        "",
    ]


def _section_fundamentals(metrics: dict, peers: list[dict], edgar: dict) -> list[str]:
    lines = ["## 2. Fundamentals", ""]

    lines += ["### Snapshot & comps _(fact / consensus / derived)_", ""]
    table = comparables_table([metrics, *peers])
    lines += _md_table(table)
    lines += [
        "",
        "_Levels — **fact:** filed/primary or exchange quote; **consensus:** "
        "analyst aggregate (cross-check on Koyfin/TIKR); **derived:** computed "
        "here from statements._",
        "",
    ]

    lines += ["### SEC EDGAR cross-check — latest 10-K facts _[fact]_", ""]
    rev, ni = edgar.get("revenue"), edgar.get("net_income")
    if rev is None and ni is None:
        lines += [
            "- Not found on EDGAR (non-US filer, or EDGAR_USER_AGENT unset). "
            "Anchor fundamentals against the primary regulator filing instead "
            "(e.g. annual report / 20-F).",
        ]
    else:
        if rev:
            lines.append(f"- Revenue: {_money(rev[1])} (FY end {rev[0]})")
        if ni:
            lines.append(f"- Net income: {_money(ni[1])} (FY end {ni[0]})")
        lines.append(
            f"- Reconcile against yfinance-loaded net margin "
            f"({format_value('net_margin', metrics.get('net_margin'))}) — flag "
            "any material gap before trusting derived KPIs."
        )
    lines.append("")

    lines += ["### Growth — 5y from statements _[derived]_", ""]
    lines += [_labelled(k, metrics) for k in _GROWTH_KEYS]
    lines += [
        "",
        "_Positive diluted-share CAGR = SBC dilution; negative = net buybacks. "
        "Judge growth net of dilution._",
        "",
    ]

    lines += ["### Quality & balance sheet _[fact / derived]_", ""]
    lines += [_labelled(k, metrics) for k in _QUALITY_KEYS]
    lines += [""]

    lines += [
        "### Moat & management _(complete — quantitative evidence required)_",
        "",
        "- Moat type (network / switching cost / cost / intangibles) + the "
        "metric that proves it (e.g. sustained ROIC spread over WACC):",
        "- Durability / what could erode it:",
        "- Capital allocation track record (buybacks at what multiples, M&A "
        "returns, insider ownership):",
        "- Management incentives & candour (read the last earnings call):",
        "",
    ]
    return lines


def _section_valuation(metrics: dict, tech: dict) -> list[str]:
    price = metrics.get("price") or tech.get("price")
    lines = ["## 3. Valuation", "", "### Current multiples", ""]
    mult_keys = ("pe_ttm", "pe_fwd", "peg", "ev_ebitda", "ev_sales", "pb")
    lines += [_labelled(k, metrics) for k in mult_keys]
    lines += [
        "",
        "_yfinance PEG is unreliable — never act on it without a consensus "
        "cross-check._",
        "",
        "### Scenario table _(complete: probabilities must sum to 100%)_",
        "",
        "| Scenario | Prob | Fair value | Horizon (y) | Total return | Annualized |",
        "|----------|-----:|-----------:|------------:|-------------:|-----------:|",
        "| Bear     |      |            |             |              |            |",
        "| Base     |      |            |             |              |            |",
        "| Bull     |      |            |             |              |            |",
        "",
        f"- Current price _[fact]_: {format_value('price', price)}",
        "- **Probability-weighted annualized return:** _fill via "
        "`stocks.analysis.report.scenario_table(price, [...])` once targets set_",
        "- **Margin of safety** = (base fair value − price) / base fair value: _____",
        "- Method: simplified DCF or justified multiples — state assumptions "
        "(discount rate, terminal growth, exit multiple) explicitly.",
        "",
    ]
    return lines


def _section_technicals(tech: dict) -> list[str]:
    lines = ["## 4. Technicals _[derived from price history]_", ""]
    if not tech:
        lines += ["- No price history cached — run `stocks update` first.", ""]
        return lines
    lines += [
        f"- Trend: {tech.get('trend')}",
        f"- Price {format_value('price', tech.get('price'))} · "
        f"SMA20 {format_value('price', tech.get('sma20'))} · "
        f"SMA50 {format_value('price', tech.get('sma50'))} · "
        f"SMA200 {format_value('price', tech.get('sma200'))}",
        (
            f"- RSI(14): {tech['rsi14']:.0f}"
            if tech.get("rsi14") is not None
            else "- RSI(14): n/a"
        ),
        f"- 52w range: {format_value('price', tech.get('low_52w'))} – "
        f"{format_value('price', tech.get('high_52w'))} "
        f"({pct(tech.get('pct_from_high'))} from high)",
        f"- Recent support / resistance (60d): "
        f"{format_value('price', tech.get('support'))} / "
        f"{format_value('price', tech.get('resistance'))}",
        "",
        "**Staggered entry suggestions** _(mechanical — refine against volume "
        "and structure):_",
    ]
    for label, lvl in staggered_entries(tech):
        lines.append(f"- {label}: {format_value('price', lvl)}")
    stop = technical_stop(tech)
    lines += [
        f"- Technical stop (5% below support): {format_value('price', stop)}",
        "",
    ]
    return lines


def _section_risks() -> list[str]:
    return [
        "## 5. Risks & kill criteria _(complete)_",
        "",
        "- Company-specific:",
        "- Sector / competitive:",
        "- Macro (rates, FX, cycle):",
        "- **Kill criteria** — the specific, observable events that invalidate "
        "the thesis and force an exit (not vague \"if it drops\"):",
        "",
    ]


def _section_catalysts() -> list[str]:
    return [
        "## 6. Catalysts _(with dates)_",
        "",
        "- Next earnings date:",
        "- Product / regulatory / capital-return events + expected dates:",
        "- What would confirm the thesis is playing out:",
        "",
    ]


def _section_conclusion() -> list[str]:
    return [
        "## 7. Actionable conclusion _(complete)_",
        "",
        "- **Action:** buy / add / hold / avoid / trim (state it — \"hold\" "
        "when the ranking flags red is only honest if you say why):",
        "- **Position size** (% of portfolio):",
        "- **Entry form** (lump vs staggered tranches above):",
        "- **Review metrics** (what you will re-check and when):",
        "- **Portfolio-factor overlap** (does this double an existing "
        "tech/EM/duration bet?):",
        "",
    ]


def _md_table(df: pd.DataFrame) -> list[str]:
    """DataFrame -> GitHub markdown table lines (index in first column)."""
    header = ["Metric", *[str(c) for c in df.columns]]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for idx, row in df.iterrows():
        cells = [str(idx), *[str(v) for v in row]]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_report(
    *,
    ticker: str,
    name: str = "",
    metrics: dict,
    peers: list[dict] | None = None,
    edgar: dict | None = None,
    technicals: dict | None = None,
    as_of: str,
    fx: tuple[float, str] | None = None,
) -> str:
    """Full 7-section analysis scaffold as a Markdown string (pure)."""
    peers = peers or []
    edgar = edgar or {}
    technicals = technicals or {}
    title = f"{name} ({ticker})" if name else ticker

    head = [
        f"# {title} — Investment Analysis",
        "",
        f"_Generated {as_of} · toolkit scaffold following the mandatory "
        "7-section framework. Quantitative sections are auto-filled from SEC "
        "EDGAR + yfinance and tagged **fact / consensus / derived**; judgment "
        "sections are prompts to complete._",
        "",
    ]
    if fx:
        head += [f"_FX USD→EUR (ECB spot, {fx[1]}): {fx[0]:.4f}_", ""]

    body = (
        _section_thesis(ticker)
        + _section_fundamentals(metrics, peers, edgar)
        + _section_valuation(metrics, technicals)
        + _section_technicals(technicals)
        + _section_risks()
        + _section_catalysts()
        + _section_conclusion()
    )

    tail = ["## Sources & reliability", ""]
    tail += _md_table(sources_table().set_index("KPI"))
    tail += ["", "---", "", f"_{DISCLAIMER}_", ""]

    return "\n".join([*head, *body, *tail])


def gather(ticker: str, peers: list[str] | None = None, eur: bool = False) -> dict:
    """Fetch everything render_report needs (network). Kept out of the pure path."""
    from stocks.analysis.screener import fetch_metrics_many
    from stocks.data.edgar import cross_check
    from stocks.data.fetch import load_cached

    peers = peers or []
    # Main + peers fetched concurrently; order is preserved.
    rows = fetch_metrics_many([ticker, *peers])
    metrics, peer_metrics = rows[0], rows[1:]

    try:
        edgar = cross_check(ticker)
    except Exception:  # EDGAR unavailable / no UA — degrade, never fabricate
        edgar = {}

    hist = load_cached(ticker)
    tech = technical_snapshot(hist) if hist is not None else {}

    fx = None
    if eur:
        from stocks.data.fx import usd_eur

        try:
            fx = usd_eur()
        except Exception:
            fx = None

    return {
        "metrics": metrics,
        "peers": peer_metrics,
        "edgar": edgar,
        "technicals": tech,
        "fx": fx,
    }
