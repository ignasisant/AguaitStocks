"""Fetch raw fundamentals (snapshot + annual statements) via yfinance.

yfinance is the *loading* source (free, no key). It is NOT the verification
source — see stocks.analysis.fundamentals.KPI_SOURCES for where each KPI
should be cross-checked (SEC EDGAR primary for US filers).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf


@dataclass
class RawFundamentals:
    """One ticker's raw fundamental data, annual statements newest-first."""

    ticker: str
    info: dict = field(default_factory=dict)
    income: pd.DataFrame = field(default_factory=pd.DataFrame)
    balance: pd.DataFrame = field(default_factory=pd.DataFrame)
    cashflow: pd.DataFrame = field(default_factory=pd.DataFrame)
    income_q: pd.DataFrame = field(default_factory=pd.DataFrame)


def fetch_fundamentals(ticker: str) -> RawFundamentals:
    """Download snapshot info + annual + quarterly statements for one ticker."""
    t = yf.Ticker(ticker)
    return RawFundamentals(
        ticker=ticker.upper(),
        info=t.info or {},
        income=t.financials,
        income_q=t.quarterly_financials,
        balance=t.balance_sheet,
        cashflow=t.cashflow,
    )
