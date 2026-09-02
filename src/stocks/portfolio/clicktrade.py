"""Parse a ClickTrade / Saxo "Trades executed" report into ledger Transactions.

ClickTrade (iBroker) runs stocks on a Saxo Bank white-label, so its export is
the SaxoTraderGO **Operaciones ejecutadas / Trades executed** report (Informes
históricos → Operaciones ejecutadas), downloadable as Excel and sometimes CSV.
English and Spanish headers are recognised; accents are stripped before
matching so "Símbolo" and "Simbolo" both resolve. The parser refuses the whole
file unless the distinctive columns (date, buy/sell, quantity, price, currency
and a symbol or ISIN) are all found — a statement from another broker can
never be half-imported by accident.

Shape notes this parser absorbs:

* The Excel report has preamble rows (title, account, date range) above the
  table — the header row is located by scanning, not assumed to be first.
* Saxo symbols carry an exchange code ("TEF:xmce", "AAPL:xnas"). Known codes
  map to the Yahoo suffix (xmce → TEF.MC); unknown ones fall back to the ISIN
  as the ticker — map it to a Yahoo symbol under ``aliases:`` in
  watchlist.yaml (the established EU-broker-code mechanism).
* Buy vs sell is a B/S (C/V) column: "Bought"/"Compra" vs "Sold"/"Venta".
* Numbers may be locale-formatted ("1.234,56") in the CSV; Excel cells arrive
  as native numbers. Dates may be ISO, DD-MM-YYYY or Excel datetimes.
* Each row is cross-checked against its own "Traded value" column (qty ×
  price within 2%) so a mis-detected decimal separator cannot corrupt cost
  basis.
* Fees: an explicit costs/commission column is used when present; otherwise
  the fee is derived as |booked amount| − qty × price when that difference is
  small (≤ 2% of gross) — larger gaps mean the booked amount is in the
  account currency and the fee imports as 0 rather than mixing currencies.
* Non-stock rows (FxSpot, CFDs, futures…) are listed as skipped when the
  report carries an instrument-type column.
* Dividends are NOT in this report (they live in the account statement) —
  import them via the generic CSV or add manually.

Nothing here writes to the ledger; the Import page previews and commits.
"""

from __future__ import annotations

import csv
import io
import unicodedata
from datetime import date, datetime

from stocks.portfolio.degiro import _num
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult

# Logical key -> accepted header names, lowercased and accent-stripped.
_HEADERS = {
    "date": (
        "trade time", "trade date",
        "hora de la operacion", "fecha de la operacion", "fecha de operacion",
        "fecha valor", "fecha",
    ),
    "side": ("b/s", "c/v", "buy/sell", "compra/venta", "sentido"),
    "quantity": ("amount", "quantity", "shares", "cantidad", "titulos"),
    "price": ("price", "precio"),
    "currency": (
        "instrument currency", "currency",
        "divisa del instrumento", "divisa", "moneda",
    ),
    "symbol": ("instrument symbol", "simbolo del instrumento", "simbolo", "symbol"),
    "isin": ("instrument isin", "isin del instrumento", "isin"),
    "instrument": ("instrument", "instrumento"),
    "type": (
        "instrument type", "asset type", "product type",
        "tipo de instrumento", "tipo de activo", "tipo de producto",
    ),
    "traded_value": (
        "traded value", "trade value",
        "valor de la operacion", "valor negociado", "importe de la operacion",
    ),
    "booked": (
        "booked amount", "importe registrado", "importe contabilizado",
        "importe en cuenta",
    ),
    "costs": (
        "costs", "commission", "total costs",
        "comision", "comisiones", "costes", "gastos",
    ),
}
_REQUIRED = ("date", "side", "quantity", "price", "currency")

# Instrument-type values that are position-affecting stock/ETF trades; other
# types (FxSpot, CfdOnStock, FuturesContract…) are skipped, never imported.
_STOCK_TYPES = ("stock", "share", "etf", "etn", "equity", "accion", "fondo cotizado")

# Saxo exchange code (after the ":" in the symbol) -> Yahoo suffix.
EXCHANGE_SUFFIX = {
    "xnas": "", "xngs": "", "xnys": "", "xase": "", "arcx": "", "bats": "",
    "xmce": ".MC", "xmad": ".MC",
    "xetr": ".DE", "xeta": ".DE", "xfra": ".F",
    "xpar": ".PA", "xams": ".AS", "xbru": ".BR", "xlis": ".LS",
    "xmil": ".MI", "mtaa": ".MI",
    "xlon": ".L", "xswx": ".SW", "xvtx": ".SW",
    "xsto": ".ST", "xosl": ".OL", "xcse": ".CO", "xhel": ".HE",
    "xtse": ".TO", "xhkg": ".HK", "xtks": ".T", "xjpx": ".T",
}

_BUY_PREFIXES = ("b", "comp")
_SELL_PREFIXES = ("s", "vend", "vent")


def parse(filename: str, data: bytes) -> ParseResult:
    """Parse a ClickTrade/Saxo trades report (xlsx or csv) — no side effects."""
    if filename.lower().endswith(".xlsx"):
        rows = _xlsx_rows(data)
    else:
        rows = _csv_rows(data.decode("utf-8-sig"))
    return _parse_rows(rows)


def _xlsx_rows(data: bytes) -> list[list[str]]:
    import openpyxl  # deferred: only xlsx uploads pay the import

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return [[_cell_text(c) for c in row] for row in wb.worksheets[0].iter_rows()]
    finally:
        wb.close()


def _cell_text(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, datetime | date):
        return v.isoformat()
    return str(v).strip()


