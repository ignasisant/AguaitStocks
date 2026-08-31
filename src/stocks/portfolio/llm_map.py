"""Import a broker export nobody wrote a parser for, by mapping its columns.

The dedicated parsers (revolut, degiro, ibkr, …) all key off exact headers, so
an export from a broker we don't cover — or a spreadsheet the user keeps by
hand — has nowhere to go. This module fills that gap: the file is read into a
raw grid of text cells, a *sample* of it goes to the model, and what comes
back is a **mapping**, not data:

    {"header_row": 3,
     "columns": {"date": 0, "ticker": 2, "action": 4, "quantity": 5,
                 "price": 6, "currency": 7, "fee": 8, "note": null},
     "date_format": "%d/%m/%Y", "decimal": ",", "thousands": ".",
     "action_map": {"Compra": "buy", "Venta": "sell", "Dividendo": "dividend"}}

The mapping is then applied to every row **in Python**. That split is the
whole point: the model never transcribes a price or a date, so it cannot
hallucinate one into the ledger, and a 10-row file costs exactly as much to
map as a 2000-row one. Columns are addressed by index rather than by header
text — no fuzzy matching, and duplicate or blank headers stay unambiguous.

PDFs get the other treatment. A PDF has no regular grid to map: instrument
names wrap over two lines, the table breaks across pages, and hundreds of
cover-page and footer lines sit around it. So for PDFs the model *extracts*
the transactions themselves, a few pages per call. That is a deliberate
exception to the rule above, and the guard against a hallucinated price is the
mandatory preview: nothing an extraction produces reaches the ledger until the
user has looked at the table and pressed the button.

Either way, rows that can't be turned into a Transaction land in
``ParseResult.skipped`` with a reason, never silently dropped — the same
contract generic.py has. Semantic checks (future dates, oversells, unknown
tickers) stay in validate.py, so an LLM-read import is quarantined exactly
like a Revolut one. Nothing here writes to the ledger.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from stocks.portfolio.ledger import ACTIONS, Transaction
from stocks.portfolio.revolut import ParseResult

if TYPE_CHECKING:
    from stocks.web.llm import Provider

# What the model is shown. Enough rows to see the shape of the data (and any
# preamble above the header), few enough to stay a cheap classifier call.
SAMPLE_ROWS = 18
MAX_COLS = 30
MAX_CELL = 40

FIELDS = ("date", "ticker", "action", "quantity", "price", "currency", "fee", "note")
_REQUIRED = ("date", "ticker", "action")

# Tried in order when the model's date_format doesn't parse a cell — brokers
# mix these freely and one bad guess must not reject the whole file.
_DATE_FALLBACKS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y",
)

# Everything that is decoration around a number: currency symbols and codes,
# spaces (including the non-breaking kind Excel exports love), quotes.
_MONEY_JUNK = re.compile(r"[^\d,.\-+()]")

# What a document turned out to be. "positions" matters on its own: a
# portfolio report lists what you hold right now with no dated buys or sells,
# so there is genuinely nothing to import, and saying so beats "unreadable".
KIND_TRADES = "trades"
KIND_POSITIONS = "positions"
KIND_NONE = "none"

# PDF extraction budget. Pages are batched up to this many characters per
# call (~3k tokens of statement text), and no document costs more than
# MAX_PDF_CALLS calls however long it is — the tail is reported, not dropped.
PDF_CHARS_PER_CALL = 12_000
MAX_PDF_CALLS = 8


class ProviderUnavailable(Exception):
    """The model could not be reached.

    Kept apart from every other failure on purpose: "the assistant is down"
    and "this file makes no sense" look identical in a ParseResult, and
    telling the user to fix their export when the real problem is a dead API
    key sends them off to do the wrong work.
    """


def _ask(provider: Provider, api_key: str, system: str, content: str) -> str:
    try:
        return provider.complete(
            api_key,
            provider.classifier_model or provider.default_model,
            system,
            [{"role": "user", "content": content}],
        )
    except Exception as exc:
        raise ProviderUnavailable(str(exc)) from exc


@dataclass(frozen=True)
class Extraction:
    """What came out of a file, and what the file turned out to be."""

    result: ParseResult = field(default_factory=ParseResult)
    kind: str = KIND_NONE
    unavailable: bool = False  # the model never answered; the file is unjudged


# --------------------------------------------------------------- file to grid


def _cells(row) -> list[str]:
    out = []
    for c in row:
        if c is None:
            out.append("")
        else:
            text = str(c).strip()
            out.append("" if text.lower() in ("nan", "nat", "none") else text)
    return out


def _read_csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    # European exports are semicolon-delimited as often as comma-delimited;
    # sniffing beats guessing, and a failed sniff falls back to comma.
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [_cells(r) for r in csv.reader(io.StringIO(text), dialect)]


def _read_excel(data: bytes) -> list[list[str]]:
    import pandas as pd

    frame = pd.read_excel(io.BytesIO(data), header=None, dtype=str)
    return [_cells(r) for r in frame.itertuples(index=False, name=None)]


def _words_grid(page, ytol: float = 3.0, xgap: float = 6.0) -> list[list[str]]:
    """Rows rebuilt from where the words sit on the page.

    Words sharing a baseline (within ytol points) form a row; a horizontal gap
    wider than xgap starts a new cell. This is the only way to read the many
    statement PDFs that *position* their text instead of drawing a table grid
    — pdfplumber's table finder needs ruling lines and returns nothing at all
    for those, so without this they extract as an empty file.

    Text wrapped over two lines inside one visual row (a long instrument name)
    lands on its own row here; the mapping downstream skips such rows with a
    reason rather than mangling them.
    """
    lines: dict[int, list] = {}
    for word in page.extract_words():
        lines.setdefault(round(word["top"] / ytol), []).append(word)

    grid = []
    for key in sorted(lines):
        cells: list[str] = []
        current: list[str] = []
        prev_right = None
        for word in sorted(lines[key], key=lambda w: w["x0"]):
            if prev_right is not None and word["x0"] - prev_right > xgap:
                cells.append(" ".join(current))
                current = []
            current.append(word["text"])
            prev_right = word["x1"]
        if current:
            cells.append(" ".join(current))
        grid.append(cells)
    return grid


def _read_pdf(data: bytes) -> list[list[str]]:
    """The PDF's transaction table, however it is drawn.

    Ruled tables are preferred: the widest one is kept and stacked across
    pages (a statement's table breaks over pages, while tables of a different
    column count are page furniture — headers, summary boxes). A PDF with no
    ruling lines at all falls back to reading word positions.
    """
    import pdfplumber

    tables: list[list[list[str]]] = []
    positioned: list[list[str]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            found = page.extract_tables() or []
            tables.extend(found)
            if not found:
                positioned.extend(_words_grid(page))
    if not tables:
        return [_cells(r) for r in positioned]
    widths: dict[int, int] = {}
    for t in tables:
        for row in t:
            widths[len(row)] = widths.get(len(row), 0) + 1
    best = max(widths, key=lambda w: (widths[w] * w, w))
    return [_cells(r) for t in tables for r in t if len(r) == best]


def read_grid(filename: str, data: bytes) -> list[list[str]]:
    """The file as raw text cells, header row not yet assumed.

    Empty when the format isn't readable here or the file has no table at all;
    the caller turns that into a skip reason rather than an exception.
    """
    name = filename.lower()
    try:
        if name.endswith(".pdf"):
            grid = _read_pdf(data)
        elif name.endswith((".xlsx", ".xlsm")):
            grid = _read_excel(data)
        else:
            grid = _read_csv(data)
    except Exception:
        return []
    return [r for r in grid if any(c for c in r)]  # drop blank lines


# ------------------------------------------------------------------- mapping


def sample(grid: list[list[str]]) -> str:
    """The first rows, indexed, as the model sees them."""
    lines = []
    for i, row in enumerate(grid[:SAMPLE_ROWS]):
        cells = [c[:MAX_CELL] for c in row[:MAX_COLS]]
        lines.append(f"row {i}: " + " | ".join(cells))
    return "\n".join(lines)


_SYSTEM = """You map a broker's transaction export onto a fixed ledger schema.
You are shown the first rows of the file, each cell separated by " | " and
numbered by row and (implicitly) by column, starting at 0.

