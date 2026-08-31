"""Column mapping for exports no dedicated parser owns (portfolio/llm_map).

The contract under test is the split that makes this safe: the model names
columns, Python converts rows. So the interesting cases are the conversion
(European decimals, accountancy negatives, date formats, unmapped actions),
the defensive validation of whatever JSON comes back, and the fact that one
call maps a file of any length.
"""

from __future__ import annotations

import io
import json

import pytest

from stocks.portfolio import llm_map

# A hand-kept Spanish spreadsheet: two preamble rows, semicolons, comma
# decimals, dotted thousands, an action column in Spanish and a trailing
# totals line — none of which any dedicated parser accepts.
ES_CSV = (
    "Extracto de operaciones;;;;;;\n"
    "Cuenta 1234;;;;;;\n"
    "Fecha;Valor;Operación;Títulos;Precio;Divisa;Comisión\n"
    "02/01/2024;AAPL;Compra;10;180,50;USD;1,20\n"
    "05/03/2024;AAPL;Venta;-4;190,00;USD;1,20\n"
    "16/02/2024;MSFT;Dividendo;0;1.234,56;USD;0\n"
    "03/04/2024;SAN;Traspaso;5;4,10;EUR;0\n"
)

MAPPING = {
    "header_row": 2,
    "columns": {"date": 0, "ticker": 1, "action": 2, "quantity": 3,
                "price": 4, "currency": 5, "fee": 6, "note": None},
    "date_format": "%d/%m/%Y",
    "decimal": ",",
    "thousands": ".",
    "action_map": {"compra": "buy", "venta": "sell", "dividendo": "dividend"},
}


class _StubProvider:
    classifier_model = "stub-mini"
    default_model = "stub"

    def __init__(self, reply="", boom=False):
        self.reply, self.boom = reply, boom
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append((api_key, model, system, messages))
        if self.boom:
            raise RuntimeError("network down")
        return self.reply


# -------------------------------------------------------------- file to grid


def test_read_grid_sniffs_semicolons_and_keeps_preamble():
    grid = llm_map.read_grid("extracto.csv", ES_CSV.encode("utf-8"))
    assert grid[2][:3] == ["Fecha", "Valor", "Operación"]
    assert grid[3][0] == "02/01/2024"


def test_read_grid_drops_blank_lines():
    grid = llm_map.read_grid("x.csv", b"a,b\n\n,\n1,2\n")
    assert grid == [["a", "b"], ["1", "2"]]


def test_read_grid_survives_latin1():
    grid = llm_map.read_grid("x.csv", "Fecha,Valor\n02/01/2024,Telefónica\n"
                             .encode("latin-1"))
    assert grid[1][0] == "02/01/2024"


def test_read_grid_returns_empty_for_an_unreadable_file():
    assert llm_map.read_grid("x.xlsx", b"not really a spreadsheet") == []


def test_sample_is_indexed_and_truncated():
    grid = [["a" * 100, "b"], ["1", "2"]]
    out = llm_map.sample(grid)
    assert out.startswith("row 0: ")
    assert "a" * llm_map.MAX_CELL in out
    assert "a" * (llm_map.MAX_CELL + 1) not in out


# ------------------------------------------------------------ mapping checks


def _grid():
    return llm_map.read_grid("extracto.csv", ES_CSV.encode("utf-8"))


def test_parse_mapping_accepts_a_good_reply():
    raw = '{"header_row": 2, "columns": {"date": 0, "ticker": 1, "action": 2}}'
    out = llm_map.parse_mapping(raw, _grid())
    assert out["header_row"] == 2
    assert out["columns"]["date"] == 0
    assert out["columns"]["quantity"] is None  # absent stays absent


def test_parse_mapping_rejects_a_missing_required_column():
    raw = '{"header_row": 2, "columns": {"date": 0, "ticker": 1}}'
    assert llm_map.parse_mapping(raw, _grid()) is None


