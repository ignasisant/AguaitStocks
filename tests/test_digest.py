"""Daily digest builder (stocks.notify.digest)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stocks.data.earnings import EarningsEvent
from stocks.notify import digest as dg

D = date(2026, 8, 14)  # a Friday


def full_data() -> dg.DigestData:
    return dg.DigestData(
        date=D,
        total_eur=48230.0,
        day=(412.0, 0.0086),
        week=(-1105.0, -0.0224),
        movers=[("NVDA", 0.032), ("SHOP", 0.021), ("MELI", 0.014),
                ("TTD", -0.011), ("FSLR", -0.019), ("UNH", -0.028)],
        earnings=[EarningsEvent("NVDA", date(2026, 8, 20), 6)],
        highlight="Nvidia drove most of today's gain.",
    )


# ---------------------------------------------------------------- rendering


def test_render_full_digest_en():
    text = dg.render_digest(full_data(), "en")
    assert "<b>📊 TopStocks — daily digest</b> · Fri 14 Aug" in text
    assert "<b>Portfolio</b> €48,230" in text
    assert "Day +412 € (+0.86%)" in text
    assert "Week -1,105 € (-2.24%)" in text
    assert "▲ NVDA +3.2%" in text and "▼ UNH -2.8%" in text
    assert "• NVDA — Thu 20 Aug (T-6)" in text
    assert "💡 Nvidia drove most of today" in text


def test_render_spanish_labels():
    text = dg.render_digest(full_data(), "es")
    assert "resumen diario" in text
    assert "<b>Cartera</b>" in text
    assert "Día" in text and "Semana" in text
    assert "vie 14 ago" in text


def test_render_watchlist_only_omits_value_lines():
    data = dg.DigestData(date=D, watchlist_only=True,
                         movers=[("AAPL", 0.01)], earnings=[])
    text = dg.render_digest(data, "en")
    assert "Portfolio" not in text
    assert "▲ AAPL +1.0%" in text


def test_render_escapes_html_in_dynamic_text():
    data = dg.DigestData(date=D, movers=[("A&B<X>", 0.05)],
                         highlight="risk <on> & rising")
    text = dg.render_digest(data, "en")
    assert "A&amp;B&lt;X&gt;" in text
    assert "risk &lt;on&gt; &amp; rising" in text


def test_render_partial_sections_still_render():
    data = dg.DigestData(date=D, total_eur=1000.0)  # no day/week/movers/earnings
    text = dg.render_digest(data, "en")
    assert "€1,000" in text
    assert "Top movers" not in text and "Earnings" not in text


def test_movers_capped_at_three_per_side():
    movers = [(f"G{i}", 0.01 * (10 - i)) for i in range(5)]
    movers += [(f"L{i}", -0.01 * (i + 1)) for i in range(5)]
    text = dg.render_digest(dg.DigestData(date=D, movers=movers), "en")
    assert text.count("▲") == 3 and text.count("▼") == 3


# ------------------------------------------------------------ compute


def test_compute_digest_data_watchlist_only(tmp_path, monkeypatch):
    """No ledger transactions -> movers/earnings from the watchlist."""
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("watchlist:\n  - ticker: AAPL\n")
    db = tmp_path / "portfolio.db"  # missing file -> no transactions

    import stocks.analysis.portfolio as ap

    monkeypatch.setattr(ap, "session_moves", lambda ts, max_workers=8: {"AAPL": 0.012})
    monkeypatch.setattr(
        dg, "upcoming",
        lambda holdings, within_days=7: [EarningsEvent("AAPL", date(2026, 8, 18), 4)],
    )

    data = dg.compute_digest_data(watchlist, db)
    assert data.watchlist_only is True
    assert data.total_eur is None
    assert data.movers == [("AAPL", 0.012)]
    assert data.earnings[0].ticker == "AAPL"


def test_compute_digest_data_with_ledger(tmp_path, monkeypatch):
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("watchlist: []\n")
    db = tmp_path / "portfolio.db"

    class FakePosition:
        ticker = "NVDA"

    monkeypatch.setattr(dg, "all_transactions", lambda path: ["tx"])
    monkeypatch.setattr(dg, "build", lambda txs: ([FakePosition()], []))

    import stocks.analysis.portfolio as ap

    idx = pd.to_datetime(["2026-08-06", "2026-08-13", "2026-08-14"])
    values = pd.DataFrame({"NVDA": [900.0, 950.0, 1000.0]}, index=idx)
    monkeypatch.setattr(ap, "position_values_history", lambda pos, period="1mo": values)
    monkeypatch.setattr(ap, "session_moves", lambda ts, max_workers=8: {"NVDA": 0.05})
    monkeypatch.setattr(dg, "upcoming", lambda holdings, within_days=7: [])

    data = dg.compute_digest_data(watchlist, db)
    assert data.watchlist_only is False
    assert data.total_eur == 1000.0
    assert data.day == pytest.approx((50.0, 50.0 / 950.0))
    assert data.week == pytest.approx((100.0, 100.0 / 900.0))
    assert data.movers == [("NVDA", 0.05)]


def test_compute_sections_fail_independently(tmp_path, monkeypatch):
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("watchlist:\n  - ticker: AAPL\n")

    import stocks.analysis.portfolio as ap

    def boom(*a, **k):
        raise RuntimeError("throttled")

    monkeypatch.setattr(ap, "session_moves", boom)
    monkeypatch.setattr(dg, "upcoming", boom)

    data = dg.compute_digest_data(watchlist, tmp_path / "portfolio.db")
    assert data.movers == [] and data.earnings == []  # dropped, not raised