Reply with ONLY a JSON object, no prose, no code fences:
{"header_row": <index of the row holding the column headers>,
 "columns": {"date": <column index>, "ticker": <index>, "action": <index>,
             "quantity": <index or null>, "price": <index or null>,
             "currency": <index or null>, "fee": <index or null>,
             "note": <index or null>},
 "date_format": "<strftime format of the date cells, e.g. %d/%m/%Y>",
 "decimal": "<the decimal separator, '.' or ','>",
 "thousands": "<the thousands separator, or an empty string>",
 "action_map": {"<the exact text in the action column>":
                "<buy|sell|dividend|fee|split>"}}

Rules:
- Column indexes are 0-based positions in the " | " list, NOT header names.
- Required: date, ticker, action. If the file has no column that identifies
  the security, or no column that says what happened, reply {"columns": null}.
- "ticker": the symbol column. If the file only has an ISIN, map that column —
  it is imported as-is and mapped to a symbol later.
- "price" is per share. For a dividend row, map the total amount column to
  "price". For a split, "quantity" is the ratio.
- "action_map" needs one entry per distinct value you can see in the action
  column, including the ones you would ignore — map those to the closest of
  buy/sell/dividend/fee/split, and leave out only values that are clearly not
  transactions (cash top-ups, transfers, balance lines).