def test_parse_mapping_rejects_an_out_of_range_index():
    raw = '{"header_row": 2, "columns": {"date": 0, "ticker": 1, "action": 99}}'
    assert llm_map.parse_mapping(raw, _grid()) is None


def test_parse_mapping_rejects_an_explicit_refusal():
    assert llm_map.parse_mapping('{"columns": null}', _grid()) is None


def test_parse_mapping_rejects_garbage():
    assert llm_map.parse_mapping("sorry, I can't help", _grid()) is None
    assert llm_map.parse_mapping("{not json", _grid()) is None


def test_parse_mapping_drops_unknown_actions():
    raw = ('{"header_row": 2, "columns": {"date": 0, "ticker": 1, "action": 2},'
           ' "action_map": {"Compra": "buy", "Traspaso": "transfer"}}')
    out = llm_map.parse_mapping(raw, _grid())
    assert out["action_map"] == {"compra": "buy"}


def test_parse_mapping_refuses_a_thousands_separator_equal_to_the_decimal():
    """Stripping it would silently eat the decimals off every number."""
    raw = ('{"header_row": 2, "columns": {"date": 0, "ticker": 1, "action": 2},'
           ' "decimal": ",", "thousands": ","}')
    assert llm_map.parse_mapping(raw, _grid())["thousands"] == ""


# ------------------------------------------------------------------ numbers


@pytest.mark.parametrize("text,expected", [
    ("1.234,56", 1234.56),
    ("€ 1.234,56", 1234.56),
    ("-12,50", -12.5),
    ("(12,50)", -12.5),  # accountancy negative
    (" 1 234,00 EUR", 1234.0),  # non-breaking spaces
    ("", None),
    ("n/a", None),
])
def test_number_european(text, expected):
    assert llm_map._number(text, ",", ".") == expected


@pytest.mark.parametrize("text,expected", [
    ("1,234.56", 1234.56),
    ("$1,234.56", 1234.56),
    ("180.50", 180.5),
])
def test_number_us(text, expected):
    assert llm_map._number(text, ".", ",") == expected


def test_number_recovers_undeclared_thousands():
    """'1.234.567' can only be a grouped integer — two separators, no decimals."""
    assert llm_map._number("1.234.567", ".", "") == 1234567.0


# -------------------------------------------------------------------- dates


def test_date_uses_the_mapped_format_first():
    assert llm_map._date("02/01/2024", "%d/%m/%Y") == "2024-01-02"
    assert llm_map._date("02/01/2024", "%m/%d/%Y") == "2024-02-01"


def test_date_falls_back_when_the_mapped_format_is_wrong():
    assert llm_map._date("2024-01-02", "%d/%m/%Y") == "2024-01-02"


def test_date_strips_a_time_part():
    assert llm_map._date("2024-01-02 14:30:15", "%Y-%m-%d") == "2024-01-02"


def test_date_gives_up_rather_than_guessing():
    assert llm_map._date("last tuesday", "%Y-%m-%d") is None
    assert llm_map._date("", "%Y-%m-%d") is None


# ------------------------------------------------------------------ applying


def test_apply_mapping_converts_every_row():
    result = llm_map.apply_mapping(_grid(), MAPPING)
    buy, sell, div = result.transactions

    assert (buy.date, buy.ticker, buy.action) == ("2024-01-02", "AAPL", "buy")
    assert (buy.quantity, buy.price, buy.fee, buy.currency) == (
        10.0, 180.5, 1.2, "USD")
    # exported as a negative quantity; the action already carries the direction
    assert sell.action == "sell" and sell.quantity == 4.0
    assert div.action == "dividend" and div.price == 1234.56


def test_apply_mapping_skips_unmapped_actions_with_a_reason():
    result = llm_map.apply_mapping(_grid(), MAPPING)
    assert [s["type"] for s in result.skipped] == ["Traspaso"]
    assert result.skipped[0]["reason"] == "action not recognised"
    assert result.skipped[0]["row"] == 7  # 1-based, header included


