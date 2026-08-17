"""Chat actions: change the app straight from the assistant panel.

Same architecture as the skill router (web/chat_skills.py): a keyword gate
keeps overhead at zero for normal analysis questions; when it hits, one cheap
classifier-model call turns the message into a structured action — favorite /
unfavorite a ticker, add price alerts, put it in a group (tag) — which is then
executed against the account's watchlist through the same helpers the profile
editor uses (auth.set_favorite / set_alerts / set_tags). Anything the parser
can't turn into a valid action degrades to None and the message flows on to
the normal LLM answer: actions add a fast path, they never block chat.

Streamlit-free (auth is imported lazily inside execute) so parsing stays
trivially testable; all UI (confirmation bubbles, i18n) lives in chat_core.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stocks.web.llm import Provider

KINDS = ("favorite", "unfavorite", "set_alerts", "tag")

# Symbols as the app knows them: Yahoo tickers, broker codes, crypto pairs
# (BTC-EUR), class shares (BRK.B).
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")

# Cheap gate so the extra classifier call only happens when the message could
# plausibly be an action (EN + ES vocabulary). False positives are fine — the
# parser returns null and chat proceeds; false negatives just mean no action.
_GATE_RE = re.compile(
    r"favou?rit|\bfav\b|alert|av[ií]s|notif|\btag\b|etiquet|group|grupo",
    re.IGNORECASE,
)


def maybe_action(text: str) -> bool:
    """Whether the message is worth one classifier call."""
    return bool(_GATE_RE.search(text))


@dataclass
class Action:
    kind: str
    ticker: str
    alerts: list[dict] = field(default_factory=list)  # {"type", "price"} each
    tags: list[str] = field(default_factory=list)


_SYSTEM = """You turn requests from a stock-tracker chat into ONE app action.
Reply with ONLY a JSON object, no prose, no code fences:
{"action": "favorite" | "unfavorite" | "set_alerts" | "tag" | null,
 "ticker": "<symbol>",
 "alerts": [{"type": "above" or "below", "price": <number>}, ...],
 "tags": ["<group name>", ...]}

Rules:
- "action" is null when the message is a question, opinion or analysis
  request rather than a request to change the app. When in doubt, null.
- "ticker": the symbol the action targets. Resolve "this"/"it" ("esto") from
  the context's ticker in focus; resolve company names against the watchlist
  listing and reuse the symbol exactly as listed there.
- set_alerts: price thresholds only. Direction from wording (below/under/
  drops/falls/baja/cae -> below; above/over/rises/hits/sube/llega -> above).
  Two bare prices -> the lower one below, the higher one above. One bare
  price -> above.
- tag: "tags" holds the group name(s) to add the ticker to. Reuse the exact
  spelling of an existing group from the context when the user means it.
- favorite / unfavorite: add the ticker to, or remove it from, favorites.
"""


def parse_action(raw: str) -> Action | None:
    """An Action out of a classifier reply, defensively.

    First {...} blob parsed as JSON; anything missing, unknown or malformed
    (bad kind, invalid ticker, empty alerts/tags for the kinds that need them)
    yields None so the caller falls through to a normal answer."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("action") not in KINDS:
        return None
    kind = data["action"]

    ticker = str(data.get("ticker") or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        return None

    alerts: list[dict] = []
    for a in data.get("alerts") or []:
        if not isinstance(a, dict) or a.get("type") not in ("above", "below"):
            continue
        try:
            price = float(a.get("price"))
        except (TypeError, ValueError):
            continue
        rule = {"type": a["type"], "price": price}
        if price > 0 and rule not in alerts:
            alerts.append(rule)

    tags: list[str] = []
    for t in data.get("tags") or []:
        t = str(t).strip()
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)

    if kind == "set_alerts" and not alerts:
        return None
    if kind == "tag" and not tags:
        return None
    return Action(kind, ticker, alerts if kind == "set_alerts" else [],
                  tags if kind == "tag" else [])


def detect(
    provider: Provider, api_key: str, message: str, context: str = ""
) -> Action | None:
    """Parse a message into an Action via the provider's cheapest model.

    None on any failure — network, bad JSON, no action — and the caller
    proceeds with the normal answer. Same BYOK key as the conversation."""
    user = (context + "\n\n" if context else "") + f"User message: {message}"
    try:
        raw = provider.complete(
            api_key,
            provider.classifier_model,
            _SYSTEM,
            [{"role": "user", "content": user}],
        )
    except Exception:
        return None
    return parse_action(raw)


def execute(action: Action, path: Path | None = None) -> None:
    """Apply an Action to the account's watchlist.

    Alerts and tags are additive: new rules/groups merge into what the entry
    already has (set_alerts/set_tags replace, so the existing values are read
    back first). Favorite/unfavorite set the flag idempotently."""
    from stocks.config import load_watchlist
    from stocks.web import auth  # deferred: keeps this module streamlit-free

    p = path or auth.watchlist_path()
    if action.kind in ("favorite", "unfavorite"):
        auth.set_favorite(action.ticker, action.kind == "favorite", p)
        return

    holding = next(
        (h for h in load_watchlist(p) if h.ticker.upper() == action.ticker), None
    )
    if action.kind == "set_alerts":
        existing = [
            {k: v for k, v in asdict(a).items() if v is not None}
            for a in (holding.alerts if holding else [])
        ]
        new = [a for a in action.alerts if a not in existing]
        auth.set_alerts(action.ticker, existing + new, p)
    elif action.kind == "tag":
        auth.set_tags(action.ticker, (holding.tags if holding else []) + action.tags, p)