def _csv_rows(text: str) -> list[list[str]]:
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _parse_rows(rows: list[list[str]]) -> ParseResult:
    header_at, idx = _find_header(rows)
    if idx is None:
        return ParseResult(skipped=[{
            "row": 1,
            "type": "header",
            "reason": (
                "not a ClickTrade/Saxo trades report — no row has the "
                "expected columns (date, B/S, quantity, price, currency, "
                "symbol/ISIN)"
            ),
        }])

    result = ParseResult()
    for i, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
        if not any(c.strip() for c in row):
            continue  # blank line
        kind = _cell(row, idx.get("type"))
        if kind and not _is_stock(kind):
            result.skipped.append(_skip(row, idx, i, kind, "not a stock/ETF trade"))
            continue
        try:
            result.transactions.append(_build_tx(row, idx))
        except (IndexError, ValueError) as exc:
            result.skipped.append(_skip(row, idx, i, kind or "trade", str(exc)))
    return result


def _find_header(
    rows: list[list[str]],
) -> tuple[int, dict[str, int] | None]:
    """Locate the table header: first row (within the preamble budget) whose
    cells resolve every required column plus a symbol or ISIN."""
    for pos, row in enumerate(rows[:30]):
        idx: dict[str, int] = {}
        for col, cell in enumerate(row):
            name = _norm(cell)
            for key, names in _HEADERS.items():
                if key not in idx and name in names:
                    idx[key] = col
        if all(k in idx for k in _REQUIRED) and ("symbol" in idx or "isin" in idx):
            return pos, idx
    return 0, None


def _norm(value: str) -> str:
    """Lowercase, strip accents and collapse whitespace for header matching."""
    v = unicodedata.normalize("NFD", value.strip().lower())
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    return " ".join(v.split())


def _cell(row: list[str], pos: int | None) -> str:
    return row[pos].strip() if pos is not None and pos < len(row) else ""


def _is_stock(kind: str) -> bool:
    k = _norm(kind)
    return any(t in k for t in _STOCK_TYPES)


def _skip(
    row: list[str], idx: dict[str, int], line: int, kind: str, reason: str
) -> dict:
    return {
        "row": line,
        "type": kind,
        "reason": reason,
        "date": _cell(row, idx.get("date")),
        "ticker": (_cell(row, idx.get("symbol")) or _cell(row, idx.get("isin"))).upper(),
        "quantity": _num(_cell(row, idx.get("quantity"))),
        "amount": 0.0,
        "currency": _cell(row, idx.get("currency")).upper(),
    }


def _build_tx(row: list[str], idx: dict[str, int]) -> Transaction:
    ticker = _resolve_ticker(
        _cell(row, idx.get("symbol")), _cell(row, idx.get("isin"))
    )
    action = _side(_cell(row, idx["side"]))
    date_ = _parse_date(_cell(row, idx["date"]))
    qty = abs(_num(_cell(row, idx["quantity"])))
    if qty == 0:
        raise ValueError("row has no quantity")
    price = _num(_cell(row, idx["price"]))
    if price <= 0:
        raise ValueError(f"row has non-positive price {price:g}")
    currency = _cell(row, idx["currency"]).upper()
    if not currency:
        raise ValueError("row has no currency")
    gross = qty * price

    # Guard against decimal-separator misreads: qty × price must match the
    # statement's own traded-value column within 2%.
    traded = abs(_num(_cell(row, idx.get("traded_value"))))
    if traded > 0 and abs(gross - traded) > traded * 0.02:
        raise ValueError(
            f"row inconsistent: {qty:g} × {price:g} = {gross:.2f} "
            f"but traded value is {traded:.2f}"
        )

    fee = abs(_num(_cell(row, idx.get("costs"))))
    if fee == 0:
        # Booked amount = gross ± costs when booked in the trade currency; a
        # difference beyond 2% means it's in the account currency — leave 0.
        booked = abs(_num(_cell(row, idx.get("booked"))))
        if booked > 0 and abs(booked - gross) <= gross * 0.02:
            fee = round(abs(booked - gross), 6)

    return Transaction(
        date=date_,
        ticker=ticker,
        action=action,
        quantity=qty,
        price=price,
        currency=currency,
        fee=fee,
        note=f"clicktrade {_cell(row, idx.get('instrument'))}".strip(),
    )


def _resolve_ticker(symbol: str, isin: str) -> str:
    """Saxo "TEF:xmce" -> "TEF.MC"; unknown exchange (or no symbol) -> ISIN."""
    isin = isin.strip().upper()
    sym, _, exchange = symbol.strip().upper().partition(":")
    if sym:
        suffix = EXCHANGE_SUFFIX.get(exchange.lower()) if exchange else ""
        if suffix is not None:
            return sym + suffix
    if isin:
        return isin
    if sym:
        return sym  # unknown exchange, no ISIN — validation will flag it
    raise ValueError("row has neither symbol nor ISIN")


def _side(value: str) -> str:
    v = _norm(value)
    if v.startswith(_BUY_PREFIXES):
        return "buy"
    if v.startswith(_SELL_PREFIXES):
        return "sell"
    raise ValueError(f"unrecognised buy/sell value {value!r}")


def _parse_date(value: str) -> str:
    """ISO (with or without time), DD-MM-YYYY or DD/MM/YYYY to YYYY-MM-DD."""
    v = value.strip().split("T", 1)[0].split(" ", 1)[0]
    if not v:
        raise ValueError("missing date")
    parts = v.replace("/", "-").split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        if len(parts[2]) == 4:
            return f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"
    raise ValueError(f"unrecognised date {value!r}")
