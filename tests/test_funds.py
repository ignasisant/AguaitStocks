"""ETF / traded-fund support: classification, unit normalisation, look-through.

Offline: the classifier's cache is redirected to tmp_path and every yfinance
call is a fake Ticker built from fixture payloads shaped like the real ones
(including the two Yahoo quirks the module exists to absorb — a percent
expense ratio in `info` and a fractional one in `fundOperations`).
"""

import json

import pandas as pd
import pytest

from stocks.analysis.portfolio import allocation
from stocks.data import funds


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the learned-type cache at tmp_path and drop the in-process copy."""
    monkeypatch.setattr(funds, "TYPE_CACHE", tmp_path / "quote_types.json")
    monkeypatch.setattr(funds, "_types", None)
    yield
    monkeypatch.setattr(funds, "_types", None)


def ops_frame(expense=0.000945, turnover=0.03) -> pd.DataFrame:
    """`funds_data.fund_operations` shape: fund column, category column."""
    return pd.DataFrame(
        {"FUND": [expense, turnover, 496384.34], "Category Average": [0.007, 0.94, 1.0]},
        index=pd.Index(
            [
                "Annual Report Expense Ratio",
                "Annual Holdings Turnover",
                "Total Net Assets",
            ],
            name="Attributes",
        ),
    )


def holdings_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Name": ["Apple Inc", "NVIDIA Corp", "Broken Row"],
            "Holding Percent": [0.0704, 0.0755, None],
        },
        index=pd.Index(["AAPL", "NVDA", "OOPS"], name="Symbol"),
    )


class FakeFundsData:
    def __init__(self, **kw):
        self.fund_operations = kw.get("ops", ops_frame())
        self.top_holdings = kw.get("holdings", holdings_frame())
        self.sector_weightings = kw.get(
            "sectors", {"technology": 0.4, "realestate": 0.02, "energy": 0.0}
        )
        self.asset_classes = kw.get(
            "asset_classes", {"stockPosition": 0.999, "bondPosition": 0.0}
        )
        self.bond_holdings = kw.get("bonds")
        self.fund_overview = kw.get(
            "overview", {"categoryName": "Large Blend", "family": "State Street"}
        )
        self.description = kw.get("description", "Tracks the S&P 500.")


class FakeTicker:
    """Stands in for yf.Ticker; `payloads` maps symbol -> (info, funds_data)."""

    payloads: dict = {}
    calls: list = []

    def __init__(self, symbol):
        self.symbol = symbol
        FakeTicker.calls.append(symbol)
        self._info, self._funds = FakeTicker.payloads.get(symbol, ({}, None))

    @property
    def info(self):
        return self._info

    @property
    def funds_data(self):
        if self._funds is None:
            raise RuntimeError(f"{self.symbol}: No Fund data found.")
        return self._funds


@pytest.fixture
def fake_yf(monkeypatch):
    import yfinance as yf

    FakeTicker.payloads = {}
    FakeTicker.calls = []
    monkeypatch.setattr(yf, "Ticker", FakeTicker)
    return FakeTicker


# --------------------------------------------------------------- classification


def test_catalog_classifies_offline():
    # No network, no cache file: the catalog alone answers for a known fund.
    assert funds.is_fund("SPY", fetch=False)
    assert funds.is_fund("iwda.as", fetch=False)
    assert not funds.is_fund("AAPL", fetch=False)


def test_unknown_symbol_is_not_a_fund_without_fetch():
    assert funds.quote_type("WEIRD.XX", fetch=False) is None
    assert not funds.is_fund("WEIRD.XX", fetch=False)


def test_quote_type_fetches_once_then_reads_the_cache(fake_yf):
    fake_yf.payloads = {"NEWETF": ({"quoteType": "ETF"}, FakeFundsData())}

    assert funds.quote_type("NEWETF") == "ETF"
    assert funds.quote_type("NEWETF") == "ETF"
    assert fake_yf.calls == ["NEWETF"], "second call should hit the cache"
    assert funds.is_fund("NEWETF", fetch=False)


def test_learned_types_persist_to_disk(tmp_path, monkeypatch):
    funds.remember("VFIAX", "mutualfund")
    stored = json.loads((tmp_path / "quote_types.json").read_text())
    assert stored == {"VFIAX": "MUTUALFUND"}

    # A fresh process reads it back and needs no lookup.
    monkeypatch.setattr(funds, "_types", None)
    assert funds.is_fund("VFIAX", fetch=False)


def test_remember_ignores_blanks_and_survives_a_readonly_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(funds, "TYPE_CACHE", tmp_path / "nope" / "types.json")
    funds.remember("AAPL", None)
    funds.remember("", "ETF")
    funds.remember("AAPL", "EQUITY")  # unwritable path must not raise
    assert funds.quote_type("AAPL", fetch=False) == "EQUITY"


def test_failed_lookup_is_not_cached(fake_yf):
    fake_yf.payloads = {}  # empty info, as a dead symbol returns
    assert funds.quote_type("GHOST") is None
    assert funds.quote_type("GHOST", fetch=False) is None


def test_index_is_never_treated_as_a_fund(fake_yf):
    fake_yf.payloads = {"^GSPC": ({"quoteType": "INDEX"}, None)}
    assert funds.quote_type("^GSPC") == "INDEX"
    assert not funds.is_fund("^GSPC")


# ---------------------------------------------------------------------- search


def test_search_ranks_symbol_matches_before_names():
    hits = funds.search_funds("VT", limit=4)
    assert hits[0][0] == "VT"
    assert all(isinstance(name, str) and name for _, name in hits)


def test_search_matches_names_and_respects_the_limit():
    hits = funds.search_funds("emerging", limit=2)
    assert len(hits) == 2
    assert all("EM" in name or "Emerging" in name for _, name in hits)


def test_search_falls_back_to_fuzzy_for_a_typo():
    hits = [s for s, _ in funds.search_funds("nasdq")]
    assert any(s in ("QQQM", "SXRV.DE", "EQQQ.L", "EQQQ.DE") for s in hits)


def test_search_empty_query_is_empty():
    assert funds.search_funds("") == []


def test_fund_name_is_the_catalog_name():
    assert funds.fund_name("vwce.de") == "Vanguard FTSE All-World UCITS ETF (Acc)"
    assert funds.fund_name("AAPL") is None


# ------------------------------------------------------------------- profile


def test_profile_normalises_yahoo_units(fake_yf):
    fake_yf.payloads = {
        "SPY": (
            {
                "quoteType": "ETF",
                "longName": "SPDR S&P 500 ETF Trust",
                "currency": "USD",
                "totalAssets": 795_306_885_120,
                "yield": 0.0101,
                "netExpenseRatio": 0.0945,  # percent, and the ops row wins
            },
            FakeFundsData(),
        )
    }
    p = funds.fetch_profile("SPY")

    assert p is not None
    assert p.quote_type == "ETF"
    assert p.expense_ratio == pytest.approx(0.000945)  # fraction, from fundOperations
    assert p.turnover == pytest.approx(0.03)
    assert p.aum == 795_306_885_120
    assert p.dividend_yield == pytest.approx(0.0101)
    assert p.category == "Large Blend"
    # Zero-weight buckets drop; Yahoo's "realestate" spelling gets the same
    # label a stock's info["sector"] would carry.
    assert p.sectors == (("Technology", 0.4), ("Real Estate", 0.02))
    assert p.asset_classes == (("Equity", 0.999),)


def test_profile_reads_the_percent_expense_ratio_when_ops_is_blank(fake_yf):
    fake_yf.payloads = {
        "IWDA.AS": (
            {"quoteType": "ETF", "netExpenseRatio": 0.2, "currency": "EUR"},
            FakeFundsData(ops=ops_frame(expense=0.0)),
        )
    }
    p = funds.fetch_profile("IWDA.AS")
    assert p.expense_ratio == pytest.approx(0.002)  # 0.20%, as a fraction
    assert p.aum is None  # Yahoo publishes none for UCITS listings


def test_profile_holdings_are_sorted_and_malformed_rows_drop(fake_yf):
    fake_yf.payloads = {"SPY": ({"quoteType": "ETF"}, FakeFundsData())}
    p = funds.fetch_profile("SPY")

    assert [h.symbol for h in p.holdings] == ["NVDA", "AAPL"]
    assert p.top_weight == pytest.approx(0.0755)
    assert p.disclosed_weight == pytest.approx(0.1459)


def test_profile_is_none_for_a_stock(fake_yf):
    fake_yf.payloads = {"AAPL": ({"quoteType": "EQUITY"}, None)}
    assert funds.fetch_profile("AAPL") is None
    # ...and the kind is remembered, so no one fetches it again.
    assert funds.quote_type("AAPL", fetch=False) == "EQUITY"


def test_profile_survives_missing_fund_data(fake_yf):
    """funds_data raises for some listings; the costs half must still render."""
    fake_yf.payloads = {
        "ODD.MI": ({"quoteType": "ETF", "netExpenseRatio": 0.1}, None)
    }
    p = funds.fetch_profile("ODD.MI")

    assert p is not None
    assert p.expense_ratio == pytest.approx(0.001)
    assert p.holdings == () and p.sectors == ()
    assert p.disclosed_weight == 0.0
    assert p.top_weight is None


def test_bond_fund_carries_duration_not_a_basket(fake_yf):
    bonds = pd.DataFrame(
        {"FUND": [3.23, 9.35, None], "Category Average": [None, None, None]},
        index=pd.Index(["Duration", "Maturity", "Credit Quality"], name="Average"),
    )
    fake_yf.payloads = {
        "AGGH.MI": (
            {"quoteType": "ETF", "netExpenseRatio": 0.1},
            FakeFundsData(
                holdings=pd.DataFrame(),
                sectors={},
                asset_classes={"bondPosition": 0.994, "cashPosition": 0.006},
                bonds=bonds,
            ),
        )
    }
    p = funds.fetch_profile("AGGH.MI")

    assert p.is_bond_fund
    assert p.bond_duration == pytest.approx(3.23)
    assert p.bond_maturity == pytest.approx(9.35)
    assert p.sectors == ()


def test_profile_accepts_preloaded_info_without_a_second_lookup(fake_yf):
    fake_yf.payloads = {"SPY": ({"quoteType": "ETF"}, FakeFundsData())}
    p = funds.fetch_profile("SPY", info={"quoteType": "ETF", "currency": "USD"})

    assert p.currency == "USD"
    assert fake_yf.calls == ["SPY"]  # funds_data only


# -------------------------------------------------------------- look-through


def test_sector_split_normalises_to_one():
    profile = funds.FundProfile(
        ticker="X", name="X", quote_type="ETF",
        sectors=(("Technology", 0.30), ("Energy", 0.10)),
    )
    split = funds.sector_split(profile)
    assert sum(split.values()) == pytest.approx(1.0)
    assert split["Technology"] == pytest.approx(0.75)


def test_sector_split_is_empty_without_weights():
    assert funds.sector_split(None) == {}
    assert funds.sector_split(
        funds.FundProfile(ticker="X", name="X", quote_type="ETF")
    ) == {}


def test_allocation_looks_through_a_fund():
    weights = {"NVDA": 0.4, "SPY": 0.6}
    meta = {
        "NVDA": {"sector": "Technology"},
        "SPY": {
            "sector": "Funds",
            "sector_weights": {"Technology": 0.5, "Energy": 0.5},
        },
    }
    alloc = allocation(weights, meta, "sector")

    assert alloc["Technology"] == pytest.approx(0.7)
    assert alloc["Energy"] == pytest.approx(0.3)
    assert "Funds" not in alloc  # the label is only the no-weights fallback
    assert alloc.sum() == pytest.approx(1.0)


def test_allocation_leaves_an_undisclosed_remainder_unknown():
    alloc = allocation(
        {"BND": 1.0},
        {"BND": {"sector": "Funds", "sector_weights": {"Technology": 0.6}}},
        "sector",
    )
    assert alloc["Technology"] == pytest.approx(0.6)
    assert alloc["Unknown"] == pytest.approx(0.4)


def test_allocation_without_a_split_is_unchanged():
    alloc = allocation(
        {"NVDA": 0.5, "BTC-EUR": 0.5},
        {"NVDA": {"sector": "Technology"}, "BTC-EUR": {"sector": "Crypto"}},
        "sector",
    )
    assert dict(alloc.round(4)) == {"Technology": 0.5, "Crypto": 0.5}
