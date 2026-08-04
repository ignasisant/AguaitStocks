"""Fundamental KPIs: computation from raw statements + source-of-truth map.

Pure functions over RawFundamentals so tests run offline.

Three reliability levels apply to every number shown downstream:
  fact       — verifiable in a primary source (SEC EDGAR 10-K/10-Q)
  consensus  — market/analyst aggregate (forward P/E, PEG)
  derived    — computed here from statements (ROIC, FCF yield, CAGRs)
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stocks.analysis.moat import moat_score
from stocks.data.fundamentals import RawFundamentals


@dataclass(frozen=True)
class KpiSource:
    label: str
    unit: str  # "x" multiple | "pct" | "money" | "ratio"
    level: str  # fact | consensus | derived
    loader: str  # where the toolkit loads it from
    verify: str  # where to cross-check before acting on it
    note: str = ""
    desc: str = ""  # plain-language definition, surfaced as help tooltips in the UI


# Ordered map: KPI key -> how it is loaded and where it must be verified.
# Verification hierarchy: SEC EDGAR (primary) > stockanalysis.com (10y ratios)
# > macrotrends (15-20y trends) > Koyfin/TIKR (multi-company comps).
KPI_SOURCES: dict[str, KpiSource] = {
    "price": KpiSource(
        "Price",
        "money",
        "fact",
        "yfinance info.currentPrice",
        "exchange quote / TradingView",
        desc="Latest traded price per share, in the listing currency.",
    ),
    "market_cap": KpiSource(
        "Market cap",
        "money",
        "fact",
        "yfinance info.marketCap",
        "stockanalysis.com",
        desc="Share price × shares outstanding — what the market says the equity is "
        "worth.",
    ),
    "ev": KpiSource(
        "Enterprise value",
        "money",
        "derived",
        "yfinance info.enterpriseValue",
        "stockanalysis.com",
        "cap + net debt; check debt against latest 10-Q on EDGAR",
        desc="Market cap plus debt minus cash — the price of the whole business, not "
        "just the equity.",
    ),
    "pe_ttm": KpiSource(
        "P/E (TTM)",
        "x",
        "fact",
        "yfinance info.trailingPE",
        "EDGAR EPS (10-K/10-Q) + spot price",
        desc="Price / earnings per share over the trailing twelve months. Lower = "
        "cheaper per unit of current profit.",
    ),
    "pe_fwd": KpiSource(
        "P/E (fwd)",
        "x",
        "consensus",
        "yfinance info.forwardPE",
        "Koyfin/TIKR analyst consensus",
        desc="Price / next-year consensus EPS. Below the TTM P/E means analysts expect "
        "earnings to grow.",
    ),
    "peg": KpiSource(
        "PEG (TTM)",
        "x",
        "consensus",
        "yfinance info.trailingPegRatio",
        "Koyfin/TIKR consensus growth",
        "yfinance PEG is unreliable — never act on it without cross-check",
        desc="P/E divided by expected EPS growth — a growth-adjusted P/E. Under ~1 is "
        "classically 'cheap for its growth'.",
    ),
    "pb": KpiSource(
        "P/B",
        "x",
        "fact",
        "yfinance info.priceToBook",
        "EDGAR balance sheet equity",
        desc="Price / book (accounting) value per share. Most meaningful for banks and "
        "asset-heavy businesses.",
    ),
    "ev_ebitda": KpiSource(
        "EV/EBITDA",
        "x",
        "derived",
        "yfinance info.enterpriseToEbitda",
        "stockanalysis.com / TIKR",
        desc="Enterprise value / earnings before interest, tax, depreciation & "
        "amortisation — a P/E that ignores capital structure, comparable across "
        "leverage.",
    ),
    "ev_sales": KpiSource(
        "EV/Sales",
        "x",
        "derived",
        "yfinance info.enterpriseToRevenue",
        "stockanalysis.com / TIKR",
        desc="Enterprise value / revenue. The fallback multiple for high-growth or "
        "not-yet-profitable names where P/E breaks down.",
    ),
    "roe": KpiSource(
        "ROE",
        "pct",
        "fact",
        "yfinance info.returnOnEquity",
        "EDGAR NI / equity",
        desc="Return on equity: net income / shareholder equity. Profitability of the "
        "equity base — note leverage inflates it.",
    ),
    "roic": KpiSource(
        "ROIC",
        "pct",
        "derived",
        "computed: EBIT*(1-tax) / invested capital (yfinance statements)",
        "stockanalysis.com / macrotrends (definitions differ — compare method)",
        desc="Return on invested capital: after-tax operating profit / all capital "
        "employed (equity + debt). How well the whole business compounds; >15% sustained "
        "is a quality mark.",
    ),
    "gross_margin": KpiSource(
        "Gross margin",
        "pct",
        "fact",
        "yfinance info.grossMargins",
        "EDGAR income statement",
        desc="Revenue minus cost of goods sold, as % of revenue — pricing power on the "
        "product itself.",
    ),
    "op_margin": KpiSource(
        "Operating margin",
        "pct",
        "fact",
        "yfinance info.operatingMargins",
        "EDGAR income statement",
        desc="Operating profit as % of revenue — what the core business keeps after "
        "operating costs.",
    ),
    "net_margin": KpiSource(
        "Net margin",
        "pct",
        "fact",
        "yfinance info.profitMargins",
        "EDGAR income statement",
        desc="Net income as % of revenue — the bottom line after interest and tax.",
    ),
    "fcf": KpiSource(
        "FCF (last FY)",
        "money",
        "fact",
        "yfinance cashflow 'Free Cash Flow'",
        "EDGAR 10-K cash flow (CFO - capex)",
        desc="Free cash flow: operating cash flow minus capital expenditure — the cash "
        "left over to pay dividends, buy back shares or reinvest.",
    ),
    "fcf_yield": KpiSource(
        "FCF yield",
        "pct",
        "derived",
        "computed: FCF / market cap",
        "stockanalysis.com",
        desc="Free cash flow / market cap — the cash return you buy at today's price "
        "(the inverse of the FCF multiple).",
    ),
    "net_debt_ebitda": KpiSource(
        "Net debt / EBITDA",
        "x",
        "derived",
        "computed from yfinance balance + income",
        "EDGAR 10-K/10-Q debt notes",
        "negative = net cash",
        desc="Debt minus cash, divided by EBITDA — roughly the years of operating profit "
        "needed to repay the debt. <1 conservative, >3 heavy; negative = more cash than "
        "debt.",
    ),
    "cash_conversion": KpiSource(
        "Cash conversion (FCF/NI)",
        "x",
        "derived",
        "computed from yfinance statements",
        "EDGAR 10-K",
        ">1 sustained is a quality signal; <1 check accruals",
        desc="Free cash flow / net income — how much of accounting profit turns into "
        "actual cash.",
    ),
    "revenue_cagr": KpiSource(
        "Revenue CAGR (5y stmts)",
        "pct",
        "derived",
        "computed from yfinance annual income",
        "macrotrends (longer window)",
        desc="Compound annual growth rate of revenue over the last ~5 fiscal years of "
        "filed statements.",
    ),
    "net_income_cagr": KpiSource(
        "Net income CAGR (5y stmts)",
        "pct",
        "derived",
        "computed from yfinance annual income",
        "macrotrends",
        desc="Compound annual growth rate of net income over the last ~5 fiscal years.",
    ),
    "fcf_cagr": KpiSource(
        "FCF CAGR (5y stmts)",
        "pct",
        "derived",
        "computed from yfinance annual cashflow",
        "macrotrends",
        desc="Compound annual growth rate of free cash flow over the last ~5 fiscal "
        "years.",
    ),
    "share_dilution": KpiSource(
        "Diluted shares CAGR",
        "pct",
        "derived",
        "computed from yfinance 'Diluted Average Shares'",
        "EDGAR 10-K share counts",
        "positive = dilution (SBC), negative = net buybacks",
        desc="Yearly change in diluted share count. Positive = you're being diluted "
        "(usually stock comp); negative = net buybacks shrinking the share count.",
    ),
    "moat": KpiSource(
        "Moat score",
        "score",
        "derived",
        "computed: ROIC persistence, margin stability, growth, FCF, dilution "
        "(yfinance statements)",
        "your own qualitative judgement + Morningstar moat rating",
        "quant proxies only — cannot see brand, network effects or switching costs",
        desc="0-100 heuristic of how much moat *evidence* the filings show: "
        "persistent ROIC (30%), stable gross margins (25%), durable growth (15%), "
        "cash conversion (15%), share-count discipline (15%). ≥70 wide, 45-70 "
        "narrow, <45 no moat. A screen, not a verdict.",
    ),
}

METRIC_ORDER = list(KPI_SOURCES)


def cagr(start: float | None, end: float | None, years: float) -> float | None:
    """Compound annual growth rate; None when undefined (sign flips, zeros)."""
    if not start or not end or years <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def _row(df: pd.DataFrame, label: str) -> pd.Series | None:
    if df is None or df.empty or label not in df.index:
        return None
    row = df.loc[label].dropna()
    return row if not row.empty else None


def _latest(df: pd.DataFrame, label: str) -> float | None:
    """Most recent annual value (yfinance columns are newest-first)."""
    row = _row(df, label)
    return float(row.iloc[0]) if row is not None else None


def _series_cagr(df: pd.DataFrame, label: str) -> float | None:
    """CAGR across available annual columns (newest-first)."""
    row = _row(df, label)
    if row is None or len(row) < 2:
        return None
    newest, oldest = float(row.iloc[0]), float(row.iloc[-1])
    return cagr(oldest, newest, years=len(row) - 1)


def annual_financials(raw: RawFundamentals) -> pd.DataFrame:
    """Annual Revenue, Net Income, Diluted EPS for charting — oldest-first.

    Columns present only when the underlying row exists. Index is the fiscal
    year (int). Empty DataFrame when the income statement is unavailable.
    """
    income = raw.income
    if income is None or income.empty:
        return pd.DataFrame()
    eps_label = next(
        (lbl for lbl in ("Diluted EPS", "Basic EPS") if lbl in income.index), None
    )
    wanted = [("Total Revenue", "Revenue"), ("Net Income", "Net Income")]
    if eps_label:
        wanted.append((eps_label, "EPS"))
    present = [(src, dst) for src, dst in wanted if src in income.index]
    if not present:
        return pd.DataFrame()
    df = income.loc[[src for src, _ in present]].T
    df.columns = [dst for _, dst in present]
    df.index = [getattr(c, "year", c) for c in df.index]  # period-end -> fiscal year
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.iloc[::-1]  # yfinance is newest-first; charts read left-to-right


def quarterly_eps(raw: RawFundamentals) -> pd.DataFrame:
    """Quarterly Diluted (fallback Basic) EPS for charting — oldest-first.

    Index is a "YYYYQn" period label. Empty DataFrame when the quarterly
    income statement or an EPS row is unavailable.
    """
    income = raw.income_q
    if income is None or income.empty:
        return pd.DataFrame()
    eps_label = next(
        (lbl for lbl in ("Diluted EPS", "Basic EPS") if lbl in income.index), None
    )
    if eps_label is None:
        return pd.DataFrame()
    df = income.loc[[eps_label]].T
    df.columns = ["EPS"]
    df.index = [
        f"{c.year}Q{c.quarter}" if hasattr(c, "quarter") else str(c) for c in df.index
    ]
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.iloc[::-1]  # yfinance is newest-first; charts read left-to-right


def compute_metrics(raw: RawFundamentals) -> dict[str, float | str | None]:
    """All KPIs for one ticker. Missing data -> None, never invented."""
    info = raw.info
    m: dict[str, float | str | None] = {"ticker": raw.ticker}
    m["currency"] = info.get("currency")
    m["price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    m["market_cap"] = info.get("marketCap")
    m["ev"] = info.get("enterpriseValue")
    m["pe_ttm"] = info.get("trailingPE")
    m["pe_fwd"] = info.get("forwardPE")
    m["peg"] = info.get("trailingPegRatio") or info.get("pegRatio")
    m["pb"] = info.get("priceToBook")
    m["ev_ebitda"] = info.get("enterpriseToEbitda")
    m["ev_sales"] = info.get("enterpriseToRevenue")
    m["roe"] = info.get("returnOnEquity")
    m["gross_margin"] = info.get("grossMargins")
    m["op_margin"] = info.get("operatingMargins")
    m["net_margin"] = info.get("profitMargins")

    # -- derived from statements --
    ebit = _latest(raw.income, "EBIT")
    tax_rate = _latest(raw.income, "Tax Rate For Calcs")
    invested = _latest(raw.balance, "Invested Capital")
    m["roic"] = (
        ebit * (1 - tax_rate) / invested
        if ebit is not None and tax_rate is not None and invested
        else None
    )

    fcf = _latest(raw.cashflow, "Free Cash Flow")
    m["fcf"] = fcf
    m["fcf_yield"] = fcf / m["market_cap"] if fcf and m["market_cap"] else None

    ebitda = _latest(raw.income, "EBITDA")
    net_debt = _latest(raw.balance, "Net Debt")
    if net_debt is None:
        debt, cash = info.get("totalDebt"), info.get("totalCash")
        net_debt = debt - cash if debt is not None and cash is not None else None
    m["net_debt_ebitda"] = net_debt / ebitda if net_debt is not None and ebitda else None

    net_income = _latest(raw.income, "Net Income")
    m["cash_conversion"] = fcf / net_income if fcf and net_income else None

    m["revenue_cagr"] = _series_cagr(raw.income, "Total Revenue")
    m["net_income_cagr"] = _series_cagr(raw.income, "Net Income")
    m["fcf_cagr"] = _series_cagr(raw.cashflow, "Free Cash Flow")
    m["share_dilution"] = _series_cagr(raw.income, "Diluted Average Shares")
    m["moat"] = moat_score(raw).score
    return m


def format_value(key: str, value: float | str | None) -> str:
    """Human string with explicit units; 'n/a' when data is missing."""
    if value is None:
        return "n/a"
    unit = KPI_SOURCES[key].unit if key in KPI_SOURCES else "ratio"
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "x":
        return f"{value:.1f}x"
    if unit == "score":
        return f"{float(value):.0f}/100"
    if unit == "money":
        v = float(value)
        for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if abs(v) >= div:
                return f"{v / div:,.2f}{suffix}"
        return f"{v:,.2f}"
    return f"{value}"


# Rule-of-thumb interpretation bands per KPI: value-ascending
# (upper_bound_exclusive, label, streamlit-color). The color already encodes the
# verdict, so metrics where higher is better (ROIC, FCF yield) simply end green
# and multiples where lower is cheaper end red — no direction flag needed. Bands
# are rough and sector-agnostic: a fast visual cue, never the comps table.
# Percentage metrics carry fraction values (0.15 == 15%), so bands match.
_VERDICT_BANDS: dict[str, list[tuple[float, str, str]]] = {
    # valuation multiples — lower is cheaper
    "pe_ttm": [(15, "cheap", "green"), (25, "fair", "orange"),
               (40, "expensive", "red"), (float("inf"), "very expensive", "red")],
    "pe_fwd": [(15, "cheap", "green"), (25, "fair", "orange"),
               (40, "expensive", "red"), (float("inf"), "very expensive", "red")],
    "peg": [(1, "cheap", "green"), (2, "fair", "orange"),
            (float("inf"), "expensive", "red")],
    "pb": [(1.5, "cheap", "green"), (4, "fair", "orange"),
           (float("inf"), "expensive", "red")],
    "ev_ebitda": [(10, "cheap", "green"), (16, "fair", "orange"),
                  (float("inf"), "expensive", "red")],
    "ev_sales": [(3, "cheap", "green"), (8, "fair", "orange"),
                 (float("inf"), "expensive", "red")],
    # quality / returns — higher is better (fraction values)
    "roic": [(0.08, "weak", "red"), (0.15, "decent", "orange"),
             (float("inf"), "strong", "green")],
    "roe": [(0.10, "weak", "red"), (0.20, "decent", "orange"),
            (float("inf"), "strong", "green")],
    "fcf_yield": [(0.02, "low", "red"), (0.05, "decent", "orange"),
                  (float("inf"), "high", "green")],
    # leverage — lower is safer; below zero is net cash
    "net_debt_ebitda": [(0, "net cash", "green"), (1, "low", "green"),
                        (3, "moderate", "orange"), (float("inf"), "high", "red")],
    # dilution CAGR — negative = buybacks (good), positive = dilution
    "share_dilution": [(-0.005, "buybacks", "green"), (0.005, "flat", "gray"),
                       (0.02, "dilutive", "orange"),
                       (float("inf"), "heavy dilution", "red")],
    # momentum — RSI(14): <30 oversold, 30-70 neutral, >70 overbought
    "rsi": [(30, "oversold", "green"), (70, "neutral", "gray"),
            (float("inf"), "overbought", "red")],
    # moat evidence score — thresholds mirror stocks.analysis.moat
    "moat": [(45, "no moat", "red"), (70, "narrow", "orange"),
             (float("inf"), "wide", "green")],
}


def verdict(key: str, value: float | str | None) -> tuple[str, str] | None:
    """Cheap/fair/expensive read on a KPI: (label, streamlit color).

    None when there is no band for `key` or the value is missing / non-numeric.
    Booleans are rejected too (they are int subclasses but never real KPIs).
    NaN falls through every band and returns None, so callers get a blank cue
    rather than a wrong one.
    """
    bands = _VERDICT_BANDS.get(key)
    if bands is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    for upper, label, color in bands:
        if value < upper:
            return label, color
    return None


def verdict_md(key: str, value: float | str | None) -> str:
    """Colored Streamlit-markdown chip for a KPI verdict, or "" when none."""
    v = verdict(key, value)
    return f":{v[1]}[{v[0]}]" if v else ""


def comparables_table(metrics: list[dict]) -> pd.DataFrame:
    """KPIs as rows, tickers as columns — the framework's comps table."""
    cols = {}
    for m in metrics:
        cols[str(m["ticker"])] = [format_value(k, m.get(k)) for k in METRIC_ORDER]
    labels = [KPI_SOURCES[k].label for k in METRIC_ORDER]
    return pd.DataFrame(cols, index=labels)