def test_apply_mapping_skips_an_unreadable_date():
    grid = [["Fecha", "Valor", "Operación"], ["mañana", "AAPL", "Compra"]]
    mapping = {**MAPPING, "header_row": 0,
               "columns": {**MAPPING["columns"], "quantity": None,
                           "price": None, "currency": None, "fee": None}}
    result = llm_map.apply_mapping(grid, mapping)
    assert not result.transactions
    assert "unreadable date" in result.skipped[0]["reason"]


def test_apply_mapping_skips_a_row_with_no_symbol():
    grid = [["Fecha", "Valor", "Operación"], ["02/01/2024", "", "Compra"]]
    mapping = {**MAPPING, "header_row": 0,
               "columns": {**MAPPING["columns"], "quantity": None,
                           "price": None, "currency": None, "fee": None}}
    result = llm_map.apply_mapping(grid, mapping)
    assert result.skipped[0]["reason"] == "no symbol"


def test_apply_mapping_accepts_an_already_canonical_action():
    """A file that already says "buy" needs no action_map entry."""
    grid = [["date", "ticker", "action"], ["2024-01-02", "AAPL", "BUY"]]
    mapping = {**MAPPING, "header_row": 0, "action_map": {},
               "columns": {**MAPPING["columns"], "quantity": None,
                           "price": None, "currency": None, "fee": None}}
    assert llm_map.apply_mapping(grid, mapping).transactions[0].action == "buy"


# ------------------------------------------------------------ pdf extraction
# A PDF has no regular grid to map, so the model extracts the values itself.
# _pdf_batches is monkeypatched throughout: pdfplumber's text extraction is
# not what these tests are about (same approach as test_revolut_pdf).


POSITIONS_PAGE = """Posiciones, EUR
Instrumento | Cantidad | PreciodeApertura | PrecioActual | Valordemercado
InModeLtd(ISIN:
USD | 112 | 18,15890 | 14,85000 | 1.428,82
IL0011595993)
"""

TRADES_PAGE = """Operaciones ejecutadas
Fecha | Valor | Tipo | Titulos | Precio
02/01/2024 | AAPL | Compra | 10 | 180,50
"""


def _extraction(kind, transactions=()):
    return json.dumps({"kind": kind, "transactions": list(transactions)})


def _batches(monkeypatch, pages):
    monkeypatch.setattr(llm_map, "_pdf_batches", lambda data: list(pages))


def test_a_portfolio_report_is_reported_as_positions(monkeypatch):
    """The user's real case: holdings with no dated movements. Nothing to
    import is the right answer — but it must not read as 'unreadable'."""
    _batches(monkeypatch, [POSITIONS_PAGE])
    found = llm_map.extract("Portfolio.pdf", b"%PDF",
                            _StubProvider(_extraction("positions")), "k")

    assert found.kind == llm_map.KIND_POSITIONS
    assert not found.result.transactions


def test_extracted_trades_become_transactions(monkeypatch):
    _batches(monkeypatch, [TRADES_PAGE])
    provider = _StubProvider(_extraction("trades", [{
        "date": "2024-01-02", "ticker": "aapl", "action": "Buy",
        "quantity": 10, "price": 180.5, "currency": "usd", "fee": 1.2,
        "note": "primera compra",
    }]))
    found = llm_map.extract("statement.pdf", b"%PDF", provider, "k")

    tx, = found.result.transactions
    assert found.kind == llm_map.KIND_TRADES
    assert (tx.date, tx.ticker, tx.action) == ("2024-01-02", "AAPL", "buy")
    assert (tx.quantity, tx.price, tx.fee, tx.currency) == (10.0, 180.5, 1.2, "USD")


