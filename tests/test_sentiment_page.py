"""The Pulse page's staged load: nine reserved slots, every one of them resolved.

The page reserves a skeleton for each card before it fetches anything and fills
each one as the source it waits on lands. That only works if every path out of
a slot ends in content or an explanation — an unresolved slot shimmers forever,
and it does so silently, which is exactly the kind of bug no exception reports.

Each scenario below kills one source and asserts two things: no skeleton
survives the run, and the page still renders all nine headings. A dead source
must cost its own card's contents and nothing else.
"""

import numpy as np
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from yfinance.exceptions import YFRateLimitError

from stocks.analysis import sentiment as sm
from stocks.data import macro

PAGE = "src/stocks/web/app_pages/sentiment.py"
SKELETON_MARKER = "topstocks-sk"
BLOCKS = 9

# Enough history for the trailing-year percentiles and the 200-session trend
# average the page asks for.
SESSIONS = 400


def _tape(seed: int) -> pd.Series:
    """A deterministic price path — a drift plus a wobble, never flat.

    Flat series would make every percentile a tie and every trend "unknown",
    which is not the shape this page is being tested against.
    """
    index = pd.bdate_range("2024-01-01", periods=SESSIONS)
    steps = np.sin(np.linspace(0, 12 + seed, SESSIONS)) / 200 + 0.0004
    return pd.Series(100 * np.cumprod(1 + steps), index=index, dtype=float)


def _fake_closes(tickers, period="1y"):
    return {t: _tape(i) for i, t in enumerate(tickers)}


def _fake_fred(sids, **_kw):
    return {sid: _tape(i) / 20 for i, sid in enumerate(sids)}


def _fake_inflation(*_a, **_kw):
    return pd.DataFrame(
        [
            {
                "area": area, "period": "2026-08", "headline": 3.0 + i * 0.2,
                "core": 2.4, "prior": 2.9, "six_months": 1.9, "momentum": 1.1,
                "path": [1.9 + j * 0.1 for j in range(14)],
            }
            for i, area in enumerate(macro.INFLATION_AREAS)
        ]
    )


@pytest.fixture
def page(monkeypatch):
    """Run the page against synthetic sources and hand back its rendered HTML.

    Every network source is stubbed by default and each test below breaks one
    of them, so the scenarios are deterministic and the suite stays offline —
    the thing under test is which slots get resolved, not what the market did.

    `st.cache_data` entries outlive a run inside one process, so the cache is
    cleared before each: without that a stubbed loader is never called and
    every scenario silently re-reads the first one's data.
    """
    monkeypatch.setattr("stocks.analysis.portfolio.load_closes", _fake_closes)
    monkeypatch.setattr("stocks.analysis.portfolio.load_meta", lambda t, **k: {})
    monkeypatch.setattr("stocks.data.macro.fred_many", _fake_fred)
    monkeypatch.setattr("stocks.data.macro.inflation", _fake_inflation)
    monkeypatch.setattr("stocks.data.funds.sector_weights", lambda t: {})

    def _run() -> tuple[AppTest, str]:
        st.cache_data.clear()
        at = AppTest.from_file(PAGE, default_timeout=120)
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
        return at, "".join(str(el.body) for el in at.get("html"))

    return _run


def test_the_fixture_covers_every_symbol_the_page_asks_for():
    """A stub that misses a registry entry would fake a partial outage."""
    assert set(_fake_closes(sm.all_tickers())) == set(sm.all_tickers())


def _rows(markup: str) -> int:
    """Trend rows in the markup, discounting the four the stylesheet names."""
    return markup.count('class="ag-trend-row"')


def test_every_slot_resolves_when_all_sources_answer(page):
    at, markup = page()
    assert SKELETON_MARKER not in markup
    assert len(at.subheader) == BLOCKS
    assert _rows(markup) > 40


def test_a_throttled_price_host_leaves_no_shimmer(page, monkeypatch):
    """Yahoo throttles datacenter IPs routinely; six cards depend on it."""
    def _throttled(*_a, **_kw):
        raise YFRateLimitError

    monkeypatch.setattr("stocks.analysis.portfolio.load_closes", _throttled)
    at, markup = page()
    assert SKELETON_MARKER not in markup
    # The page keeps its shape: every heading is still there, saying why its
    # card is empty rather than vanishing.
    assert len(at.subheader) == BLOCKS


def test_dead_fred_costs_only_the_rates_card(page, monkeypatch):
    def _dead(*_a, **_kw):
        raise OSError("fred unreachable")

    monkeypatch.setattr("stocks.data.macro.fred_many", _dead)
    at, markup = page()
    assert SKELETON_MARKER not in markup
    assert len(at.subheader) == BLOCKS
    # The price-driven blocks are untouched, so the row count stays high even
    # with the rates and inflation cards blanked.
    assert _rows(markup) > 25


def test_dead_eurostat_costs_only_the_inflation_card(page, monkeypatch):
    def _dead(*_a, **_kw):
        raise OSError("eurostat unreachable")

    monkeypatch.setattr("stocks.data.macro.inflation", _dead)
    at, markup = page()
    assert SKELETON_MARKER not in markup
    assert len(at.subheader) == BLOCKS
    assert _rows(markup) > 35


def test_an_empty_price_result_also_resolves_the_slots(page, monkeypatch):
    """The third failure path: a fetch that raises nothing and returns nothing.

    Neither except branch sees this one, and it is what a symbol list that
    Yahoo answers with empty frames produces.
    """
    monkeypatch.setattr("stocks.analysis.portfolio.load_closes", lambda *a, **k: {})
    at, markup = page()
    assert SKELETON_MARKER not in markup
    assert len(at.subheader) == BLOCKS
