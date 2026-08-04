"""Forward valuation: DCF, reverse-DCF, and relative (multiple) fair value.

TIKR's headline trick — "value any stock in under a minute, no spreadsheet":
project free cash flow, discount it, read off an implied per-share fair value
and expected return. The whole output is **derived**, only as trustworthy as
its assumptions, so every result reports `terminal_weight` — how much of the
value leans on the terminal value (the shakiest input). Pair it with explicit
bull / base / bear scenarios rather than a single point estimate.

The reverse-DCF (`implied_growth`) is the most useful piece for a growth-tilted
book: instead of asking "what is it worth", it asks "what FCF growth does
today's price already assume", so you can judge whether the market's baked-in
expectation is beatable.

Pure functions over plain floats — no network — so tests run offline. Growth,
discount rate, and terminal growth are the analyst's to own; a consensus
starting point comes from stocks.data.estimates. Network assembly lives in
`gather` at the bottom, kept off the pure path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class DcfInputs:
    """Everything the DCF needs except the growth assumption (passed per call).

    `fcf0` is the latest annual free cash flow (equity-FCF proxy); `net_cash`
    is cash minus total debt (negative = net debt) added back to equity value.
    """

    fcf0: float
    shares: float
    net_cash: float = 0.0
    discount_rate: float = 0.10
    terminal_growth: float = 0.025
    years: int = 5


@dataclass(frozen=True)
class DcfResult:
    fair_value: float  # per share
    equity_value: float
    pv_explicit: float
    pv_terminal: float
    terminal_weight: float  # pv_terminal / (pv_explicit + pv_terminal)
    fcf_path: tuple[float, ...]


def project_fcf(fcf0: float, growth: float | Sequence[float], years: int) -> list[float]:
    """FCF for each explicit year 1..years.

    `growth` is a scalar (held constant) or a per-year sequence of length
    `years` — use `fade_growth` to taper a high starting rate toward terminal.
    """
    if isinstance(growth, (int, float)):
        rates = [float(growth)] * years
    else:
        rates = [float(g) for g in growth]
        if len(rates) != years:
            raise ValueError(f"growth sequence has {len(rates)} rates, need {years}")
    path, fcf = [], fcf0
    for g in rates:
        fcf *= 1 + g
        path.append(fcf)
    return path


def fade_growth(start: float, end: float, years: int) -> list[float]:
    """Linearly fade the annual growth rate from `start` (yr 1) to `end` (yr N)."""
    if years <= 0:
        return []
    if years == 1:
        return [float(end)]
    step = (end - start) / (years - 1)
    return [start + step * i for i in range(years)]


def _validate(inputs: DcfInputs) -> None:
    if inputs.shares <= 0:
        raise ValueError("shares must be positive")
    if inputs.years <= 0:
        raise ValueError("years must be positive")


def _assemble(
    path: list[float], r: float, tv: float, years: int, net_cash: float, shares: float
) -> DcfResult:
    pv_explicit = sum(f / (1 + r) ** (i + 1) for i, f in enumerate(path))
    pv_terminal = tv / (1 + r) ** years
    equity = pv_explicit + pv_terminal + net_cash
    denom = pv_explicit + pv_terminal
    return DcfResult(
        fair_value=equity / shares,
        equity_value=equity,
        pv_explicit=pv_explicit,
        pv_terminal=pv_terminal,
        terminal_weight=pv_terminal / denom if denom else 0.0,
        fcf_path=tuple(path),
    )


def dcf_value(inputs: DcfInputs, growth: float | Sequence[float]) -> DcfResult:
    """Per-share fair value with a Gordon-growth terminal (perpetuity)."""
    _validate(inputs)
    r, g_term = inputs.discount_rate, inputs.terminal_growth
    if r <= g_term:
        raise ValueError("discount_rate must exceed terminal_growth (Gordon terminal)")
    path = project_fcf(inputs.fcf0, growth, inputs.years)
    tv = path[-1] * (1 + g_term) / (r - g_term)
    return _assemble(path, r, tv, inputs.years, inputs.net_cash, inputs.shares)


def dcf_value_exit_multiple(
    inputs: DcfInputs, growth: float | Sequence[float], exit_multiple: float
) -> DcfResult:
    """Per-share fair value with a relative terminal: final-year FCF x multiple."""
    _validate(inputs)
    if exit_multiple <= 0:
        raise ValueError("exit_multiple must be positive")
    path = project_fcf(inputs.fcf0, growth, inputs.years)
    tv = path[-1] * exit_multiple
    return _assemble(
        path, inputs.discount_rate, tv, inputs.years, inputs.net_cash, inputs.shares
    )


def implied_growth(
    price: float, inputs: DcfInputs, lo: float = -0.5, hi: float = 1.0, tol: float = 1e-6
) -> float | None:
    """Constant FCF growth rate today's price implies (reverse-DCF, bisection).

    Returns g such that `dcf_value(inputs, g).fair_value == price`, or None when
    price falls outside the [lo, hi] growth bracket. Assumes fcf0 > 0 (fair
    value is then monotonic increasing in growth); with negative fcf0 the sign
    check simply returns None rather than a misleading root.
    """
    if price is None or price <= 0 or inputs.fcf0 <= 0:
        return None

    def f(g: float) -> float:
        return dcf_value(inputs, g).fair_value - price

    flo, fhi = f(lo), f(hi)
    if flo > 0 or fhi < 0:  # price below zero-floor value, or above hi-growth value
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < tol * max(1.0, price):
            return mid
        if fm < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def multiple_fair_value(
    metric_per_share: float | None, target_multiple: float | None
) -> float | None:
    """Relative valuation: per-share metric (e.g. fwd EPS) x target multiple (P/E)."""
    if metric_per_share is None or target_multiple is None:
        return None
    return metric_per_share * target_multiple


def expected_return(
    price: float | None, fair_value: float | None, years: float = 1.0
) -> dict[str, float | None]:
    """Total and annualized return from price to fair value over `years`."""
    if not price or price <= 0 or fair_value is None:
        return {"total": None, "annualized": None}
    total = fair_value / price - 1
    can_ann = years > 0 and fair_value > 0
    ann = (fair_value / price) ** (1 / years) - 1 if can_ann else None
    return {"total": total, "annualized": ann}


@dataclass(frozen=True)
class ValuationScenario:
    name: str
    prob: float  # 0..1
    fair_value: float
    years: float = 5.0


def blend(
    price: float, scenarios: list[ValuationScenario]
) -> tuple[list[dict], dict]:
    """Per-scenario and probability-weighted fair value + return.

    Returns (rows, weighted) where weighted has fair_value / total / annualized.
    Raises if probabilities do not sum to ~1 — scenarios must be exhaustive.
    """
    total_prob = sum(s.prob for s in scenarios)
    if scenarios and abs(total_prob - 1.0) > 1e-6:
        raise ValueError(f"scenario probabilities sum to {total_prob:.3f}, expected 1.0")
    rows = []
    for s in scenarios:
        er = expected_return(price, s.fair_value, s.years)
        rows.append(
            {
                "name": s.name,
                "prob": s.prob,
                "fair_value": s.fair_value,
                "years": s.years,
                "total": er["total"],
                "annualized": er["annualized"],
            }
        )
    weighted = {
        "fair_value": sum(s.prob * s.fair_value for s in scenarios),
        "total": sum(r["prob"] * (r["total"] or 0) for r in rows),
        "annualized": sum(r["prob"] * (r["annualized"] or 0) for r in rows),
    }
    return rows, weighted


def scenario_growths(base_growth: float, spread: float = 0.05) -> dict[str, float]:
    """The bear/base/bull growth cases implied by a base growth ± spread."""
    return {
        "bear": base_growth - spread,
        "base": base_growth,
        "bull": base_growth + spread,
    }


def scenario_values(
    inputs: DcfInputs,
    base_growth: float,
    spread: float = 0.05,
    exit_multiple: float | None = None,
) -> dict[str, DcfResult]:
    """Bear / base / bull DCF results from a base growth ± spread.

    Pass `exit_multiple` to use a relative terminal instead of Gordon growth.
    """
    out = {}
    for name, g in scenario_growths(base_growth, spread).items():
        out[name] = (
            dcf_value_exit_multiple(inputs, g, exit_multiple)
            if exit_multiple is not None
            else dcf_value(inputs, g)
        )
    return out


DEFAULT_SCENARIO_PROBS = {"bear": 0.25, "base": 0.50, "bull": 0.25}


def summarize(
    inputs: DcfInputs,
    price: float | None,
    base_growth: float,
    spread: float = 0.05,
    exit_multiple: float | None = None,
    probs: dict[str, float] = DEFAULT_SCENARIO_PROBS,
) -> dict:
    """One struct with everything the CLI and the web page render.

    Keys: results (name -> DcfResult), cases (name -> growth), probs, and —
    only when a price is available — weighted (blend dict), mos (margin of
    safety vs base fair value), implied (reverse-DCF growth).
    """
    results = scenario_values(
        inputs, base_growth, spread=spread, exit_multiple=exit_multiple
    )
    out: dict = {
        "results": results,
        "cases": scenario_growths(base_growth, spread),
        "probs": dict(probs),
        "weighted": None,
        "mos": None,
        "implied": None,
    }
    if price:
        scenarios = [
            ValuationScenario(n, probs[n], results[n].fair_value, float(inputs.years))
            for n in ("bear", "base", "bull")
        ]
        _, out["weighted"] = blend(price, scenarios)
        base_fv = results["base"].fair_value
        out["mos"] = (base_fv - price) / base_fv if base_fv else None
        out["implied"] = implied_growth(price, inputs)
    return out


def gather(
    ticker: str,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.025,
    years: int = 5,
) -> dict:
    """Assemble DcfInputs + consensus for one ticker (network). Off the pure path.

    Returns inputs=None when FCF or share count is unavailable (e.g. a company
    with negative/missing free cash flow) rather than fabricating a value.
    """
    from stocks.analysis.fundamentals import compute_metrics
    from stocks.data.estimates import consensus, fetch_estimates
    from stocks.data.fundamentals import fetch_fundamentals

    raw = fetch_fundamentals(ticker)
    metrics = compute_metrics(raw)
    info = raw.info

    fcf0 = metrics.get("fcf")
    shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    cash, debt = info.get("totalCash"), info.get("totalDebt")
    net_cash = (cash - debt) if cash is not None and debt is not None else 0.0

    try:
        cons = consensus(fetch_estimates(ticker))
    except Exception:  # no coverage / network down — degrade, never fabricate
        cons = None

    # Consensus next-FY EPS growth is the preferred starting point; fall back to
    # the realized 5y revenue CAGR, then leave None for the caller to supply.
    base_growth = None
    if cons and cons.eps_growth_next_fy is not None:
        base_growth = cons.eps_growth_next_fy
    elif metrics.get("revenue_cagr") is not None:
        base_growth = metrics["revenue_cagr"]

    inputs = None
    if fcf0 and shares:
        inputs = DcfInputs(
            fcf0=float(fcf0),
            shares=float(shares),
            net_cash=float(net_cash),
            discount_rate=discount_rate,
            terminal_growth=terminal_growth,
            years=years,
        )

    return {
        "ticker": ticker.upper(),
        "metrics": metrics,
        "consensus": cons,
        "inputs": inputs,
        "base_growth": base_growth,
        "price": metrics.get("price"),
    }
