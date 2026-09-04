"""Reading a price frame's index, typed.

pandas types `DataFrame.index` as the plain `Index` base and attaches
`DatetimeIndex`'s own date fields dynamically, so a checker sees none of
`.normalize()`, `.dayofweek`, `.tz` or `.strftime` on a frame that plainly has
them. Every price history in the web layer is keyed by timestamp; naming that
once here keeps the suppressions off the call sites.
"""

from __future__ import annotations

from typing import cast

import pandas as pd


def dates(df: pd.DataFrame) -> pd.DatetimeIndex:
    """`df.index` as the DatetimeIndex it always is."""
    return cast(pd.DatetimeIndex, df.index)


def sessions(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Each bar's calendar day at midnight — intraday bars grouped by session."""
    return dates(df).normalize()  # ty: ignore[unresolved-attribute]


def weekdays(df: pd.DataFrame):
    """Monday=0 .. Sunday=6 for every bar."""
    return dates(df).dayofweek  # ty: ignore[unresolved-attribute]
