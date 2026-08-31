"""Merged "€ (+%)" table cells — the pair collapse in widgets.ticker_table_html.

The Positions table showed each move twice (day_eur next to day_pct), so the
desktop grid carried ten columns of which four were the same two numbers. The
pair merge is what allows one cell per move; these tests pin the parts that
would silently regress it: the dropped column, the sign colors, the
off-session dimming, and the raw column names never reaching a header.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

from stocks.web import widgets
from stocks.web.widgets import (
    DOWN_COLOR,
    LOSS_BAND,
    LOSS_COLOR_MUTED,
    PROFIT_BAND,
    TEXT_MUTED,
    UP_COLOR,
    ticker_table_html,
)

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"

FMT = {
    "value_eur": "€{:,.0f}",
    "day_eur": "€{:+,.0f}",
    "day_pct": "{:+.1%}",
    "pnl_eur": "€{:,.0f}",
    "pnl_pct": "{:+.1%}",
}
SIGNED = ("day_eur", "day_pct", "pnl_eur", "pnl_pct")
PAIRS = (("day_eur", "day_pct"), ("pnl_eur", "pnl_pct"))


@pytest.fixture(autouse=True)
def _plain_ticker_cells(monkeypatch):
    """ticker_cell needs a logged-in session (logo cache, watchlist path);
    the pair logic doesn't care what the ticker cell contains."""
    monkeypatch.setattr(widgets, "ticker_cell", lambda t, **kw: f"<b>{t}</b>")


def _frame():
    return pd.DataFrame({
        "ticker": ["GOOG", "META", "NVO"],
        "value_eur": [8372.0, 4436.0, 3511.0],
        "day_eur": [-97.0, 93.0, 0.0],
        "day_pct": [-0.011, 0.021, 0.0],
        "pnl_eur": [2798.0, -288.0, float("nan")],
        "pnl_pct": [0.502, -0.061, float("nan")],
    })


def _table(**kw) -> str:
    return ticker_table_html(
        _frame(), fmt=FMT, signed=SIGNED, pairs=PAIRS, **kw
    )


def _cells(html: str) -> list[list[str]]:
    return [
        re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        for row in re.findall(r"<tr>(.*?)</tr>", html, re.S)[1:]
    ]


def test_pair_renders_both_numbers_in_one_cell_and_drops_the_pct_column():
    html = _table()
    # 3 columns left (ticker, value, day, P/L) — the two pct columns folded in.
    assert [len(r) for r in _cells(html)] == [4, 4, 4]
    day = _cells(html)[0][2]
    assert "€-97" in day and "-1.1%" in day
    assert "day_pct" not in html and "pnl_pct" not in html


def test_sign_drives_the_pill_tint_and_a_flat_move_reads_neutral():
    rows = _cells(_table())
    assert LOSS_BAND in rows[0][2] and DOWN_COLOR in rows[0][2]   # -1.1%
    assert PROFIT_BAND in rows[1][2] and UP_COLOR in rows[1][2]   # +2.1%
    # Flat: no "+0.0%", grey pill — a market that hasn't moved isn't a gain.
    assert "+0.0%" not in rows[2][2] and "0.0%" in rows[2][2]
    assert TEXT_MUTED in rows[2][2]


def test_missing_pct_leaves_plain_text_not_an_empty_pill():
    nvo_pnl = _cells(_table())[2][3]
    assert "n/a" in nvo_pnl
    assert PROFIT_BAND not in nvo_pnl and LOSS_BAND not in nvo_pnl


def test_off_session_rows_dim_only_the_day_pair():
    rows = _cells(_table(muted={"GOOG"}, muted_cols=("day_eur", "day_pct")))
    assert LOSS_COLOR_MUTED in rows[0][2]      # day pair dimmed
    assert UP_COLOR in rows[0][3]              # total P/L keeps full color
    # A live row is untouched by the muting.
    assert LOSS_COLOR_MUTED not in rows[1][2]


def test_labels_still_name_the_merged_column():
    html = _table(labels={"day_eur": "Today", "pnl_eur": "Total P/L"})
    heads = re.findall(r"<th[^>]*>(.*?)</th>", html)
    assert "Today" in heads and "Total P/L" in heads
    assert "day_eur" not in heads and "pnl_eur" not in heads


def test_positions_table_ships_translated_headers():
    """The bug that started this: the Positions grid rendered raw frame keys
    (cost_eur, day_pct) as headers. Every column it feeds the table needs a
    label, in both catalogs."""
    src = (WEB / "app_pages" / "portfolio.py").read_text()
    keys = set(re.findall(r'tr\("(portfolio\.col_[a-z_]+)"\)', src))
    assert keys, "positions table lost its column labels"
    import json

    for lang in ("en", "es"):
        catalog = json.loads(
            (WEB / "locales" / lang / "portfolio.json").read_text(encoding="utf-8")
        )
        assert keys <= set(catalog), (lang, keys - set(catalog))


# ------------------------------------------------------------- click-to-sort


def test_sortable_stamps_raw_values_on_every_body_cell():
    """The client sorter reads data-s, never the formatted text — a cell
    printing "€8,372" or "€-97 (-1.1%)" has to carry 8372.0 / -97.0."""
    html = _table(sortable="positions")
    keys = dict(
        (m.group(1), m.group(2))
        for m in re.finditer(r'<td id="T_\w+_(row\d+_col\d+)"[^>]*data-s="([^"]*)"', html)
    )
    assert len(keys) == 3 * 4  # every cell, no gaps
    assert keys["row0_col1"] == "8372.0"       # market value, not "€8,372"
    assert keys["row0_col2"] == "-97.0"        # merged cell sorts by the €
    assert keys["row2_col3"] == ""             # NaN P/L → blank, sorted last
    assert keys["row0_col0"] == "goog"         # ticker sorts by symbol


