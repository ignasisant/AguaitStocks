"""Market-pulse math — the composite, its alignment rules and the personalisation.

Everything here runs on synthetic series: the point is that a page about live
markets has arithmetic that can be pinned down without a network, especially
the alignment rules, which exist because of real-world data faults (a
half-printed session, a feed that quietly stopped) that are hard to reproduce
on demand.
"""

import numpy as np
import pandas as pd
import pytest

from stocks.analysis import sentiment as sm


def days(n: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def ramp(n: int, lo: float = 100.0, hi: float = 200.0, start: str = "2024-01-01"):
    return pd.Series(np.linspace(lo, hi, n), index=days(n, start), dtype=float)


def flat(n: int, value: float = 100.0, start: str = "2024-01-01"):
    return pd.Series([value] * n, index=days(n, start), dtype=float)


# ------------------------------------------------------------------ primitives
def test_pct_over_spans_trading_days():
    s = pd.Series([100.0, 110.0, 121.0], index=days(3))
    assert sm.pct_over(s, 1) == pytest.approx(0.10)
    assert sm.pct_over(s, 2) == pytest.approx(0.21)


def test_pct_over_too_short_is_nan():
    assert np.isnan(sm.pct_over(pd.Series([1.0, 2.0], index=days(2)), 5))


def test_ytd_anchors_on_last_close_of_prior_year():
    idx = pd.to_datetime(["2024-12-30", "2024-12-31", "2025-01-02", "2025-01-03"])
    s = pd.Series([90.0, 100.0, 105.0, 110.0], index=idx)
    # 110 / 100 - 1: measured from December's final close, so January's own
    # opening gap is inside the number.
    assert sm.ytd_return(s) == pytest.approx(0.10)


def test_ytd_without_prior_year_is_nan():
    s = pd.Series([100.0, 110.0], index=pd.to_datetime(["2025-01-02", "2025-01-03"]))
    assert np.isnan(sm.ytd_return(s))


def test_from_high_is_never_positive():
    s = pd.Series([100.0, 120.0, 90.0], index=days(3))
    assert sm.from_high(s) == pytest.approx(90 / 120 - 1)
    assert sm.from_high(ramp(50)) == pytest.approx(0.0)


def test_rank_series_scores_the_extremes():
    ranked = sm.rank_series(ramp(300), window=252, min_periods=60)
    assert ranked.iloc[-1] == pytest.approx(100.0)


def test_rank_series_inverts():
    plain = sm.rank_series(ramp(300), window=252, min_periods=60)
    flipped = sm.rank_series(ramp(300), invert=True, window=252, min_periods=60)
    assert flipped.iloc[-1] == pytest.approx(100.0 - plain.iloc[-1])


def test_rank_series_needs_min_periods():
    assert sm.rank_series(ramp(20), min_periods=60).empty


def test_naive_index_strips_timezone():
    s = pd.Series([1.0], index=pd.DatetimeIndex(["2025-01-02"], tz="America/New_York"))
    assert sm.naive_index(s).index.tz is None


# -------------------------------------------------------------------- regimes
@pytest.mark.parametrize(
    ("score", "key"),
    [(0, "stress"), (19.9, "stress"), (30, "caution"), (50, "neutral"),
     (70, "appetite"), (95, "euphoria"), (100, "euphoria")],
)
def test_regime_bands(score, key):
    assert sm.regime_key(score) == key


def test_regime_of_nan_is_unknown():
    assert sm.regime_key(float("nan")) == "unknown"


# ------------------------------------------------------------------- composite
def tape(n: int, early: float, late: float, tail: int = 25, price: float = 100.0):
    """A price path that compounds at `early` and then at `late` for `tail` days.

    Built from returns rather than levels because most composite inputs read a
    *change*, and a straight line in price is a decelerating return series — a
    linear ramp scores as weakening momentum, which is the opposite of what a
    "rising market" fixture is supposed to mean.
    """
    r = np.full(n, early)
    r[-tail:] = late
    return pd.Series(price * np.cumprod(1.0 + r), index=days(n), dtype=float)


def _closes(n: int = 400) -> dict[str, pd.Series]:
    """A synthetic risk-on tape: a broad advance, falling vol, credit bid.

    Every leg is set so its own component lands near the top of its trailing
    year — equal weight outpacing cap weight, emerging markets outpacing the
    S&P, junk outpacing investment grade, long bonds sold.
    """
    return {
        "^GSPC": tape(n, 0.0002, 0.004),
        "SPY": tape(n, 0.0002, 0.004),
        "RSP": tape(n, 0.0002, 0.006),    # equal weight leads -> broad advance
        "EEM": tape(n, 0.0002, 0.007),    # emerging markets lead -> global risk-on
        "^VIX": tape(n, 0.0002, -0.010),  # volatility falling to its 1y low
        "HYG": tape(n, 0.0001, 0.003),
        "LQD": tape(n, 0.0001, 0.0005),
        "TLT": tape(n, 0.0001, -0.002),   # long bonds sold for stocks
        "IEF": flat(n),
    }


def test_pulse_scores_a_risk_on_tape_high():
    result = sm.pulse(_closes())
    assert result.score > 70
    assert result.regime in {"appetite", "euphoria"}
    assert len(result.components) >= sm.MIN_COMPONENTS


def test_pulse_components_average_to_the_headline():
    """The breakdown on screen must add up to the number above it."""
    result = sm.pulse(_closes())
    shown = [c.score for c in result.components]
    assert result.score == pytest.approx(sum(shown) / len(shown))


def test_pulse_reports_missing_components():
    closes = _closes()
    del closes["^VIX"]
    result = sm.pulse(closes)
    assert "volatility" in result.missing
    assert "term" in result.missing  # needs ^VIX3M, never present here
    assert not any(c.key == "volatility" for c in result.components)


def test_pulse_history_is_a_real_path():
    result = sm.pulse(_closes())
    assert len(result.history) > 50
    assert result.history.between(0, 100).all()
    assert result.as_of == result.history.index[-1]


def test_a_lone_series_running_ahead_does_not_become_the_composite():
    """The bug the alignment rules exist for.

    ^VIX prints an in-progress bar while the equity closes are still
    yesterday's. Averaged naively, the newest row holds that one series and the
    "composite" silently becomes it — a 7-input score reporting one input. The
    carry-forward fixes it by bringing the other six into that row, so the new
    reading uses today's vol against yesterday's everything-else, and the score
    stays a mean of the full set.
    """
    closes = _closes()
    before = sm.pulse(closes)
    vix = closes["^VIX"]
    extra = pd.Timestamp(vix.index[-1]) + pd.Timedelta(days=1)
    # A vol spike, so "the composite became the VIX component" would be loud.
    closes["^VIX"] = pd.concat([vix, pd.Series([60.0], index=[extra])])
    after = sm.pulse(closes)
    vol = next(c for c in after.components if c.key == "volatility")
    assert len(after.components) == len(before.components)
    assert after.score == pytest.approx(
        sum(c.score for c in after.components) / len(after.components)
    )
    assert after.score != pytest.approx(vol.score)


def test_pulse_drops_an_input_that_stopped_publishing():
    """A feed that died months ago must not carry its last value forever."""
    closes = _closes()
    closes["^VIX"] = closes["^VIX"].iloc[:-60]
    result = sm.pulse(closes)
    assert "volatility" in result.missing


def test_pulse_carries_a_one_day_reporting_lag():
    """A single missing session is a reporting lag, not a dead feed."""
    closes = _closes()
    full = sm.pulse(closes)
    closes["^VIX"] = closes["^VIX"].iloc[:-1]
    lagged = sm.pulse(closes)
    assert "volatility" not in lagged.missing
    assert lagged.as_of == full.as_of


def test_pulse_uses_the_fred_spread_when_given_one():
    closes = _closes()
    spread = ramp(400, 8.0, 3.0)  # spreads tightening = risk-on
    result = sm.pulse(closes, hy_spread=spread)
    credit = next(c for c in result.components if c.key == "credit")
    assert credit.raw == pytest.approx(3.0)
    assert credit.score > 90


def test_pulse_falls_back_to_etf_credit_without_fred():
    """No FRED spread means the credit leg is priced off ETFs, not dropped."""
    closes = _closes()
    result = sm.pulse(closes, hy_spread=None)
    credit = next(c for c in result.components if c.key == "credit")
    expected = float(closes["HYG"].iloc[-1] / closes["IEF"].iloc[-1])
    assert credit.raw == pytest.approx(expected)


def test_pulse_with_nothing_is_not_a_number():
    result = sm.pulse({})
    assert np.isnan(result.score)
    assert result.regime == "unknown"
    assert set(result.missing) == set(sm.COMPONENT_KEYS)


def test_component_text_handles_nan():
    assert sm.Component("x", 50.0, float("nan")).text == "n/a"


# -------------------------------------------------------------- personalisation
def test_relative_strength_is_excess_over_the_benchmark():
    closes = {"SPY": ramp(30, 100, 110), "XLE": ramp(30, 100, 120)}
    excess = sm.relative_strength(closes, {"Energy": "XLE"}, "SPY", 21)
    assert excess["Energy"] == pytest.approx(
        sm.pct_over(closes["XLE"], 21) - sm.pct_over(closes["SPY"], 21)
    )


def test_relative_strength_without_the_benchmark_is_empty():
    assert sm.relative_strength({"XLE": ramp(30)}, {"Energy": "XLE"}, "SPY", 21).empty


def test_rotation_capture_rescales_over_covered_weight():
    """An unmapped slice must not dilute the reading toward zero.

    Half the book in one sector that beat the index by 4% reads +4%, not +2%:
    the other half is crypto or a bond sleeve with no sector at all, and
    averaging it in as a zero would understate what the equity side did.
    """
    weights = pd.Series({"Technology": 0.5, "Crypto": 0.5})
    excess = pd.Series({"Technology": 0.04})
    assert sm.rotation_capture(weights, excess) == pytest.approx(0.04)


def test_rotation_capture_without_overlap_is_nan():
    assert np.isnan(
        sm.rotation_capture(pd.Series({"Crypto": 1.0}), pd.Series({"Energy": 0.01}))
    )


def test_tilt_counts_a_sector_you_do_not_hold():
    """Not holding a benchmark sector is a real underweight, not a blank."""
    book = pd.Series({"Technology": 0.6})
    bench = pd.Series({"Technology": 0.3, "Utilities": 0.1})
    result = sm.tilt(book, bench)
    assert result["Technology"] == pytest.approx(0.3)
    assert result["Utilities"] == pytest.approx(-0.1)


def test_fx_exposure_skips_the_base_currency():
    total, contrib = sm.fx_exposure(
        pd.Series({"EUR": 0.3, "USD": 0.7}), {"USD": -0.02}, base="EUR"
    )
    assert total == pytest.approx(-0.014)
    assert "EUR" not in contrib.index


def test_fx_exposure_without_a_quoted_move_is_nan():
    total, contrib = sm.fx_exposure(pd.Series({"USD": 1.0}), {}, base="EUR")
    assert np.isnan(total)
    assert contrib.empty


def test_pin_order_leads_with_the_reader_geography():
    order = sm.pin_order(sm.INDICES, pd.Series({"Spain": 0.8, "United States": 0.2}))
    assert order[0].ticker == "^IBEX"
    # Nothing is dropped, and an index with no country mapping keeps its place
    # at the back rather than vanishing.
    assert len(order) == len(sm.INDICES)
    assert {i.ticker for i in order} == {i.ticker for i in sm.INDICES}


def test_pin_order_without_weights_keeps_the_registry_order():
    assert sm.pin_order(sm.INDICES, pd.Series(dtype=float)) == list(sm.INDICES)


# ------------------------------------------------------------------ registries
def test_all_tickers_is_deduplicated_and_covers_every_registry():
    tickers = sm.all_tickers()
    assert len(tickers) == len(set(tickers))
    for wanted in (
        [i.ticker for i in sm.INDICES]
        + [g.ticker for g in sm.GAUGES]
        + list(sm.SECTOR_ETFS.values())
        + list(sm.PULSE_TICKERS)
        + list(sm.BENCHMARKS)
    ):
        assert wanted in tickers


def test_sector_etf_keys_match_the_allocation_buckets():
    """The join with a book's own sector weights is by string, so the sector
    registry has to spell sectors the way data.funds normalises them."""
    from stocks.data.funds import SECTOR_LABELS

    assert set(sm.SECTOR_ETFS) == set(SECTOR_LABELS.values())


# ---------------------------------------------------- basket vs the benchmarks
def test_beta_survives_a_book_spanning_two_exchange_timezones():
    """The regression the page's `naive_index` calls exist for.

    yfinance stamps each symbol's daily bars in its own exchange timezone, so a
    book holding a Madrid name and a New York name carries two zones. `beta`
    intersects on the index, so the same session under two zones has no
    overlap at all and the regression runs over nothing — a silent NaN in a
    KPI tile rather than an error.
    """
    from stocks.analysis.portfolio import beta, portfolio_returns, returns_frame

    n = 120
    madrid = pd.Series(
        np.linspace(100.0, 130.0, n),
        index=days(n).tz_localize("Europe/Madrid"),
        name="SAN.MC",
    )
    new_york = pd.Series(
        np.linspace(100.0, 140.0, n),
        index=days(n).tz_localize("America/New_York"),
        name="AAPL",
    )
    bench = pd.Series(
        np.linspace(100.0, 135.0, n),
        index=days(n).tz_localize("America/New_York"),
        name="^GSPC",
    )
    mixed = returns_frame(
        {"SAN.MC": sm.naive_index(madrid), "AAPL": sm.naive_index(new_york)}
    )
    port = sm.naive_index(portfolio_returns(mixed, {"SAN.MC": 0.4, "AAPL": 0.6}))
    bench_returns = sm.naive_index(bench.pct_change().iloc[1:])

    assert not port.empty
    assert not np.isnan(beta(port, bench_returns))

    # Without the normalisation there is no shared date at all.
    raw = returns_frame({"SAN.MC": madrid, "AAPL": new_york})
    assert raw.dropna().empty


# --------------------------------------------------------------------- trends
def test_changes_are_absolute_and_pct_changes_are_returns():
    """Rates move in their own units; prices move in percent."""
    s = pd.Series([4.00, 4.10, 4.33], index=days(3))
    assert sm.changes(s, {"a": 2})["a"] == pytest.approx(0.33)
    assert sm.pct_changes(s, {"a": 2})["a"] == pytest.approx(0.33 / 4.00)


def test_changes_omit_horizons_the_series_cannot_span():
    """Absent, not NaN — the caller prints what exists instead of "n/a" cells."""
    s = ramp(30)
    out = sm.changes(s, {"week": 5, "year": 252})
    assert set(out) == {"week"}
    assert set(sm.pct_changes(s, {"week": 5, "year": 252})) == {"week"}


def _path(*segments: tuple[float, float, int]) -> pd.Series:
    """A series built from (start, end, n) legs, so a shape can be dictated."""
    values: list[float] = []
    for lo, hi, n in segments:
        values.extend(np.linspace(lo, hi, n))
    return pd.Series(values, index=days(len(values)), dtype=float)


def test_trend_state_reads_established_trends():
    assert sm.trend_state(ramp(300, 100, 200)) == "up"
    assert sm.trend_state(ramp(300, 200, 100)) == "down"


def test_trend_state_separates_a_turn_from_a_trend():
    """The four states exist because the two middle ones are the useful ones.

    A long rise followed by a shallow slide is still above its slow average —
    a single "above its average" flag calls that an uptrend. The fast arm has
    already turned, so this reads as a top losing it rather than as health.
    Mirrored for a base forming after a long decline.
    """
    topping = _path((100.0, 200.0, 250), (200.0, 194.0, 30))
    assert sm.trend_state(topping) == "turning_down"
    basing = _path((200.0, 100.0, 250), (100.0, 106.0, 30))
    assert sm.trend_state(basing) == "turning_up"


def test_trend_state_direction_is_the_fast_arm_slope_not_a_crossover():
    """The regression behind that choice.

    Through a sharp rollover the fast average sits above the slow one for weeks
    purely because it has not caught down yet. A "fast above slow" direction
    test therefore reports a market falling every single day as heading *up*;
    the fast arm's own slope cannot.
    """
    crash = _path((100.0, 200.0, 240), (200.0, 150.0, 8))
    fast = crash.rolling(sm.TREND_FAST).mean()
    slow = crash.rolling(sm.TREND_SLOW).mean()
    # Eight sessions into a 25% drawdown the fast arm is still miles above the
    # slow one, so "fast above slow" would report this as recovering.
    assert fast.iloc[-1] > slow.iloc[-1]
    assert crash.iloc[-1] < slow.iloc[-1]
    assert sm.trend_state(crash) == "down"


def test_trend_state_needs_the_slow_window():
    assert sm.trend_state(ramp(40), slow=100) == "unknown"


def test_percentile_then_is_measured_as_of_that_date():
    """Not today's window shifted — the number the page would have printed.

    On a rising series both readings are at the top of their own trailing
    year, which is the point: a percentile is relative, and comparing today's
    percentile against a year that includes today's extremes would drift.
    """
    s = ramp(400)
    assert sm.percentile_now(s) == pytest.approx(100.0)
    assert sm.percentile_then(s, ago=21) == pytest.approx(100.0)


def test_percentile_then_is_nan_when_the_series_is_too_short():
    assert np.isnan(sm.percentile_then(ramp(10), ago=21))


def test_above_ma_share_excludes_unreadable_symbols_from_both_counts():
    closes = {
        "UP": ramp(300, 100, 200),
        "DOWN": ramp(300, 200, 100),
        "SHORT": ramp(20),
    }
    hits, total = sm.above_ma_share(closes, ["UP", "DOWN", "SHORT", "ABSENT"], 100)
    assert (hits, total) == (1, 2)


def test_above_ma_share_of_nothing_is_zero_zero():
    assert sm.above_ma_share({}, ["UP"], 100) == (0, 0)


def test_rolling_correlation_spans_two_timezones():
    n = 200
    a = pd.Series(
        np.linspace(100.0, 150.0, n), index=days(n).tz_localize("America/New_York")
    )
    b = pd.Series(
        np.linspace(150.0, 100.0, n), index=days(n).tz_localize("Europe/Madrid")
    )
    corr = sm.rolling_correlation(a, b, window=60)
    assert not corr.empty
    assert corr.between(-1, 1).all()


def test_rolling_correlation_too_short_is_empty():
    assert sm.rolling_correlation(ramp(30), ramp(30), window=60).empty


def test_regime_run_counts_only_the_current_band():
    # 10, 10 -> stress; then three sessions of 70 -> appetite.
    history = pd.Series([10.0, 10.0, 70.0, 70.0, 70.0], index=days(5))
    assert sm.regime_run(history) == 3


def test_regime_run_of_nothing_is_zero():
    assert sm.regime_run(pd.Series(dtype=float)) == 0


@pytest.mark.parametrize(
    ("yield_move", "slope_move", "expected"),
    [
        (+1.0, +1.0, "bear_steepening"),
        (+1.0, -1.0, "bear_flattening"),
        (-1.0, +1.0, "bull_steepening"),
        (-1.0, -1.0, "bull_flattening"),
    ],
)
def test_rate_quadrant_crosses_the_two_moves(yield_move, slope_move, expected):
    n = 100
    yields = pd.Series(np.linspace(4.0, 4.0 + yield_move, n), index=days(n))
    slope = pd.Series(np.linspace(0.4, 0.4 + slope_move, n), index=days(n))
    assert sm.rate_quadrant(yields, slope, days=63) == expected


def test_rate_quadrant_needs_both_series_to_span_the_window():
    assert sm.rate_quadrant(ramp(20), ramp(200), days=63) == "unknown"


def test_rolling_beta_recovers_a_known_beta():
    n = 200
    bench = pd.Series(
        np.sin(np.linspace(0, 20, n)) / 100, index=days(n), dtype=float
    )
    asset = bench * 1.5
    rolling = sm.rolling_beta(asset, bench, window=60)
    assert rolling.iloc[-1] == pytest.approx(1.5)


def test_rolling_beta_survives_a_flat_benchmark():
    """A benchmark with no variance would divide by zero, not raise."""
    n = 200
    flat_bench = pd.Series([0.0] * n, index=days(n), dtype=float)
    assert sm.rolling_beta(ramp(n).pct_change(), flat_bench, window=60).empty


def test_drift_returns_now_and_then():
    now, then = sm.drift(ramp(200, 0.0, 199.0), ago=63)
    assert now == pytest.approx(199.0)
    assert then == pytest.approx(136.0)


def test_drift_without_enough_history_has_no_then():
    now, then = sm.drift(ramp(10, 0.0, 9.0), ago=63)
    assert now == pytest.approx(9.0)
    assert np.isnan(then)


def test_components_carry_where_they_stood_a_month_ago():
    result = sm.pulse(_closes())
    assert result.components
    assert all(c.then == c.then for c in result.components)
    # The synthetic tape accelerates in its final stretch, so today's scores
    # sit above where they were before the acceleration.
    volatility = next(c for c in result.components if c.key == "volatility")
    assert volatility.score > volatility.then


def test_component_then_is_nan_without_enough_history():
    """A young composite still renders; the "was here" tick just has nowhere
    to sit."""
    closes = {k: v.iloc[:70] for k, v in _closes(330).items()}
    result = sm.pulse(closes, min_periods=60)
    assert result.components
    assert all(np.isnan(c.then) for c in result.components)