- Never invent values from the sample rows; you are only naming columns.
"""


def _index(value, width: int) -> int | None:
    """A column index from the model, or None when it isn't usable."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < width else None


def parse_mapping(raw: str, grid: list[list[str]]) -> dict | None:
    """A validated mapping out of the model's reply, or None.

    None means "this file can't be mapped" — a missing required column, an
    out-of-range index, unparseable JSON. The caller reports that rather than
    importing a half-understood file.
    """
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("columns"), dict):
        return None

    width = max((len(r) for r in grid), default=0)
    header_row = _index(data.get("header_row"), len(grid))
    columns = {
        f: _index(data["columns"].get(f), width) for f in FIELDS
    }
    if any(columns[f] is None for f in _REQUIRED):
        return None

    actions = {}
    for key, value in (data.get("action_map") or {}).items():
        value = str(value).strip().lower()
        if value in ACTIONS:
            actions[str(key).strip().lower()] = value

    decimal = str(data.get("decimal") or ".")[:1] or "."
    thousands = str(data.get("thousands") or "")[:1]
    return {
        "header_row": 0 if header_row is None else header_row,
        "columns": columns,
        "date_format": str(data.get("date_format") or "").strip(),
        "decimal": decimal,
        # A separator that is also the decimal point would eat the decimals.
        "thousands": "" if thousands == decimal else thousands,
        "action_map": actions,
    }


def map_columns(provider: Provider, api_key: str,
                grid: list[list[str]]) -> dict | None:
    """Ask the provider's cheapest model how this file is laid out.

    None means the model looked and could not match the columns. A model that
    could not be reached raises ProviderUnavailable instead — the two need
    different answers.
    """
    return parse_mapping(_ask(provider, api_key, _SYSTEM, sample(grid)), grid)


# ------------------------------------------------------------------ applying


def _number(text: str, decimal: str, thousands: str) -> float | None:
    """A float out of a broker's formatting, or None.

    Handles currency symbols and codes, thin/non-breaking spaces, and the
    accountancy convention of wrapping negatives in parentheses.
    """
    raw = _MONEY_JUNK.sub("", text or "")
    if not raw:
        return None
    negative = raw.startswith("-") or ("(" in raw and ")" in raw)
    raw = raw.strip("()+-")
    if thousands:
        raw = raw.replace(thousands, "")
    if decimal != ".":
        raw = raw.replace(decimal, ".")
    if raw.count(".") > 1:
        # More than one separator survived, so the thousands one was never
        # declared. A trailing group of exactly three digits is a thousands
        # group ("1.234.567"); anything else is the decimal part ("1.234.56").
        head, _, tail = raw.rpartition(".")
        raw = head.replace(".", "") + ("" if len(tail) == 3 else ".") + tail
    try:
        value = float(raw)
    except ValueError:
        return None
    return -value if negative else value


def _date(text: str, fmt: str) -> str | None:
    """An ISO date out of a cell, trying the mapped format then the usual ones."""
    text = (text or "").strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0] if len(text) > 10 else text
    for candidate in ([fmt] if fmt else []) + list(_DATE_FALLBACKS):
        try:
            return datetime.strptime(text, candidate).date().isoformat()
        except (ValueError, TypeError):
            continue
    return None


