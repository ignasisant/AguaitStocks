"""Importing a statement from the assistant panel (web/chat_core.py).

The UI is Streamlit, but the two steps that touch the user's book are not:
_prepare_import turns an upload into a validated, previewable batch and writes
nothing, and _commit_import writes exactly that batch and records it as
undoable. Both are exercised here against real files and a real ledger.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from stocks.portfolio import last_import
from stocks.portfolio.ledger import all_transactions
from stocks.web import chat_core

T212_CSV = (
    "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,"
    "Currency (Price / share),Exchange rate,Total,Currency (Total),"
    "Withholding tax,Currency (Withholding tax),Notes,ID\n"
    "Market buy,2024-01-03 14:30:15,US0378331005,AAPL,Apple Inc,"
    "10.0000000,125.00,USD,1.0854,1151.65,EUR,,,,EOF1\n"
    "Market buy,2024-01-04 14:30:15,US5949181045,MSFT,Microsoft,"
    "2.0000000,370.00,USD,1.0854,740.00,EUR,,,,EOF2\n"
)

UNKNOWN_CSV = (
    "Fecha;Valor;Operación;Títulos;Precio;Divisa\n"
    "02/01/2024;AAPL;Compra;10;180,50;USD\n"
    "05/03/2024;AAPL;Traspaso;1;1,00;USD\n"
)

MAPPING = {
    "header_row": 0,
    "columns": {"date": 0, "ticker": 1, "action": 2, "quantity": 3,
                "price": 4, "currency": 5},
    "date_format": "%d/%m/%Y", "decimal": ",", "thousands": ".",
    "action_map": {"Compra": "buy"},
}


class _StubProvider:
    classifier_model = "stub-mini"
    default_model = "stub"

    def __init__(self, mapping=MAPPING):
        self.mapping, self.calls = mapping, []

    def complete(self, api_key, model, system, messages):
        self.calls.append(messages)
        return json.dumps(self.mapping)


@pytest.fixture
def account(tmp_path, monkeypatch):
    """A signed-in account's paths, plus a plain-dict session state."""
    (tmp_path / "watchlist.yaml").write_text(
        yaml.safe_dump({"watchlist": [{"ticker": "AAPL"}]}))
    paths = SimpleNamespace(
        root=tmp_path,
        watchlist=tmp_path / "watchlist.yaml",
        db=tmp_path / "portfolio.db",
        last_import=tmp_path / "last_import.json",
        prefs=tmp_path / "prefs.json",
        chat=tmp_path / "chat.json",
    )
    monkeypatch.setattr(chat_core.auth, "user_paths", lambda: paths)
    monkeypatch.setattr(chat_core.st, "session_state", {})
    # tr() reads the session language; the English default is enough here.
    monkeypatch.setattr(chat_core, "tr", lambda key, **kw: key)
    return paths


# ------------------------------------------------------------------ prepare


def test_prepare_reads_a_known_broker_without_calling_the_model(account):
    provider = _StubProvider()
    pending = chat_core._prepare_import("t212.csv", T212_CSV.encode(),
                                        provider, "key")

    assert pending["platform"] == "trading212"
    assert pending["label"] == "Trading 212"
    assert [t.ticker for t in pending["transactions"]] == ["AAPL", "MSFT"]
    assert provider.calls == []


def test_prepare_maps_an_unknown_export_and_reports_what_it_skipped(account):
    pending = chat_core._prepare_import("extracto.csv", UNKNOWN_CSV.encode(),
                                        _StubProvider(), "key")

    assert pending["platform"] == "llm"
    assert len(pending["transactions"]) == 1
    assert pending["skipped"][0]["type"] == "Traspaso"


def test_prepare_writes_nothing(account):
    chat_core._prepare_import("t212.csv", T212_CSV.encode(), _StubProvider(), "")
    assert all_transactions(account.db) == []
    assert not account.last_import.exists()