# Comps ranking direction per KPI. Size (market cap, EV), price and raw FCF
# say nothing about who screens better, so they stay out of the score.
_RANK_LOWER_BETTER = frozenset(
    {"pe_ttm", "pe_fwd", "peg", "pb", "ev_ebitda", "ev_sales",
     "net_debt_ebitda", "share_dilution"}
)
_RANK_HIGHER_BETTER = frozenset(
    {"roe", "roic", "gross_margin", "op_margin", "net_margin", "fcf_yield",
     "cash_conversion", "revenue_cagr", "net_income_cagr", "fcf_cagr"}
)
# A ticker missing most KPIs can't win on the two it happens to have.
_RANK_MIN_METRICS = 5


def comp_scores(metrics: list[dict]) -> dict[str, float]:
    """Composite comps score per ticker in [0, 1] — cross-sectional, not banded.

    Each rankable KPI contributes a normalized average rank across the tickers
    that report it (best = 1, worst = 0, ties share); a ticker's score is the
    mean over its ranked KPIs. Tickers ranked on fewer than _RANK_MIN_METRICS
    KPIs are dropped — too sparse to compare fairly.
    """
    per_ticker: dict[str, list[float]] = {str(m["ticker"]): [] for m in metrics}
    for key in _RANK_LOWER_BETTER | _RANK_HIGHER_BETTER:
        vals = {
            str(m["ticker"]): float(m[key])
            for m in metrics
            if isinstance(m.get(key), (int, float))
            and not isinstance(m.get(key), bool)
            and pd.notna(m[key])
        }
        if len(vals) < 2:
            continue
        n = len(vals)
        for t, v in vals.items():
            beaten = sum(1 for o in vals.values() if o > v) if (
                key in _RANK_LOWER_BETTER
            ) else sum(1 for o in vals.values() if o < v)
            tied = sum(1 for o in vals.values() if o == v) - 1
            per_ticker[t].append((beaten + tied / 2) / (n - 1))
    return {
        t: sum(s) / len(s)
        for t, s in per_ticker.items()
        if len(s) >= _RANK_MIN_METRICS
    }


def comp_medals(metrics: list[dict]) -> dict[str, str]:
    """Medal emoji for the 3 best composite comps scores (skipped when fewer
    than 3 tickers qualify — a 2-horse race has no podium)."""
    scores = comp_scores(metrics)
    if len(scores) < 3:
        return {}
    ranked = sorted(scores, key=scores.get, reverse=True)
    return dict(zip(ranked, ("🥇", "🥈", "🥉")))


def sources_table() -> pd.DataFrame:
    """KPI -> load source, verification source, reliability level."""
    return pd.DataFrame(
        {
            "KPI": [s.label for s in KPI_SOURCES.values()],
            "Level": [s.level for s in KPI_SOURCES.values()],
            "Loaded from": [s.loader for s in KPI_SOURCES.values()],
            "Verify against": [s.verify for s in KPI_SOURCES.values()],
            "Note": [s.note for s in KPI_SOURCES.values()],
        }
    )
