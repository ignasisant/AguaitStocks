"""Screener tests — synthetic metric dicts, no network."""

import pandas as pd

from stocks.analysis.screener import (
    Filter,
    apply_filters,
    format_frame,
    metrics_frame,
    rank,
)


def sample_metrics() -> list[dict]:
    return [
        {"ticker": "CHEAP", "pe_ttm": 10.0, "roic": 0.25, "fcf_yield": 0.06},
        {"ticker": "MID", "pe_ttm": 25.0, "roic": 0.15, "fcf_yield": 0.03},
        {"ticker": "RICH", "pe_ttm": 60.0, "roic": 0.05, "fcf_yield": None},
    ]


def test_metrics_frame_numeric_and_indexed():
    df = metrics_frame(sample_metrics())
    assert list(df.index) == ["CHEAP", "MID", "RICH"]
    assert df.loc["CHEAP", "pe_ttm"] == 10.0
    assert pd.isna(df.loc["RICH", "fcf_yield"])  # None -> NaN
    assert "roic" in df.columns


def test_apply_filters_min_and_max():
    df = metrics_frame(sample_metrics())
    out = apply_filters(df, [Filter("pe_ttm", "max", 30), Filter("roic", "min", 0.10)])
    assert list(out.index) == ["CHEAP", "MID"]


def test_apply_filters_nan_drops_row():
    df = metrics_frame(sample_metrics())
    out = apply_filters(df, [Filter("fcf_yield", "min", 0.0)])
    assert "RICH" not in out.index  # NaN fails the filter


def test_rank_default_direction():
    df = metrics_frame(sample_metrics())
    # pe_ttm is lower-is-better: cheapest first
    assert list(rank(df, "pe_ttm").index) == ["CHEAP", "MID", "RICH"]
    # roic is higher-is-better: best first
    assert list(rank(df, "roic").index) == ["CHEAP", "MID", "RICH"]


def test_rank_nan_sinks():
    df = metrics_frame(sample_metrics())
    assert list(rank(df, "fcf_yield").index)[-1] == "RICH"  # NaN last


def test_format_frame_has_units():
    df = metrics_frame(sample_metrics())
    disp = format_frame(df)
    assert disp.loc["CHEAP", "pe_ttm"] == "10.0x"
    assert disp.loc["CHEAP", "roic"] == "25.0%"
    assert disp.loc["RICH", "fcf_yield"] == "n/a"


def test_filter_unknown_metric_raises():
    df = metrics_frame(sample_metrics())
    try:
        apply_filters(df, [Filter("bogus", "min", 1)])
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown metric")
