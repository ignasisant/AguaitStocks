"""Work out which parser an uploaded statement needs, without asking.

The Import page has the user pick their broker first; the assistant can't —
a file arrives in chat with nothing but its name. So every registered parser
whose extensions match is simply *tried*, and the first one that yields
transactions wins.

Order is by strictness, not by registry position. Most parsers key off exact
headers (Trading 212, DEGIRO, IBKR, ClickTrade) and decline anything they
don't own, so they go first in whatever order the registry lists them. Three
resolve their columns by *alias* and are permissive enough to claim each
other's files — generic.py, Revolut stocks and Revolut crypto — so they go
last, and only after their distinctive header has been sighted. That check is
not cosmetic: without it a Revolut *stock* statement is claimed by the crypto
parser, which imports NVO as the pair NVO-USD, and a ledger-format CSV is
claimed by the stock one, which drops its fees and notes.

That is the whole reason detection can't just reuse the registry order the
Import page shows: there the user has already said which broker it is.

Whatever no parser claims falls through to llm_map: one cheap call names the
columns, and the rows are converted in Python. That fallback is the reason an
arbitrary broker Excel or a hand-kept spreadsheet can be imported at all.

Detection is read-only — the caller still validates (validate.py) and previews
before anything reaches the ledger, exactly as the Import page does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from stocks.portfolio import llm_map, platforms
from stocks.portfolio.revolut import ParseResult

if TYPE_CHECKING:
    from stocks.web.llm import Provider

LLM_KEY = "llm"  # the "platform" recorded for a mapped import

# Parsers that accept a file on loose evidence, tried after every strict one.
# Anything not listed here is strict by assumption, which is the safe default:
# a new parser is tried early and simply declines files it doesn't own.
_LOOSE = ("generic", "revolut", "revolut_crypto")

# Header that must be present before a permissive parser is even tried — the
# one column that tells these three formats apart, since their alias resolvers
# won't. Checked on CSV headers only; the strict parsers need no fingerprint,
# and a file whose header can't be read is passed through rather than blocked.
_FINGERPRINT = {
    "generic": {"date", "ticker", "action"},
    "revolut": {"ticker", "price per share"},
    "revolut_crypto": {"symbol", "value"},
}


@dataclass(frozen=True)
class Detected:
    """What the file turned out to be, and what came out of it."""

    result: ParseResult
    platform: str  # a platforms.py key, or LLM_KEY
    label: str  # display name for the preview ("Trading 212", "Column mapping")
    # What the document turned out to be (llm_map.KIND_*). "positions" is the
    # one worth telling the user about: a portfolio report holds no dated
    # movements, so "nothing to import" is the right answer, not a failure.
    kind: str = llm_map.KIND_NONE
    # True when the model was never reached, so the file was never judged.
    unavailable: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.result.transactions)


def _extension(filename: str) -> str:
    _, _, ext = filename.lower().rpartition(".")
    return ext


def _headers(filename: str, data: bytes) -> set[str]:
    """The CSV's header names, lowercased — empty for anything else."""
    if not filename.lower().endswith(".csv"):
        return set()
    grid = llm_map.read_grid(filename, data)
    return {c.strip().lower() for c in (grid[0] if grid else [])}


def _cascade() -> list:
    """Every platform, strict parsers first, the permissive ones last."""
    by_key = {p.key: p for p in platforms.PLATFORMS}
    strict = [p for p in platforms.PLATFORMS if p.key not in _LOOSE]
    return strict + [by_key[k] for k in _LOOSE if k in by_key]


def detect(filename: str, data: bytes, provider: Provider | None = None,
           api_key: str = "") -> Detected:
    """Parse an uploaded statement with whichever parser understands it.

    `provider` enables the column-mapping fallback; without one, an
    unrecognised file comes back empty with a skip reason, never guessed at.
    """
    ext = _extension(filename)
    head = _headers(filename, data)
    for platform in _cascade():
        if ext not in platform.file_types:
            continue
        need = _FINGERPRINT.get(platform.key)
        if need and head and not need <= head:
            continue
        try:
            result = platform.parse(filename, data)
        except Exception:
            continue  # a parser that chokes has simply declined the file
        if result.transactions:
            return Detected(result, platform.key, platform.label,
                            llm_map.KIND_TRADES)

    if provider is None:
        return Detected(
            ParseResult(skipped=[{
                "row": 0, "type": "file",
                "reason": "no parser recognised this file",
            }]),
            LLM_KEY, "",
        )
    found = llm_map.extract(filename, data, provider, api_key)
    return Detected(found.result, LLM_KEY, "", found.kind, found.unavailable)


def supported_types() -> tuple[str, ...]:
    """Every extension any parser accepts — what the uploader should allow."""
    seen = {t for p in platforms.PLATFORMS for t in p.file_types}
    seen.update({"xlsx", "pdf", "csv"})  # llm_map reads these even unrecognised
    return tuple(sorted(seen))