def apply_mapping(grid: list[list[str]], mapping: dict) -> ParseResult:
    """Turn every data row into a Transaction using the mapping. No LLM here."""
    result = ParseResult()
    cols = mapping["columns"]
    actions = mapping["action_map"]
    decimal, thousands = mapping["decimal"], mapping["thousands"]

    def cell(row: list[str], field: str) -> str:
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else ""

    for lineno, row in enumerate(grid[mapping["header_row"] + 1:],
                                 start=mapping["header_row"] + 2):
        raw_action = cell(row, "action").strip()
        if not any(cell(row, f) for f in _REQUIRED):
            continue  # separator / totals line
        action = actions.get(raw_action.lower()) or (
            raw_action.lower() if raw_action.lower() in ACTIONS else ""
        )
        if not action:
            result.skipped.append({
                "row": lineno, "type": raw_action or "?",
                "reason": "action not recognised",
            })
            continue

        day = _date(cell(row, "date"), mapping["date_format"])
        if day is None:
            result.skipped.append({
                "row": lineno, "type": raw_action,
                "reason": f"unreadable date {cell(row, 'date')!r}",
            })
            continue

        ticker = cell(row, "ticker").strip().upper()
        if not ticker:
            result.skipped.append({
                "row": lineno, "type": raw_action, "reason": "no symbol",
            })
            continue

        quantity = _number(cell(row, "quantity"), decimal, thousands) or 0.0
        price = _number(cell(row, "price"), decimal, thousands) or 0.0
        fee = _number(cell(row, "fee"), decimal, thousands) or 0.0
        currency = (cell(row, "currency").strip().upper() or "USD")[:3]
        try:
            result.transactions.append(Transaction(
                date=day, ticker=ticker, action=action,
                # The action carries the direction; a sell exported as a
                # negative quantity must not import as a negative position.
                quantity=abs(quantity), price=abs(price), currency=currency,
                fee=abs(fee), note=cell(row, "note").strip()[:120],
            ))
        except ValueError as exc:
            result.skipped.append({
                "row": lineno, "type": raw_action, "reason": str(exc)})
    return result


# ------------------------------------------------------------ pdf extraction


_PDF_SYSTEM = """You read a broker document and pull out its transactions.
You are shown some pages of it as text; each line is one row of the page and
" | " separates what sat in separate columns.

Reply with ONLY a JSON object, no prose, no code fences:
{"kind": "trades" | "positions" | "none",
 "transactions": [{"date": "YYYY-MM-DD", "ticker": "<symbol or ISIN>",
                   "action": "buy|sell|dividend|fee|split",
                   "quantity": <number>, "price": <number>,
                   "currency": "<3-letter code>", "fee": <number>,
                   "note": "<short label, optional>"}]}

"kind" describes these pages:
- "trades": they contain dated buys, sells, dividends or fees.
- "positions": they only state what is held right now (a portfolio report,
  a holdings summary, a valuation) with no dated movements. Return an empty
  "transactions" list — a holding is not a transaction.
- "none": no financial records here (cover page, cost glossary, disclaimer).

Rules:
- Copy values from the page. Never infer, average or complete a number, and
  never carry one row's value into another. Omit a row you cannot read in
  full rather than guessing at it.
- "date" is when the trade happened, as YYYY-MM-DD. A row with no date is not
  a transaction — leave it out.
- "ticker": the symbol if the document shows one, otherwise the ISIN.
- "quantity" and "price" are per share, unsigned; the action carries the
  direction. For a dividend, "price" is the total amount received.
- "currency" is the instrument's currency, not the account's, when they differ.
- An empty "transactions" list is a fine answer.
"""


def _page_text(grid: list[list[str]]) -> str:
    return "\n".join(" | ".join(c for c in row if c) for row in grid if any(row))


