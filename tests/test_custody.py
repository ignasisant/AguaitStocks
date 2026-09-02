"""Per-broker custody of open shares (stocks.portfolio.custody)."""

from stocks.portfolio.custody import UNKNOWN, broker_weights, by_position, mix
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.positions import build


def _eur(amount: float, currency: str, day: str) -> float:
    return amount * (0.5 if currency == "USD" else 1.0)


def test_splits_one_ticker_across_brokers():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10,
                    currency="EUR", fee=2, note="revolut"),
        Transaction("2024-02-01", "A", "buy", quantity=30, price=20,
                    currency="EUR", note="clicktrade TELEFONICA"),
    ]
    row = by_position(txs, to_base=_eur)["A"]
    assert row["clicktrade"].quantity == 30  # largest slice first
    assert list(row) == ["clicktrade", "revolut"]
    assert row["revolut"].cost == 102.0  # buy fee included, as in FIFO
    assert mix(row) == [("clicktrade", 0.75), ("revolut", 0.25)]


def test_sell_only_moves_the_selling_broker():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10,
                    currency="EUR", note="revolut"),
        Transaction("2024-02-01", "A", "buy", quantity=10, price=10,
                    currency="EUR", note="clicktrade"),
        # Sold at ClickTrade: FIFO across the book would have taken the older
        # Revolut lot, but the shares that left the account are ClickTrade's.
        Transaction("2024-03-01", "A", "sell", quantity=6, price=15,
                    currency="EUR", note="clicktrade"),
    ]
    row = by_position(txs, to_base=_eur)["A"]
    assert row["revolut"].quantity == 10
    assert row["clicktrade"].quantity == 4
    assert row["clicktrade"].cost == 40.0  # 100 basis, 60% sold off


def test_closed_position_drops_out():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=5, price=10,
                    currency="EUR", note="revolut"),
        Transaction("2024-02-01", "A", "sell", quantity=5, price=12,
                    currency="EUR", note="revolut"),
    ]
    assert by_position(txs, to_base=_eur) == {}


def test_split_scales_every_broker_and_totals_match_fifo():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=100,
                    currency="USD", note="revolut"),
        Transaction("2024-02-01", "A", "buy", quantity=5, price=120,
                    currency="USD", note="ibkr"),
        Transaction("2024-03-01", "A", "split", quantity=4),
    ]
    row = by_position(txs, to_base=_eur)["A"]
    assert row["revolut"].quantity == 40
    assert row["ibkr"].quantity == 20
    # The tax replay is the reference: custody only re-cuts the same book.
    position = build(txs, to_base=_eur)[0][0]
    assert position.quantity == sum(c.quantity for c in row.values())
    assert abs(position.cost - sum(c.cost for c in row.values())) < 1e-9


def test_sell_beyond_own_lots_falls_back_to_the_oldest():
    # Shares bought at Revolut, transferred in kind, then sold at ClickTrade:
    # ClickTrade has no lots of its own, so the total must still reconcile.
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10,
                    currency="EUR", note="revolut"),
        Transaction("2024-02-01", "A", "sell", quantity=4, price=12,
                    currency="EUR", note="clicktrade"),
    ]
    row = by_position(txs, to_base=_eur)["A"]
    assert list(row) == ["revolut"]
    assert row["revolut"].quantity == 6
    assert build(txs, to_base=_eur)[0][0].quantity == 6


def test_broker_weights_split_value_by_share_count():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=30, price=10,
                    currency="EUR", note="revolut"),
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10,
                    currency="EUR", note="clicktrade"),
        Transaction("2024-01-01", "B", "buy", quantity=1, price=10,
                    currency="EUR", note="clicktrade"),
    ]
    custody = by_position(txs, to_base=_eur)
    out = broker_weights(custody, {"A": 0.8, "B": 0.2})
    assert abs(out["revolut"] - 0.6) < 1e-9  # 75% of A
    assert abs(out["clicktrade"] - 0.4) < 1e-9  # 25% of A + all of B
    # Largest first, so the donut's slices come out ordered.
    assert list(out) == ["revolut", "clicktrade"]


def test_broker_weights_keeps_an_unattributed_position():
    out = broker_weights({}, {"A": 1.0})
    assert out == {UNKNOWN: 1.0}
    assert mix({}) == []


# ---- Custody as the UI prints it: broker names and the header's brand marks.


def test_broker_name_labels_brands_and_generic_buckets():
    from stocks.web import widgets

    assert widgets.broker_name("clicktrade") == "ClickTrade"
    assert widgets.broker_name("trading212") == "Trading 212"
    # Not a platform key — title-cased rather than shown raw.
    assert widgets.broker_name("etoro") == "Etoro"
    # The two generic buckets are localized (English is the source catalog).
    assert widgets.broker_name("manual") == "Manual"
    assert widgets.broker_name(UNKNOWN) == "Unknown"


def test_broker_chips_html_marks_each_custodian(monkeypatch):
    from stocks.web import widgets

    monkeypatch.setattr(
        widgets, "brand_logo",
        lambda key, domain: f"app/static/logos/brand-{key}.png" if domain else None,
    )
    html = widgets.broker_chips_html([("revolut", 0.67), ("clicktrade", 0.33)])
    assert "brand-revolut.png" in html and "brand-clicktrade.png" in html
    # The share of the position rides the tooltip (and the alt text), never
    # the row itself — the marks sit next to a badge, no room for a number.
    assert 'title="Revolut · 67%"' in html and 'alt="Revolut · 67%"' in html
    assert ">67%" not in html

    # One custodian: the name alone, no percentage to state.
    assert 'title="Revolut"' in widgets.broker_chips_html([("revolut", 1.0)])

    # No brand domain (hand-entered rows) -> a name pill, never a blank gap.
    pill = widgets.broker_chips_html([("manual", 1.0)])
    assert "<img" not in pill and ">Manual<" in pill

    assert widgets.broker_chips_html([]) == ""