def test_one_page_of_trades_outweighs_pages_of_holdings(monkeypatch):
    """A statement that opens with a valuation summary is still a statement."""
    _batches(monkeypatch, [POSITIONS_PAGE, TRADES_PAGE])
    replies = iter([_extraction("positions"),
                    _extraction("trades", [{"date": "2024-01-02",
                                            "ticker": "AAPL", "action": "buy",
                                            "quantity": 1, "price": 10}])])

    class _Sequence(_StubProvider):
        def complete(self, api_key, model, system, messages):
            self.calls.append(messages)
            return next(replies)

    found = llm_map.extract("mixed.pdf", b"%PDF", _Sequence(), "k")
    assert found.kind == llm_map.KIND_TRADES
    assert len(found.result.transactions) == 1


def test_each_page_block_is_its_own_call(monkeypatch):
    _batches(monkeypatch, [TRADES_PAGE] * 3)
    provider = _StubProvider(_extraction("trades"))
    llm_map.extract("long.pdf", b"%PDF", provider, "k")
    assert len(provider.calls) == 3


def test_a_block_the_model_chokes_on_costs_only_that_block(monkeypatch):
    _batches(monkeypatch, [TRADES_PAGE, TRADES_PAGE])
    replies = iter([RuntimeError("rate limited"),
                    _extraction("trades", [{"date": "2024-01-02",
                                            "ticker": "AAPL", "action": "buy",
                                            "quantity": 1, "price": 10}])])

    class _Flaky(_StubProvider):
        def complete(self, api_key, model, system, messages):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

    found = llm_map.extract("flaky.pdf", b"%PDF", _Flaky(), "k")
    assert len(found.result.transactions) == 1
    assert any(s["type"] == "batch" for s in found.result.skipped)


def test_a_document_past_the_call_limit_says_so(monkeypatch):
    """Bounded coverage must be reported, never silently truncated."""
    _batches(monkeypatch, [TRADES_PAGE] * (llm_map.MAX_PDF_CALLS + 2))
    provider = _StubProvider(_extraction("trades"))
    found = llm_map.extract("huge.pdf", b"%PDF", provider, "k")

    assert len(provider.calls) == llm_map.MAX_PDF_CALLS
    assert "2 more page block(s) not read" in found.result.skipped[-1]["reason"]


@pytest.mark.parametrize("record,why", [
    ({"date": "2024-01-02", "ticker": "AAPL", "action": "transfer"},
     "unknown action"),
    ({"date": "whenever", "ticker": "AAPL", "action": "buy"}, "unreadable date"),
    ({"date": "2024-01-02", "ticker": "", "action": "buy"}, "no symbol"),
    ("not even a dict", "not a record"),
])
def test_a_bad_extracted_record_is_dropped_with_a_reason(record, why, monkeypatch):
    """Whatever the model returns is re-checked before it can reach a ledger."""
    _batches(monkeypatch, [TRADES_PAGE])
    found = llm_map.extract("x.pdf", b"%PDF",
                            _StubProvider(_extraction("trades", [record])), "k")
    assert not found.result.transactions
    assert why in found.result.skipped[0]["reason"]


def test_extracted_numbers_are_coerced_not_trusted(monkeypatch):
    """A sell exported negative, and a price the model returned as a string."""
    _batches(monkeypatch, [TRADES_PAGE])
    found = llm_map.extract("x.pdf", b"%PDF", _StubProvider(_extraction(
        "trades", [{"date": "2024-01-02", "ticker": "AAPL", "action": "sell",
                    "quantity": -4, "price": "190.0", "fee": None}])), "k")
    tx, = found.result.transactions
    assert (tx.quantity, tx.price, tx.fee) == (4.0, 190.0, 0.0)


def test_a_dead_provider_is_not_reported_as_an_unreadable_file(monkeypatch):
    """"The assistant is down" and "your export makes no sense" need different
    answers — the second sends the user off to fix a file that is fine."""
    _batches(monkeypatch, [TRADES_PAGE, TRADES_PAGE])
    found = llm_map.extract("x.pdf", b"%PDF", _StubProvider(boom=True), "k")
    assert found.unavailable is True
    assert not found.result.transactions


