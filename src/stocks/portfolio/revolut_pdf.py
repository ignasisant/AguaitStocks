"""Parse a Revolut trading account-statement PDF into ledger Transactions.

Revolut renders the same statement as CSV or PDF; the PDF's "Transactions"
table carries the CSV columns (Date, Ticker, Type, Quantity, Price per share,
Total Amount, Currency, FX Rate) but as positioned text, so this module only
does *extraction* — each recognised line becomes a canonical row dict and is
fed through revolut.parse_rows, the exact pipeline the CSV uses. Row sanity
checks, skip reasons and fee derivation are therefore identical across both.

Extraction is anchor-based rather than positional: a line is a transaction iff
it contains a known Type keyword (BUY - MARKET, DIVIDEND, STOCK SPLIT, …).
Whatever sits left of the type is date + ticker; money and numbers to the
right map to quantity / price / total. Layout drift (fonts, column x-offsets,
page headers, the portfolio-breakdown section) is thus ignored for free.
Anything with a type keyword that still fails to parse lands in `skipped`
with a reason — never silently dropped. The CSV export stays the preferred
input; the PDF path exists for statements only kept as PDF.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from stocks.portfolio import revolut
from stocks.portfolio.statement import ParseResult

# The Type strings Revolut uses, longest-first so "DIVIDEND TAX (CORRECTION)"
# wins over "DIVIDEND". Matched case-insensitively, tolerant of -/– dashes.
_TYPE_PATTERNS = [
    r"DIVIDEND TAX \(CORRECTION\)",
    r"BUY\s*[-–]\s*(?:MARKET|LIMIT|STOP)",
    r"SELL\s*[-–]\s*(?:MARKET|LIMIT|STOP)",
    r"CASH TOP-?UP",
    r"CASH WITHDRAWAL",
    r"STOCK SPLIT",
    r"RETURN OF CAPITAL",
    r"CUSTODY FEE",
    r"DIVIDEND",
    r"REWARD",
    r"TRANSFER",
]
_TYPE_RE = re.compile("(" + "|".join(_TYPE_PATTERNS) + ")", re.IGNORECASE)

# Money renders symbol-first ("US$294.55", "USD 3,000.00", "€28.69") or, in
# European locales, symbol-last ("2.000,00 €"). One regex can't hold both:
# in "34.28 US$87.49" the symbol binds right (quantity, then prefix money) but
# in "28,69 € 2.000,00 €" it binds left — so the style is detected per line
# (which attachment occurs more often) and the matching regex is applied.
# Currency *codes* (USD, EUR…) always precede their number: that's how the
# Currency + FX Rate columns render ("USD 1.0427") in either locale.
_SYMBOLS = r"US\$|CA\$|A\$|HK\$|\$|€|£"
_CODES = r"(?<![A-Z])(?:USD|EUR|GBP|CHF|SEK|DKK|NOK|PLN|RON)(?![A-Z])"
_MONEY_PREFIX_RE = re.compile(rf"(?:{_SYMBOLS}|{_CODES})\s*(?P<num>-?\d[\d.,]*)")
_MONEY_SUFFIX_RE = re.compile(
    rf"(?:(?P<num>-?\d[\d.,]*)\s*(?:{_SYMBOLS}))|(?:{_CODES})\s*(?P<num2>-?\d[\d.,]*)"
)
_PREFIX_HINT = re.compile(rf"(?:{_SYMBOLS})\s?\d")
_SUFFIX_HINT = re.compile(rf"\d\s?(?:{_SYMBOLS})")


def _money_tokens(text: str) -> tuple[list[tuple[str, str]], list[tuple[int, int]]]:
    """(full_token, number_string) pairs plus their spans, style-aware."""
    prefix_style = len(_PREFIX_HINT.findall(text)) >= len(_SUFFIX_HINT.findall(text))
    pattern = _MONEY_PREFIX_RE if prefix_style else _MONEY_SUFFIX_RE
    hits = list(pattern.finditer(text))
    tokens = [
        (h.group(0), h.group("num") or (h.groupdict().get("num2") or ""))
        for h in hits
    ]
    return tokens, [h.span() for h in hits]
_NUMBER_RE = re.compile(r"-?\d[\d.,]*")
_CCY_RE = re.compile(r"\b(USD|EUR|GBP|CHF|SEK|DKK|NOK|PLN|RON)\b")
_SYMBOL_CCY = {"US$": "USD", "$": "USD", "€": "EUR", "£": "GBP"}

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}(?:[.\-][A-Z0-9]{1,4})?$")

# Month names as they appear in en and es statements ("24 feb. 2025").
_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "ene", "january", "enero"),
            ("feb", "february", "febrero"),
            ("mar", "march", "marzo"),
            ("apr", "abr", "april", "abril"),
            ("may", "mayo"),
            ("jun", "june", "junio"),
            ("jul", "july", "julio"),
            ("aug", "ago", "august", "agosto"),
            ("sep", "sept", "september", "septiembre"),
            ("oct", "october", "octubre"),
            ("nov", "november", "noviembre"),
            ("dec", "dic", "december", "diciembre"),
        ]
    )
    for m in names
}


def parse_pdf(source: str | Path | bytes) -> ParseResult:
    """Extract the transactions table from a Revolut statement PDF."""
    import pdfplumber  # heavy import, only needed on the PDF path

    opened = io.BytesIO(source) if isinstance(source, bytes) else source
    with pdfplumber.open(opened) as pdf:
        lines = [
            line
            for page in pdf.pages
            for line in (page.extract_text() or "").splitlines()
        ]
    return parse_lines(lines)


def parse_lines(lines: list[str]) -> ParseResult:
    """Anchor-based line extraction; separate from parse_pdf for testability."""
    rows = [_row_from_line(line, m) for line in lines if (m := _TYPE_RE.search(line))]
    return revolut.parse_rows(rows)


def _row_from_line(line: str, m: re.Match) -> dict:
    """Split one text line around the Type keyword into a canonical row dict."""
    left, right = line[: m.start()].strip(), line[m.end() :].strip()

    # Left side: "<date> <TICKER>" — ticker only when the trailing token looks
    # like one AND isn't the tail of the date (month names, 4-digit years).
    date_part, ticker = left, ""
    tokens = left.split()
    if tokens:
        tail = tokens[-1]
        if (
            _TICKER_RE.match(tail)
            and tail.lower().rstrip(".") not in _MONTHS
            and not re.fullmatch(r"\d{1,4}", tail)
        ):
            ticker, date_part = tail, " ".join(tokens[:-1])

    money, money_spans = _money_tokens(right)
    plain = [
        t.group(0)
        for t in _NUMBER_RE.finditer(right)
        if not any(s <= t.start() < e for s, e in money_spans)
    ]

    # Column order in the statement is Quantity, Price, Total, Currency, FX.
    # The Currency + FX Rate columns can render as "USD 1.0427", which the
    # money regex also matches — so tokens are assigned by column order, never
    # from the tail: trades read price then total; dividend/cash/split rows
    # have no price column, their first money token is the total.
    is_trade = m.group(0).upper().startswith(("BUY", "SELL"))
    quantity = plain[0] if plain and is_trade else ""
    if is_trade and len(money) >= 2:
        price, amount = money[0][1], money[1][1]
    elif money:
        price, amount = "", money[0][1]
    else:
        price = amount = ""
    if not is_trade and "SPLIT" in m.group(0).upper():
        quantity = plain[0] if plain else ""  # shares added by the split

    ccy = _CCY_RE.search(right)
    currency = ccy.group(1) if ccy else ""
    if not currency and money:
        for sym, code in _SYMBOL_CCY.items():
            if sym in money[0][0]:
                currency = code
                break

    return {
        "date": _to_iso(date_part),
        "ticker": ticker,
        "type": m.group(0).upper().replace("–", "-"),
        "quantity": _normalize_number(quantity),
        "price": _normalize_number(price),
        "amount": _normalize_number(amount),
        "currency": currency,
    }


def _to_iso(value: str) -> str:
    """Statement date in any supported rendering -> ISO YYYY-MM-DD.

    Unparseable strings pass through untouched: statement.parse_date will then
    reject the row into `skipped` with the original text in the reason.
    """
    v = value.strip().rstrip(",")
    head = v.split("T", 1)[0].split(" ", 1)[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
        return head
    # "24 feb. 2025" / "24 February 2025" (en + es month names)
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(\d{4})", v)
    if m and m.group(2).lower().rstrip(".") in _MONTHS:
        month = _MONTHS[m.group(2).lower().rstrip(".")]
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
    # "24/02/2025" — day-first (statement locale); month-first only when the
    # first field can't be a day. Truly ambiguous dates parse day-first and
    # the validation layer flags impossibilities (future dates etc.).
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        day, month = (a, b) if b <= 12 else (b, a)
        try:
            return datetime(y, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return value
    return value


def _normalize_number(token: str) -> str:
    """Normalise a number string to US format for statement.money.

    Handles both renderings Revolut uses: "1,723.00" (en) and "1.723,00" (es).
    Rule: when both separators appear, the last one is the decimal mark; a
    lone comma is a decimal mark unless it forms clean thousands groups.
    """
    t = token.strip()
    if not t:
        return t
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):  # 1.723,00 -> es style
            t = t.replace(".", "").replace(",", ".")
        else:  # 1,723.00 -> already US style
            t = t.replace(",", "")
    elif "," in t:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", t):  # 1,723 -> thousands
            t = t.replace(",", "")
        else:  # 28,69 -> decimal comma
            t = t.replace(",", ".")
    return t
