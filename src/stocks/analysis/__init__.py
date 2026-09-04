"""Technical analysis and indicators."""

from __future__ import annotations

import pandas as pd


def naive_dates(index) -> pd.DatetimeIndex:
    """`index` as a tz-naive DatetimeIndex.

    Price histories arrive with a mix of tz-aware and naive indexes depending
    on the market and the interval, and everything downstream compares them
    against plain dates — so they are flattened on the way in. pandas types a
    frame's `.index` as the plain `Index` base, which carries none of the
    datetime methods; naming the real class once, here, keeps every caller
    free of that.
    """
    out = pd.DatetimeIndex(pd.to_datetime(index))
    if out.tz is None:
        return out
    return out.tz_localize(None)
