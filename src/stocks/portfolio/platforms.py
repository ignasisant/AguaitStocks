"""Registry of statement platforms the Import page can parse.

One entry per broker/source: display label, accepted upload extensions, a
one-line exporting hint for the UI, and a parse callable with the uniform
signature ``parse(filename, data) -> ParseResult``. The page stays
platform-agnostic — adding a broker means writing its parser module and
appending a Platform here; validation, preview and commit are shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from stocks.portfolio import generic, revolut, revolut_crypto, revolut_pdf
from stocks.portfolio.revolut import ParseResult


@dataclass(frozen=True)
class Platform:
    key: str  # stable id, stored in the last-import record
    label: str
    file_types: tuple[str, ...]  # extensions st.file_uploader accepts
    hint: str  # one-liner: where to find the export on that platform
    parse: Callable[[str, bytes], ParseResult]  # (filename, raw bytes)


def _parse_revolut(filename: str, data: bytes) -> ParseResult:
    if filename.lower().endswith(".pdf"):
        return revolut_pdf.parse_pdf(data)
    return revolut.parse_csv(data.decode("utf-8-sig"))


def _parse_generic(filename: str, data: bytes) -> ParseResult:
    return generic.parse_csv(data.decode("utf-8-sig"))


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="revolut",
        label="Revolut",
        file_types=("csv", "pdf"),
        hint=(
            "Export from Revolut → Stocks → statement. CSV parses exactly; PDF "
            "is extracted from the transactions table and validated the same way."
        ),
        parse=_parse_revolut,
    ),
    Platform(
        key="revolut_crypto",
        label="Revolut crypto",
        file_types=("csv",),
        hint=(
            "Export from Revolut → Crypto → statement (CSV). Coins import as "
            "Yahoo pairs in the statement currency (BTC → BTC-EUR); rewards, "
            "transfers and coin-to-coin exchanges are listed as skipped."
        ),
        parse=lambda filename, data: revolut_crypto.parse_csv(
            data.decode("utf-8-sig")
        ),
    ),
    Platform(
        key="generic",
        label="Generic CSV",
        file_types=("csv",),
        hint=(
            "Any broker: a CSV with header "
            "`date,ticker,action,quantity,price,currency,fee,note` "
            "(action: buy/sell/dividend/fee/split; currency, fee, note optional)."
        ),
        parse=_parse_generic,
    ),
)


def by_key(key: str) -> Platform:
    """Look up a platform by its stable key; unknown keys fall back to Revolut
    (the only platform that existed before keys were recorded)."""
    for p in PLATFORMS:
        if p.key == key:
            return p
    return PLATFORMS[0]