def test_one_block_failing_is_not_an_outage(monkeypatch):
    _batches(monkeypatch, [TRADES_PAGE, TRADES_PAGE])
    replies = iter([RuntimeError("rate limited"), _extraction("trades")])

    class _Flaky(_StubProvider):
        def complete(self, api_key, model, system, messages):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

    assert llm_map.extract("x.pdf", b"%PDF", _Flaky(), "k").unavailable is False


def test_a_dead_provider_on_the_spreadsheet_path_too():
    found = llm_map.extract("extracto.csv", ES_CSV.encode(),
                            _StubProvider(boom=True), "k")
    assert found.unavailable is True
    assert "assistant is down" in found.result.skipped[0]["reason"]


def test_a_refused_mapping_is_not_an_outage():
    found = llm_map.extract("extracto.csv", ES_CSV.encode(),
                            _StubProvider('{"columns": null}'), "k")
    assert found.unavailable is False


def test_parse_extraction_survives_junk():
    assert llm_map.parse_extraction("sorry!") == ([], llm_map.KIND_NONE)
    assert llm_map.parse_extraction("{bad json") == ([], llm_map.KIND_NONE)
    assert llm_map.parse_extraction('{"kind": "wat"}') == ([], llm_map.KIND_NONE)


def test_a_pdf_with_no_text_is_reported(monkeypatch):
    _batches(monkeypatch, [])
    found = llm_map.extract("scan.pdf", b"%PDF", _StubProvider(""), "k")
    assert "no text could be read" in found.result.skipped[0]["reason"]


# --------------------------------------------------------------- end to end


def _reply(mapping: dict) -> str:
    return f"Here you go:\n```json\n{json.dumps(mapping)}\n```"


def test_parse_maps_once_however_long_the_file_is():
    rows = "".join(
        f"0{d % 9 + 1}/01/2024;AAPL;Compra;1;100,00;USD;0\n" for d in range(500)
    )
    data = (ES_CSV.rsplit("\n", 5)[0] + "\n" + rows).encode("utf-8")
    provider = _StubProvider(_reply(MAPPING))

    result = llm_map.parse("big.csv", data, provider, "key")

    assert len(result.transactions) == 500
    assert len(provider.calls) == 1  # the model saw a sample, not the file
    _, model, _, messages = provider.calls[0]
    assert model == "stub-mini"
    assert messages[0]["content"].count("row ") <= llm_map.SAMPLE_ROWS


def test_parse_reports_an_unreadable_file_instead_of_raising():
    result = llm_map.parse("notes.pdf", b"", _StubProvider(_reply(MAPPING)))
    assert not result.transactions
    assert "no text could be read" in result.skipped[0]["reason"]


def test_parse_reports_an_unreadable_spreadsheet():
    result = llm_map.parse("book.xlsx", b"nonsense", _StubProvider(_reply(MAPPING)))
    assert not result.transactions
    assert "no table" in result.skipped[0]["reason"]


def test_parse_reports_a_refused_mapping():
    result = llm_map.parse("extracto.csv", ES_CSV.encode(),
                           _StubProvider('{"columns": null}'))
    assert not result.transactions
    assert "columns could not be matched" in result.skipped[0]["reason"]


def test_parse_survives_a_dead_provider():
    result = llm_map.parse("extracto.csv", ES_CSV.encode(),
                           _StubProvider(boom=True))
    assert not result.transactions and result.skipped


def test_parse_reads_a_real_xlsx(tmp_path):
    """openpyxl is already a dependency (ClickTrade); the Excel path must use it."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    for row in [["Extracto"], ["Fecha", "Valor", "Operación", "Títulos",
                               "Precio", "Divisa", "Comisión"],
                ["02/01/2024", "AAPL", "Compra", "10", "180,50", "USD", "1,20"]]:
        sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)

    mapping = {**MAPPING, "header_row": 1}
    result = llm_map.parse("libro.xlsx", buf.getvalue(),
                           _StubProvider(_reply(mapping)))
    assert len(result.transactions) == 1
    assert result.transactions[0].price == 180.5
