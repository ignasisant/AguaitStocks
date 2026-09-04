"""Reading a price frame's index (stocks.web.frames).

Thin wrappers, but they are the one place the web layer asserts that a price
history's index really is a DatetimeIndex — pandas types it as the plain
`Index` base and attaches the date fields dynamically, so nothing else catches
a frame that arrives keyed by something else.
"""

import pandas as pd

from stocks.web import frames


def frame(stamps, closes=None):
    idx = pd.to_datetime(list(stamps))
    return pd.DataFrame({"Close": closes or list(range(len(idx)))}, index=idx)


def test_dates_hands_back_the_frame_s_own_index():
    df = frame(["2025-01-03", "2025-01-06"])
    assert frames.dates(df).equals(df.index)


def test_a_naive_history_reports_no_timezone():
    assert frames.dates(frame(["2025-01-03"])).tz is None


def test_a_tz_aware_history_keeps_its_timezone():
    df = frame(["2025-01-03"])
    df.index = df.index.tz_localize("America/New_York")
    assert frames.dates(df).tz is not None


def test_sessions_collapse_intraday_bars_onto_their_own_day():
    df = frame(["2025-01-03 09:30", "2025-01-03 15:55", "2025-01-06 09:30"])
    assert frames.sessions(df).unique().tolist() == [
        pd.Timestamp("2025-01-03"),
        pd.Timestamp("2025-01-06"),
    ]


def test_weekdays_number_monday_zero_through_sunday_six():
    # 2025-01-03 is a Friday, 2025-01-04 a Saturday, 2025-01-06 a Monday.
    df = frame(["2025-01-03", "2025-01-04", "2025-01-06"])
    assert frames.weekdays(df).tolist() == [4, 5, 0]


def test_an_empty_history_is_not_an_error():
    empty = pd.DataFrame({"Close": []}, index=pd.to_datetime([]))
    assert len(frames.sessions(empty)) == 0
    assert len(frames.weekdays(empty)) == 0