def test_prepare_quarantines_a_future_dated_row(account):
    """validate.py's checks apply to a chat import exactly as to the page's."""
    csv = (
        "date,ticker,action,quantity,price\n"
        "2099-01-02,AAPL,buy,10,180.5\n"
        "2024-01-02,AAPL,buy,10,180.5\n"
    )
    pending = chat_core._prepare_import("ledger.csv", csv.encode(),
                                        _StubProvider(), "")
    assert len(pending["transactions"]) == 1
    assert len(pending["rejected"]) == 1
    assert "future" in pending["rejected"][0]["why"].lower()


def test_prepare_flags_an_unknown_symbol_as_a_warning(account):
    csv = "date,ticker,action,quantity,price\n2024-01-02,ZZQQ,buy,1,10\n"
    pending = chat_core._prepare_import("ledger.csv", csv.encode(),
                                        _StubProvider(), "")
    assert len(pending["transactions"]) == 1  # warnings never block a row
    assert len(pending["flagged"]) == 1


def test_a_portfolio_report_is_named_as_such_not_as_a_failure(account, monkeypatch):
    """The real-world case: an iBroker/ClickTrade portfolio report has
    holdings but no dated movements, so the chat must say that rather than
    'I could not read this file'."""
    from stocks.portfolio import llm_map

    monkeypatch.setattr(llm_map, "_pdf_batches",
                        lambda data: ["Posiciones | Cantidad\nInMode | 112"])
    provider = _StubProvider()
    provider.complete = lambda *a: json.dumps(
        {"kind": "positions", "transactions": []})

    pending = chat_core._prepare_import("Portfolio.pdf", b"%PDF", provider, "k")
    assert pending["kind"] == llm_map.KIND_POSITIONS
    assert not pending["transactions"]

    history: list[dict] = []
    monkeypatch.setattr(chat_core.st, "session_state", {})
    monkeypatch.setattr(chat_core.skeletons, "reserve",
                        lambda *a, **kw: type("S", (), {"clear": lambda s: None})())
    monkeypatch.setattr(chat_core.auth, "save_chat", lambda *a, **kw: None)
    chat_core._ingest_uploads("panel", [("Portfolio.pdf", b"%PDF")],
                              provider, "k", history)

    assert "chat.import_positions" in history[-1]["content"]
    assert "chat.import_none" not in history[-1]["content"]
    # nothing was staged for import, so no preview appears
    assert chat_core._pending_key("panel") not in chat_core.st.session_state


# ------------------------------------------------------------------- commit


def test_commit_writes_the_batch_and_records_it_as_undoable(account):
    pending = chat_core._prepare_import("t212.csv", T212_CSV.encode(),
                                        _StubProvider(), "")
    chat_core.st.session_state[chat_core._pending_key("panel")] = pending
    history: list[dict] = []

    chat_core._commit_import("panel", pending, history)

    ledger = all_transactions(account.db)
    assert [t.ticker for t in ledger] == ["AAPL", "MSFT"]

    record = last_import.load(account.last_import)
    assert record.filename == "t212.csv" and record.platform == "trading212"
    assert sorted(record.tx_ids) == sorted(t.id for t in ledger)
    assert record.wiped is False  # chat never wipes the book

    # the confirmation is a turn in the thread, and the preview is gone
    assert history[-1]["action"] == "import"
    assert chat_core._pending_key("panel") not in chat_core.st.session_state


def test_commit_appends_to_an_existing_ledger(account):
    for csv in (T212_CSV, T212_CSV):
        pending = chat_core._prepare_import("t212.csv", csv.encode(),
                                            _StubProvider(), "")
        chat_core._commit_import("panel", pending, [])
    # The second pass sees the first as duplicates — flagged, not rejected —
    # so both land: re-importing an overlapping export is the user's call.
    assert len(all_transactions(account.db)) == 4


def test_commit_of_an_empty_batch_is_harmless(account):
    pending = {"filename": "x.csv", "label": "", "platform": "llm",
               "transactions": [], "rows": [], "flagged": [], "rejected": [],
               "skipped": []}
    chat_core._commit_import("panel", pending, [])
    assert all_transactions(account.db) == []
    assert last_import.load(account.last_import).tx_ids == []
