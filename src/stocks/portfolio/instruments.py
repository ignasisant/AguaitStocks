"""Ask the model what an instrument's ticker is.

Broker documents identify a holding however they like: a company name ("Meta
Platforms Inc.", "LVMH Moet Hennessy Louis Vuitton"), an ISIN
("US0231351067"), a venue-qualified symbol ("ZBRA:xnas"), and often several of
those in one file. The ledger wants one thing — the Yahoo Finance ticker the
rest of the app prices with — and extraction copies whatever the page said,
so something has to bridge the two.

That something is the model, not a table. Knowing that US0231351067 is Amazon,
that LVMH trades as MC.PA and that PDD's ADR is PDD is exactly what a language
model already holds and what a lookup file can only ever hold a slice of; the
slice is what made this fail on a French listing and on every ISIN. So the
labels a document actually used are collected and sent in one small call, and
the reply is a label -> ticker map.

The reply is not trusted blindly — a value that isn't ticker-shaped is
discarded, and anything the model declines to name is left exactly as the
document wrote it, so validate.py rejects that row by name and the preview
shows the user what it could not place. A wrong ticker is far worse than a
rejected row: it silently books someone else's shares into the ledger.

One call per import, distinct labels only, memoised for the process — a
statement that names the same five holdings on nine pages asks once.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stocks.web.llm import Provider

# What the ledger accepts as a symbol: bare ("AAPL"), suffixed ("MC.PA",
# "BRK-B"). Deliberately NOT the ISIN shape validate.py also tolerates — an
# ISIN is a label to resolve here, not an answer.
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z0-9]{1,4})?$")

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# Saxo/ClickTrade write "ZBRA:xnas"; the exchange half is theirs, not Yahoo's.
# Case-insensitive because a label reaches here through Transaction, which
# upper-cases every ticker it is given — "ZBRA:xnas" arrives as "ZBRA:XNAS".
_VENUE_RE = re.compile(r"^([A-Z0-9.\-]{1,8}):([A-Z]{4})$", re.I)

# The most this will ever put in one call. A statement with more distinct
# instruments than this is not a personal brokerage account, and the excess
# simply stays unresolved rather than fanning out into more calls.
MAX_LABELS = 60

_SYSTEM = """You map instrument labels from a broker statement to their ticker
symbols, as Yahoo Finance spells them.

You are given a JSON array of labels. Each is whatever a broker printed to
identify a holding: a company name, an ISIN, a symbol with an exchange code,
or a local broker code.

Reply with ONLY a JSON object, no prose, no code fences: every input label,
copied verbatim as the key, mapped to its ticker as a string, or to null.

Rules:
- Use the listing the instrument actually trades as. A US listing is bare
  ("AMZN"); anywhere else takes the Yahoo suffix ("MC.PA", "SAP.DE",
  "AZN.L", "NESN.SW"). An ADR maps to the ADR's own US symbol ("PDD").
- An ISIN maps to the ticker of the security it identifies.
- A label that is not an instrument at all — a currency code, a cash or fee
  line, an account or transaction id — maps to null.
- null is the right answer whenever you are not sure. A wrong ticker books
  the wrong company's shares; a null is shown to the user to fix by hand.
"""

# label (upper-cased) -> ticker or None. Process-wide: the same statement
# re-uploaded, or a second file from the same broker, costs no second call.
_memo: dict[str, str | None] = {}


def obvious(label: str) -> str | None:
    """The ticker `label` already is, when no knowledge is needed to see it.

    Only textual forms: a bare symbol, and Saxo's "SYMBOL:mic" (the exchange
    code is dropped, not translated — a venue suffix is knowledge, so
    "TEF:xmce" goes to the model like any other label). Everything else,
    ISINs included, returns None and is resolved.
    """
    text = (label or "").strip()
    if not text:
        return None
    if venue := _VENUE_RE.match(text):
        symbol, mic = venue.group(1).upper(), venue.group(2).lower()
        # US venues have no Yahoo suffix, so the bare symbol is already right.
        if mic in ("xnas", "xngs", "xnys", "xase", "arcx", "bats"):
            return symbol if SYMBOL_RE.match(symbol) else None
        return None
    upper = text.upper()
    if _ISIN_RE.match(upper):
        return None
    return upper if SYMBOL_RE.match(upper) else None


def _parse_reply(raw: str, labels: list[str]) -> dict[str, str | None]:
    """The model's JSON as a label -> ticker map, keeping only usable answers."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    by_upper = {label.upper(): label for label in labels}
    resolved: dict[str, str | None] = {}
    for key, value in data.items():
        label = by_upper.get(str(key).strip().upper())
        if label is None:  # a key we never asked about
            continue
        ticker = str(value or "").strip().upper()
        resolved[label] = ticker if SYMBOL_RE.match(ticker) else None
    return resolved


def resolve(labels: list[str], provider: Provider, api_key: str = "",
            ask=None) -> dict[str, str | None]:
    """`labels` -> tickers, in one call. Unknown labels map to None.

    `ask(system, content) -> str` is the model call, injected so this module
    stays free of provider plumbing (llm_map passes its own `_ask`).
    """
    wanted = [label for label in dict.fromkeys(labels) if label.strip()]
    out: dict[str, str | None] = {}
    unknown: list[str] = []
    for label in wanted:
        if (hit := obvious(label)) is not None:
            out[label] = hit
        elif label.upper() in _memo:
            out[label] = _memo[label.upper()]
        else:
            unknown.append(label)

    if not unknown or ask is None:
        return out

    for label in unknown[MAX_LABELS:]:
        out[label] = None
    batch = unknown[:MAX_LABELS]
    answers = _parse_reply(ask(_SYSTEM, json.dumps(batch, ensure_ascii=False)), batch)
    for label in batch:
        ticker = answers.get(label)
        _memo[label.upper()] = ticker
        out[label] = ticker
    return out