def _pdf_batches(data: bytes) -> list[str]:
    """The PDF's pages as text, grouped into per-call batches.

    Batched by character budget rather than page count because statement
    pages vary hugely in density, and a page is never split across calls — a
    transaction cut in half would be read wrong by both calls.
    """
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            rows = page.extract_tables() or []
            text = (_page_text([_cells(r) for t in rows for r in t]) if rows
                    else _page_text(_words_grid(page)))
            if text.strip():
                pages.append(text)

    batches: list[str] = []
    current: list[str] = []
    size = 0
    for text in pages:
        if current and size + len(text) > PDF_CHARS_PER_CALL:
            batches.append("\n".join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append("\n".join(current))
    return batches


def _transaction_from(raw: dict) -> tuple[Transaction | None, str]:
    """One extracted record as a Transaction, or None plus why not.

    Everything the model returns is re-checked here: an out-of-schema action,
    an unparseable date or a non-numeric amount drops the row instead of
    reaching the ledger.
    """
    if not isinstance(raw, dict):
        return None, "not a record"
    action = str(raw.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return None, f"unknown action {action or '?'}"
    day = _date(str(raw.get("date") or ""), "%Y-%m-%d")
    if day is None:
        return None, f"unreadable date {raw.get('date')!r}"
    ticker = str(raw.get("ticker") or "").strip().upper()
    if not ticker:
        return None, "no symbol"

    def number(key: str) -> float:
        try:
            return abs(float(raw.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    try:
        return Transaction(
            date=day, ticker=ticker, action=action,
            quantity=number("quantity"), price=number("price"),
            currency=(str(raw.get("currency") or "USD").strip().upper() or "USD")[:3],
            fee=number("fee"), note=str(raw.get("note") or "").strip()[:120],
        ), ""
    except ValueError as exc:
        return None, str(exc)


def parse_extraction(raw: str) -> tuple[list[dict], str]:
    """(records, kind) out of one extraction reply; ([], KIND_NONE) on junk."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return [], KIND_NONE
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return [], KIND_NONE
    if not isinstance(data, dict):
        return [], KIND_NONE
    kind = str(data.get("kind") or KIND_NONE).strip().lower()
    if kind not in (KIND_TRADES, KIND_POSITIONS, KIND_NONE):
        kind = KIND_NONE
    records = data.get("transactions")
    return (records if isinstance(records, list) else []), kind


def extract_pdf(data: bytes, provider: Provider, api_key: str = "") -> Extraction:
    """Have the model read a PDF's transactions out of it, batch by batch.

    Batches are independent calls, so one page the model chokes on costs that
    page and not the document. The document's kind is the strongest thing any
    batch reported: one page of real trades makes it a statement, and failing
    that, one page of holdings makes it a portfolio report.
    """
    try:
        batches = _pdf_batches(data)
    except Exception:
        batches = []
    if not batches:
        return Extraction(ParseResult(skipped=[{
            "row": 0, "type": "file",
            "reason": "no text could be read from this PDF",
        }]))

    result = ParseResult()
    kind = KIND_NONE
    dropped = len(batches) - MAX_PDF_CALLS
    attempted = min(len(batches), MAX_PDF_CALLS)
    unreachable = 0
    for index, batch in enumerate(batches[:MAX_PDF_CALLS], start=1):
        try:
            reply = _ask(provider, api_key, _PDF_SYSTEM, batch)
        except ProviderUnavailable:
            unreachable += 1
            result.skipped.append({
                "row": index, "type": "batch",
                "reason": f"pages in block {index} could not be read",
            })
            continue
        records, batch_kind = parse_extraction(reply)
        if batch_kind == KIND_TRADES or (
            batch_kind == KIND_POSITIONS and kind != KIND_TRADES
        ):
            kind = batch_kind
        for record in records:
            tx, why = _transaction_from(record)
            if tx is None:
                result.skipped.append({"row": index, "type": "row", "reason": why})
            else:
                result.transactions.append(tx)

    if dropped > 0:  # never a silent truncation
        result.skipped.append({
            "row": 0, "type": "file",
            "reason": f"{dropped} more page block(s) not read — the document is "
                      f"longer than the {MAX_PDF_CALLS}-block limit",
        })
    # Every block failing means the model was never reached, so nothing has
    # been learned about the document — that is not "an unreadable PDF".
    return Extraction(result, KIND_TRADES if result.transactions else kind,
                      unavailable=unreachable == attempted)


# ------------------------------------------------------------------ entry


def extract(filename: str, data: bytes, provider: Provider,
            api_key: str = "") -> Extraction:
    """Read one unrecognised export, by whichever route its format needs."""
    if filename.lower().endswith(".pdf"):
        return extract_pdf(data, provider, api_key)

    grid = read_grid(filename, data)
    if len(grid) < 2:
        return Extraction(ParseResult(skipped=[{
            "row": 0, "type": "file",
            "reason": "no table could be read from this file",
        }]))
    try:
        mapping = map_columns(provider, api_key, grid)
    except ProviderUnavailable as exc:
        return Extraction(ParseResult(skipped=[{
            "row": 0, "type": "file", "reason": f"the assistant is down: {exc}",
        }]), unavailable=True)
    if mapping is None:
        return Extraction(ParseResult(skipped=[{
            "row": 0, "type": "file",
            "reason": "the columns could not be matched to date/symbol/action",
        }]))
    result = apply_mapping(grid, mapping)
    return Extraction(result, KIND_TRADES if result.transactions else KIND_NONE)


def parse(filename: str, data: bytes, provider: Provider,
          api_key: str = "") -> ParseResult:
    """extract() without the document kind, for callers that don't need it."""
    return extract(filename, data, provider, api_key).result
