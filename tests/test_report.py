"""Report scaffold tests — synthetic data, no network."""

import pandas as pd
import pytest

from stocks.analysis.report import (
    DISCLAIMER,
    Scenario,
    render_report,
    scenario_table,
    staggered_entries,
    technical_snapshot,
    technical_stop,
)

from .test_fundamentals import sample_raw  # reuse the fixture builder


def _metrics() -> dict:
    from stocks.analysis.fundamentals import compute_metrics

    return compute_metrics(sample_raw())


def _price_df(n: int = 260) -> pd.DataFrame:
    # Gentle uptrend so SMA50 > SMA200 and price on top.
    close = pd.Series([100 + i * 0.5 for i in range(n)])
    return pd.DataFrame({"Close": close})


def test_technical_snapshot_uptrend():
    tech = technical_snapshot(_price_df())
    assert tech["trend"].startswith("uptrend")
    assert tech["sma200"] < tech["sma50"] < tech["price"]
    assert tech["high_52w"] >= tech["price"] >= tech["low_52w"]
    assert 0 <= tech["rsi14"] <= 100


def test_technical_snapshot_empty():
    assert technical_snapshot(pd.DataFrame()) == {}
    assert technical_snapshot(None) == {}


def test_staggered_entries_and_stop():
    tech = technical_snapshot(_price_df())
    entries = staggered_entries(tech)
    assert entries and entries[0][0] == "market"
    # Every tranche is a positive price level.
    assert all(lvl > 0 for _, lvl in entries)
    stop = technical_stop(tech)
    assert stop < tech["support"]


def test_scenario_table_prob_weighted():
    scenarios = [
        Scenario("bear", 0.3, 80.0, 1.0),
        Scenario("base", 0.5, 120.0, 1.0),
        Scenario("bull", 0.2, 160.0, 1.0),
    ]
    rows, pw_total, pw_ann = scenario_table(100.0, scenarios)
    # pw total return = .3*-.2 + .5*.2 + .2*.6 = .16
    assert abs(pw_total - 0.16) < 1e-9
    assert abs(pw_ann - 0.16) < 1e-9  # 1y horizon => annualized == total
    assert rows[1]["total_return"] == pytest.approx(0.2)


def test_scenario_table_rejects_bad_probabilities():
    with pytest.raises(ValueError):
        scenario_table(100.0, [Scenario("a", 0.4, 100.0), Scenario("b", 0.4, 120.0)])


def test_render_report_has_seven_sections_and_disclaimer():
    md = render_report(
        ticker="TEST",
        name="Test Corp",
        metrics=_metrics(),
        peers=[dict(_metrics(), ticker="PEER")],
        edgar={"revenue": ("2025-09-30", 400e9), "net_income": ("2025-09-30", 100e9)},
        technicals=technical_snapshot(_price_df()),
        as_of="2026-07-24",
        fx=(0.92, "2026-07-24"),
    )
    for n in range(1, 8):
        assert f"## {n}." in md, f"missing section {n}"
    assert DISCLAIMER in md
    assert "Test Corp (TEST)" in md
    assert "FY end 2025-09-30" in md
    assert "USD→EUR" in md
    # Reliability labels present.
    assert "_[fact]_" in md and "_[derived]_" in md


def test_render_report_degrades_without_edgar_or_technicals():
    md = render_report(
        ticker="EMPTY",
        metrics={"ticker": "EMPTY"},
        as_of="2026-07-24",
    )
    assert "Not found on EDGAR" in md
    assert "run `stocks update`" in md
    assert DISCLAIMER in md
