"""Valuation engine tests — pure math, no network."""

import pytest

from stocks.analysis.valuation import (
    DcfInputs,
    ValuationScenario,
    blend,
    dcf_value,
    dcf_value_exit_multiple,
    expected_return,
    fade_growth,
    implied_growth,
    multiple_fair_value,
    project_fcf,
    scenario_values,
)


def _inputs(**kw) -> DcfInputs:
    base = dict(fcf0=100.0, shares=100.0, net_cash=0.0, discount_rate=0.10,
                terminal_growth=0.02, years=5)
    base.update(kw)
    return DcfInputs(**base)


def test_project_fcf_scalar_and_sequence():
    assert project_fcf(100, 0.10, 3) == pytest.approx([110, 121, 133.1])
    assert project_fcf(100, [0.1, 0.2, 0.0], 3) == pytest.approx([110, 132, 132])


def test_project_fcf_bad_sequence_length():
    with pytest.raises(ValueError):
        project_fcf(100, [0.1, 0.2], 5)


def test_fade_growth():
    assert fade_growth(0.20, 0.02, 5) == pytest.approx([0.20, 0.155, 0.11, 0.065, 0.02])
    assert fade_growth(0.1, 0.02, 1) == [0.02]
    assert fade_growth(0.1, 0.02, 0) == []


def test_dcf_value_closed_form():
    # fcf0=100, g=0, r=.10, term=.02, 5y, 100 shares, no net cash.
    res = dcf_value(_inputs(), growth=0.0)
    assert res.fair_value == pytest.approx(11.70757, abs=1e-4)
    assert res.terminal_weight == pytest.approx(0.6763, abs=1e-3)
    # explicit + terminal reconcile with equity value.
    assert res.pv_explicit + res.pv_terminal == pytest.approx(res.equity_value)


def test_dcf_value_monotonic_in_growth():
    lo = dcf_value(_inputs(), 0.02).fair_value
    hi = dcf_value(_inputs(), 0.12).fair_value
    assert hi > lo


def test_dcf_net_cash_adds_per_share():
    without = dcf_value(_inputs(net_cash=0.0), 0.05).fair_value
    with_cash = dcf_value(_inputs(net_cash=500.0), 0.05).fair_value
    assert with_cash - without == pytest.approx(5.0)  # 500 / 100 shares


def test_dcf_rejects_terminal_ge_discount():
    with pytest.raises(ValueError):
        dcf_value(_inputs(discount_rate=0.03, terminal_growth=0.05), 0.05)


def test_dcf_rejects_bad_shares_years():
    with pytest.raises(ValueError):
        dcf_value(_inputs(shares=0.0), 0.05)
    with pytest.raises(ValueError):
        dcf_value(_inputs(years=0), 0.05)


def test_exit_multiple_terminal():
    res = dcf_value_exit_multiple(_inputs(), growth=0.0, exit_multiple=15.0)
    # FCF path is flat at 100; TV = 100 * 15 = 1500 discounted 5y at 10%.
    tv_pv = 1500 / 1.1**5
    assert res.pv_terminal == pytest.approx(tv_pv)
    with pytest.raises(ValueError):
        dcf_value_exit_multiple(_inputs(), 0.0, exit_multiple=0.0)


def test_implied_growth_roundtrips():
    inp = _inputs()
    for g in (0.03, 0.08, 0.15):
        price = dcf_value(inp, g).fair_value
        assert implied_growth(price, inp) == pytest.approx(g, abs=1e-4)


def test_implied_growth_out_of_bracket_returns_none():
    inp = _inputs()
    cheap = dcf_value(inp, -0.9).fair_value  # would need g below lo=-0.5
    assert implied_growth(cheap, inp) is None
    assert implied_growth(0.0, inp) is None  # non-positive price
    assert implied_growth(50.0, _inputs(fcf0=-10.0)) is None  # negative fcf0


def test_multiple_fair_value():
    assert multiple_fair_value(9.71, 30.0) == pytest.approx(291.3)
    assert multiple_fair_value(None, 30.0) is None
    assert multiple_fair_value(9.71, None) is None


def test_expected_return():
    er = expected_return(100.0, 121.0, years=2.0)
    assert er["total"] == pytest.approx(0.21)
    assert er["annualized"] == pytest.approx(0.10)
    assert expected_return(0.0, 100.0)["total"] is None
    assert expected_return(100.0, None)["total"] is None


def test_blend_probability_weighted():
    scenarios = [
        ValuationScenario("bear", 0.25, 80.0, 1.0),
        ValuationScenario("base", 0.50, 120.0, 1.0),
        ValuationScenario("bull", 0.25, 160.0, 1.0),
    ]
    rows, weighted = blend(100.0, scenarios)
    assert weighted["fair_value"] == pytest.approx(120.0)  # .25*80+.5*120+.25*160
    # total return = .25*-.2 + .5*.2 + .25*.6 = .20
    assert weighted["total"] == pytest.approx(0.20)
    assert rows[1]["total"] == pytest.approx(0.20)


def test_blend_rejects_bad_probabilities():
    bad = [ValuationScenario("a", 0.4, 100.0), ValuationScenario("b", 0.4, 120.0)]
    with pytest.raises(ValueError):
        blend(100.0, bad)


def test_scenario_values_orders_bear_base_bull():
    out = scenario_values(_inputs(), base_growth=0.08, spread=0.04)
    assert set(out) == {"bear", "base", "bull"}
    assert out["bear"].fair_value < out["base"].fair_value < out["bull"].fair_value


def test_scenario_values_exit_multiple():
    out = scenario_values(_inputs(), base_growth=0.08, spread=0.04, exit_multiple=15.0)
    assert out["bull"].fair_value > out["bear"].fair_value
