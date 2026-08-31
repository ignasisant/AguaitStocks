"""Phone rendering of the tables that aren't ticker lists.

`ticker_table_html(mobile=...)` only covers frames keyed by a symbol. The
quarterly detail grids, the dividends-by-year table, insider trades, the comps
matrix and the import previews have no symbol to hang a dense row off, so on a
phone they stack into one card per row (widgets.stacked_table_html) instead of
panning sideways. These tests pin the stacking rules and the fact that no page
renders a bare st.dataframe any more — that's the regression that puts a
seven-column grid back on a 390px screen.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

from stocks.web import widgets
from stocks.web.widgets import (
    LOSS_COLOR,
    PROFIT_COLOR,
    TEXT_MUTED,
    data_table,
    stacked_table_html,
)

WEB = Path(widgets.__file__).parent


def _frame():
    return pd.DataFrame(
        {
            "quarter": ["Q2 FY26", "Q1 FY26"],
            "revenue": [94.0, 90.2],
            "yoy": [0.123, -0.004],
            "note": ["", None],
        }
    )


def _cards(html: str) -> list[str]:
    return re.findall(r'<div class="ags-card">(.*?)</div></div>', html, re.S)


def test_stacked_cards_replace_the_grid():
    html = stacked_table_html(_frame(), title="quarter")
    assert "<table" not in html
    assert html.count('class="ags-card"') == 2


def test_title_column_heads_the_card_and_leaves_the_lines():
    html = stacked_table_html(_frame(), title="quarter")
    first = html.split('class="ags-card"')[1]
    assert '<div class="ags-title">Q2 FY26</div>' in first
    # …and it isn't repeated as a label/value line below the heading.
    assert first.count("Q2 FY26") == 1


def test_index_heads_the_card_when_asked():
    """Frames keyed by year/period (dividends, the transposed GAAP grid) carry
    their heading in the index, not in a column."""
    frame = _frame().set_index("quarter")
    html = stacked_table_html(frame, index_title=True)
    assert '<div class="ags-title">Q2 FY26</div>' in html


def test_formats_and_signs_match_the_desktop_cell():
    html = stacked_table_html(
        _frame(),
        title="quarter",
        fmt={"revenue": "${:,.1f}B", "yoy": "{:+.1%}"},
        signed=("yoy",),
        labels={"revenue": "Revenue", "yoy": "YoY"},
    )
    assert "$94.0B" in html
    assert f'<span style="color: {PROFIT_COLOR}">+12.3%</span>' in html
    assert f'<span style="color: {LOSS_COLOR}">-0.4%</span>' in html
    assert ">Revenue<" in html and ">YoY<" in html


def test_signed_zero_reads_neutral():
    """Same rule as the desktop table: an exact 0 drops the "+" and greys."""
    frame = pd.DataFrame({"k": ["a"], "chg": [0.0]})
    html = stacked_table_html(frame, title="k", fmt={"chg": "{:+.1%}"},
                              signed=("chg",))
    assert f'<span style="color: {TEXT_MUTED}">0.0%</span>' in html


def test_missing_cells_are_dropped_not_printed():
    """A phone card is worth more short than complete — "n/a" lines are noise
    where a desktop row has a column to fill."""
    html = stacked_table_html(_frame(), title="quarter")
    assert "n/a" not in html and ">note<" not in html


def test_hidden_columns_never_reach_a_card():
    html = stacked_table_html(_frame(), title="quarter", hide=("revenue",))
    assert "94" not in html


def test_values_are_escaped_but_a_title_can_be_markup():
    """Comps headers are `ticker_cell` markup (logo + symbol), which has to
    survive the transpose; every other cell is untrusted text."""
    frame = pd.DataFrame({"v": ["<script>x</script>"]}, index=["<b>GOOG</b>"])
    html = stacked_table_html(frame, index_title=True, title_html=True)
    assert "<b>GOOG</b>" in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_callable_formatters_work_like_the_ticker_rows():
    html = stacked_table_html(
        pd.DataFrame({"k": ["a"], "v": [1234.0]}),
        title="k",
        fmt={"v": lambda x: f"~{x / 1000:.1f}k"},
    )
    assert "~1.2k" in html


def test_data_table_forks_on_the_user_agent(monkeypatch):
    calls = {}

    class _Target:
        def html(self, markup):
            calls["html"] = markup

        def dataframe(self, frame, **kwargs):
            calls["dataframe"] = (frame, kwargs)

    monkeypatch.setattr(widgets, "is_mobile", lambda: True)
    data_table(_frame(), title="quarter", container=_Target(), hide_index=True)
    assert "ags-card" in calls["html"] and "dataframe" not in calls

    calls.clear()
    monkeypatch.setattr(widgets, "is_mobile", lambda: False)
    data_table(_frame(), title="quarter", container=_Target(), hide_index=True)
    assert calls["dataframe"][1] == {"hide_index": True}


def test_desktop_gets_the_same_numbers_through_a_styler(monkeypatch):
    """`fmt` is the phone format; on desktop it drives a Styler so the two
    renderings print the same string — unless the caller runs its own
    column_config, which wins."""
    seen = {}

    class _Target:
        def dataframe(self, frame, **kwargs):
            seen["frame"] = frame

    monkeypatch.setattr(widgets, "is_mobile", lambda: False)
    data_table(_frame(), fmt={"revenue": "${:,.1f}B"}, container=_Target())
    assert "$94.0B" in seen["frame"].to_html()

    data_table(_frame(), fmt={"revenue": "${:,.1f}B"}, container=_Target(),
               column_config={"revenue": None})
    assert isinstance(seen["frame"], pd.DataFrame)


# --------------------------------------------------------------- page wiring


@pytest.mark.parametrize(
    "path",
    [
        "earnings_ui.py",
        "chat_core.py",
        "app_pages/portfolio.py",
        "app_pages/ticker.py",
        "app_pages/import_transactions.py",
    ],
)
def test_no_page_renders_a_bare_dataframe(path):
    """Every grid goes through data_table (or a ticker table with a `mobile`
    spec), so adding one back without a phone form is a test failure."""
    src = (WEB / path).read_text()
    assert not re.search(r"^\s*st\.dataframe\(", src, re.M)


def test_the_watchlist_editor_stays_editable_but_narrows():
    """A card list can't add or retag a holding, so profile keeps the grid and
    drops the two columns that carry no edit."""
    src = (WEB / "app_pages" / "profile.py").read_text()
    assert "st.data_editor(" in src and "column_order=" in src


def test_the_comps_matrix_transposes_on_a_phone():
    src = (WEB / "app_pages" / "ticker.py").read_text()
    assert "stacked_table_html(comp.T" in src