def test_sortable_marks_the_table_with_its_memory_id():
    html = _table(sortable="positions")
    assert 'data-ag-sort="positions"' in html
    assert "cursor: pointer" in html      # headers read as controls
    assert "th[data-ag-dir]" in html      # active column brightens
    assert ".ag-arrow" in html            # …and carries the direction arrow


def test_plain_table_carries_no_sort_hooks():
    html = _table()
    assert "data-s=" not in html and "data-ag-sort" not in html


def test_sorter_is_wired_once_at_the_entry_point():
    """The handler lives in app.py (one MutationObserver for every table on
    the page); a table alone can't sort itself."""
    src = (WEB / "app.py").read_text()
    assert "__topstocksTableSort" in src
    assert "unsafe_allow_javascript=True" in src


def test_positions_and_realized_tables_are_sortable():
    src = (WEB / "app_pages" / "portfolio.py").read_text()
    assert 'sortable="positions"' in src and 'sortable="realized"' in src


# ------------------------------------------------------------- phone rows


def test_phone_row_puts_the_badge_next_to_the_symbol(monkeypatch):
    """The dim sub line ellipsizes, which cut the P/L number mid-digit; as a
    pill on line 1 it always reads in full."""
    monkeypatch.setattr(widgets, "is_mobile", lambda: True)
    monkeypatch.setattr(widgets, "logo", lambda t: None)
    monkeypatch.setattr(widgets, "company_name", lambda t: "Alphabet")
    html = ticker_table_html(
        _frame().assign(weight=[0.077, 0.041, 0.032]),
        fmt={**FMT, "weight": "{:.1%}"},
        signed=SIGNED,
        mobile={"value": "value_eur", "delta": "day_pct",
                "badge": "pnl_pct", "sub": ("weight",)},
    )
    line1 = re.search(r'<div class="agr-l1">(.*?)</div>', html, re.S).group(1)
    assert "GOOG" in line1 and "+50.2%" in line1 and PROFIT_BAND in line1
    # …and the sub line is back to name + weight only.
    sub = re.search(r'<div class="agr-l2">(.*?)</div>', html, re.S).group(1)
    assert "Alphabet" in sub and "7.7%" in sub and "50.2%" not in sub


def _realized_frame():
    return pd.DataFrame({
        "ticker": ["GOOG", "META"],
        "buy": ["2021-03-04", "2022-01-10"],
        "sell": ["2024-05-06", "2024-08-01"],
        "qty": [12.5, 3.0],
        "cost_eur": [1000.0, 500.0],
        "proceeds_eur": [1500.0, 480.0],
        "gain_eur": [500.0, -20.0],
        "gain_pct": [0.5, -0.04],
    })


REALIZED_FMT = {
    "qty": "{:,.4f}", "cost_eur": "€{:,.2f}", "proceeds_eur": "€{:,.2f}",
    "gain_eur": "€{:+,.2f}", "gain_pct": "{:+.1%}",
}
REALIZED_SIGNED = ("gain_eur", "gain_pct")


def test_realized_sales_merge_gain_and_return_on_desktop():
    """Adding the return can't widen the tax table — it rides in the gain
    cell, so the phone badge and the desktop grid share one column set."""
    html = ticker_table_html(
        _realized_frame(), fmt=REALIZED_FMT, signed=REALIZED_SIGNED,
        left_cols=("buy", "sell"), pairs=(("gain_eur", "gain_pct"),),
        labels={"gain_eur": "Gain"},
    )
    headers = re.findall(r"<th[^>]*>(.*?)</th>", html)
    assert "gain_pct" not in html and headers.count("Gain") == 1
    assert "€+500.00" in html and "+50.0%" in html


def test_realized_sales_render_as_phone_rows(monkeypatch):
    """Seven columns pan off a 390px screen; the tax tab's sales list gets
    the same dense rows as Positions — dates on the wrapping dim line, the
    return as a pill, no horizontal scroll."""
    monkeypatch.setattr(widgets, "is_mobile", lambda: True)
    monkeypatch.setattr(widgets, "logo", lambda t: None)
    html = ticker_table_html(
        _realized_frame(), fmt=REALIZED_FMT, signed=REALIZED_SIGNED,
        names=False,
        mobile={"value": "proceeds_eur", "delta": "gain_eur",
                "badge": "gain_pct", "wrap": True,
                "sub": ("buy", "sell", "qty"),
                "sub_labels": {"buy": "Bought", "sell": "Sold",
                               "qty": "Shares"}},
    )
    assert "<table" not in html
    line1 = re.search(r'<div class="agr-l1">(.*?)</div>', html, re.S).group(1)
    assert "GOOG" in line1 and "+50.0%" in line1 and PROFIT_BAND in line1
    sub = re.search(r'<div class="agr-l2 agr-wrap">(.*?)</div>', html, re.S).group(1)
    assert "Bought 2021-03-04" in sub and "Sold 2024-05-06" in sub
    # Proceeds on line 1 right, the signed gain under it.
    side = re.search(r'<div class="agr-side">(.*?)</div></a>', html, re.S).group(1)
    assert "€1,500.00" in side and "€+500.00" in side


def test_tax_tab_offers_the_full_table_on_a_phone():
    """The dense rows drop columns (cost basis, shares detail), so the phone
    branch keeps the sortable table one expander below — same as Positions."""
    src = (WEB / "app_pages" / "portfolio.py").read_text()
    assert "portfolio.all_columns_realized" in src
