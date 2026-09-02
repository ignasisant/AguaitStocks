"""BaFin Art. 19 MAR export tests — pure parse + mapping, no network."""

from datetime import date

from stocks.data.bafin import (
    EXPORT_TABLE_ID,
    _amount,
    _search_url,
    insider_transactions,
    parse_export,
)
from stocks.data.insiders import summarize, transactions_frame

REF = date(2026, 9, 2)

# Shape and wording copied from a live export (SAP SE / Nemetschek SE): header
# row first, German number format, money in both price columns, and one
# "Sonstiges" row that must not count as an open-market trade.
EXPORT_XML = """<?xml version="1.0"?>
<table>
<row>
<column>Emittent</column><column>Bafin-ID</column><column>ISIN</column>
<column>Meldepflichtiger</column><column>Position / Status</column>
<column>Art des Instruments</column><column>Art des Geschäfts</column>
<column>Durchschnittspreis</column><column>Aggregiertes Volumen</column>
<column>Mitteilungsdatum</column><column>Datum des Geschäfts</column>
<column>Ort des Geschäfts</column><column>Datum der Aktivierung</column>
</row>
<row>
<column>SAP SE</column><column>40001244</column><column>DE0007164600</column>
<column>Saueressig, Thomas Heinrich</column><column>Vorstand</column>
<column>Aktie</column><column>Kauf</column>
<column>178,30 EUR</column><column>267.450,00 EUR</column>
<column>26.08.2026</column><column>26.08.2026</column>
<column>Boerse Duesseldorf - Quotrix</column><column>26.08.2026 15:34:16</column>
</row>
<row>
<column>SAP SE</column><column>40001244</column><column>DE0007164600</column>
<column>Klein, Christian Kurt</column><column>Aufsichtsrat</column>
<column>Aktie</column><column>Verkauf</column>
<column>100,00 EUR</column><column>50.000,00 EUR</column>
<column>24.07.2026</column><column>24.07.2026</column>
<column>Tradegate</column><column>24.07.2026 13:17:46</column>
</row>
<row>
<column>SAP SE</column><column>40001244</column><column>DE0007164600</column>
<column>Alam, Muhammad</column><column>Vorstand</column>
<column>Aktie</column><column>Sonstiges</column>
<column>150,00 EUR</column><column>15.000,00 EUR</column>
<column>11.06.2026</column><column>11.06.2026</column>
<column>AQUIS EXCHANGE EUROPE</column><column>15.06.2026 08:22:48</column>
</row>
</table>
"""


def test_parse_export_maps_rows_to_transactions():
    txs = parse_export(EXPORT_XML, "SAP.DE")
    assert len(txs) == 3
    buy = txs[0]
    assert buy.date == date(2026, 8, 26)
    assert buy.insider == "Saueressig, Thomas Heinrich"
    assert buy.relationship == "Management Board"
    assert buy.code == "P" and buy.acquired
    assert buy.price == 178.30
    # BaFin reports money, not shares: 267,450 EUR / 178.30 = 1,500 shares.
    assert round(buy.shares) == 1500
    assert buy.currency == "EUR" and buy.source == "BaFin"
    assert buy.ticker == "SAP.DE"


def test_parse_export_sorts_newest_first_and_maps_roles():
    txs = parse_export(EXPORT_XML)
    assert [t.date for t in txs] == [
        date(2026, 8, 26), date(2026, 7, 24), date(2026, 6, 11),
    ]
    assert txs[1].relationship == "Supervisory Board"


def test_non_open_market_row_is_kept_but_excluded_from_the_net():
    txs = parse_export(EXPORT_XML)
    other = txs[2]
    assert other.code == "O" and not other.is_open_market
    summ = summarize(txs, ref=REF)
    assert summ.buy_count == 1 and summ.sell_count == 1
    assert summ.buy_value == 267450.0
    assert summ.sell_value == 50000.0
    assert summ.net_value == 217450.0


def test_frame_renders_bafin_rows_like_form4_rows():
    df = transactions_frame(parse_export(EXPORT_XML))
    assert list(df["Type"]) == ["Buy (open market)", "Sell (open market)", "Other"]
    assert df.loc[1, "Shares"] == -500  # a sale is signed negative


def test_closely_associated_role_is_translated():
    xml = EXPORT_XML.replace(
        "<column>Vorstand</column>", "<column>in enger Beziehung</column>", 1
    )
    assert parse_export(xml)[0].relationship == "Closely associated"


def test_amount_parses_german_number_format():
    assert _amount("267.450,00 EUR") == (267450.0, "EUR")
    assert _amount("5,66 EUR") == (5.66, "EUR")
    assert _amount(None) == (None, "")
    assert _amount("n/a") == (None, "")


def test_malformed_or_empty_export_is_empty_not_an_error():
    assert parse_export("<html>not the export</html>") == []
    assert parse_export("<table><row><column>Emittent</column></row></table>") == []
    assert parse_export("") == []


def test_rows_without_a_usable_price_are_skipped():
    xml = EXPORT_XML.replace("<column>178,30 EUR</column>", "<column></column>")
    assert len(parse_export(xml)) == 2


def test_search_url_prefers_isin_and_asks_for_xml():
    url = _search_url(issuer="Nemetschek SE", isin="DE0006452907")
    assert "emittentIsin=DE0006452907" in url
    assert "emittentName=&" in url  # ISIN wins; the name is not also sent
    assert f"{EXPORT_TABLE_ID}-e=3" in url  # displaytag XML export
    assert "emittentName=Nemetschek+SE" in _search_url(issuer="Nemetschek SE")


def test_no_issuer_and_no_isin_makes_no_request(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("network touched without a search key")

    monkeypatch.setattr("stocks.data.bafin.get_bytes", boom)
    assert insider_transactions("NEM.F") == []


def test_network_failure_degrades_to_empty(monkeypatch):
    import urllib.error

    def dead(*a, **k):
        raise urllib.error.URLError("portal down")

    monkeypatch.setattr("stocks.data.bafin.get_bytes", dead)
    assert insider_transactions("SAP.DE", issuer="SAP SE") == []


def test_html_form_response_is_treated_as_no_data(monkeypatch):
    monkeypatch.setattr(
        "stocks.data.bafin.get_bytes",
        lambda *a, **k: b"<!DOCTYPE html><html><body>Suchformular</body></html>",
    )
    assert insider_transactions("SAP.DE", issuer="SAP SE") == []
