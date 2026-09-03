"""App operations the assistant can perform — the chat's tool registry.

Same architecture as the skill router (web/chat_skills.py): a keyword gate
keeps overhead at zero for normal analysis questions; when it hits, one cheap
classifier-model call turns the message into a structured Action — favorite a
ticker, set price alerts, put it in a group, add it to the watchlist, record a
position — which is then executed through the same helpers the profile editor
uses. Anything the parser can't turn into a valid action degrades to None and
the message flows on to the normal LLM answer: tools add a fast path, they
never block chat.

Each tool is one TOOLS entry: the catalog line the router reads, a parser that
turns the reply's JSON into validated args (returning None to reject), a
runner that applies it to the account's watchlist, and the locale key of its
confirmation. Adding a tool means adding an entry — the system prompt, the
valid-action list and the dispatch all derive from the registry.

The router prompt is deliberately a JSON contract rather than provider-native
tool calling: the keyless free chain hops across OpenAI-compatible backends
whose tool support varies, and Provider only exposes a text stream. A native
adapter can be added per provider later without touching the tools themselves.

Streamlit-free (auth is imported lazily inside the runners) so parsing stays
trivially testable; all UI — confirmation bubbles, i18n — lives in the two
surfaces that dispatch here (web/chat_core.py and chat/engine.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ConfigDict

from stocks.chat import structured

if TYPE_CHECKING:
    from stocks.web.llm import Provider

# Symbols as the app knows them: Yahoo tickers, broker codes, crypto pairs
# (BTC-EUR), class shares (BRK.B).
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")

# Cheap gate so the extra classifier call only happens when the message could
# plausibly be an action (EN + ES vocabulary). False positives are fine — the
# parser returns null and chat proceeds; false negatives just mean no action.
_GATE_RE = re.compile(
    r"favou?rit|\bfav\b|alert|av[ií]s|notif|\btag\b|etiquet|group|grupo|"
    r"watchlist|seguimiento|a[ñn]ad|agreg|quita|elimin|\badd\b|\bremove\b|"
    r"\bdrop\b|shares|acciones|particip|position|posici[oó]n|"
    r"precio medio|coste medio|cost basis",
    re.IGNORECASE,
)


def maybe_action(text: str) -> bool:
    """Whether the message is worth one classifier call."""
    return bool(_GATE_RE.search(text))


# Importing is not one of the actions below, and never will be: it needs the
# statement itself, not a sentence about it. This gate catches the ask
# ("import my trades", "importa las compras del pdf") so each surface can
# answer it deterministically — a model left to answer is free to describe an
# import that never happened, and has. The lookbehinds keep two Spanish
# homographs out of it: "(no) me importa" ("I (don't) care") and the noun
# "el importe" ("the amount"), which a book full of trades says often.
_IMPORT_VERB = re.compile(
    r"(?<!me )(?<!te )(?<!le )(?<!nos )(?<!el )(?<!los )(?<!un )(?<!del )"
    r"(?<!su )(?<!sus )(?<!estos )"
    r"\bimport(?:a|ar|arme|ame|as|amos|o|e|es|en|ed|ing|s)?\b|"
    r"\bcarga(?:r|me)?\b|\bsube\b|\bsubir\b|\bupload\b",
    re.IGNORECASE,
)
_IMPORT_OBJECT = re.compile(
    r"transacc|transaction|operaci[oó]n|operaciones|movimiento|\btrades?\b|"
    r"\bcompras?\b|\bventas?\b|extracto|statement|\bpdf\b|\bcsv\b|"
    r"\bexcel\b|\bxlsx\b|fichero|archivo|\bfile\b|cartera|portfolio",
    re.IGNORECASE,
)


def wants_import(text: str) -> bool:
    """Whether the message is asking for a statement to be imported.

    Deliberately not a Tool: there is nothing to execute here. The surfaces
    use it to answer with the one thing that does work — attach the file —
    instead of spending a model call on a request the model cannot fulfil.
    """
    return bool(_IMPORT_VERB.search(text) and _IMPORT_OBJECT.search(text))


@dataclass(frozen=True)
class Action:
    """One parsed app operation: which tool, on which symbol, with what.

    Tool-specific values live in `args` rather than in a field per tool, so
    the registry can grow without the dataclass growing with it. `alerts` and
    `tags` are exposed as properties because both surfaces read them by name.
    """

    kind: str
    ticker: str
    args: dict = field(default_factory=dict)

    @property
    def alerts(self) -> list[dict]:
        return self.args.get("alerts", [])

    @property
    def tags(self) -> list[str]:
        return self.args.get("tags", [])


# ------------------------------------------------------------------ parsers
# Each takes the classifier's decoded JSON object and returns this tool's
# validated args — or None to reject the whole action, which sends the message
# down the normal answer path.


def _no_args(data: dict) -> dict:
    return {}


def _clean_tags(raw) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        t = str(t).strip()
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out


def _parse_alerts(data: dict) -> dict | None:
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
    return {"alerts": alerts} if alerts else None


def _parse_tags(data: dict) -> dict | None:
    tags = _clean_tags(data.get("tags"))
    return {"tags": tags} if tags else None


def _parse_name(data: dict) -> dict:
    """add_ticker's optional company name — absent is fine, blank is nothing."""
    name = str(data.get("name") or "").strip()[:60]
    return {"name": name} if name else {}


def _parse_position(data: dict) -> dict | None:
    """shares / cost, either or both. Rejects when neither is a usable number
    — "record my position" with no numbers is a question, not an action."""
    out: dict = {}
    for key in ("shares", "cost"):
        if data.get(key) is None:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            continue
        if value >= 0:
            out[key] = value
    return out or None


# ------------------------------------------------------------------ runners
# Applied to the account's own watchlist. Alerts and tags are additive: the
# entry's existing values are read back and merged, because auth.set_* replace.


def _holding(ticker: str, path: Path):
    from stocks.config import load_watchlist

    return next(
        (h for h in load_watchlist(path) if h.ticker.upper() == ticker), None
    )


def _auth():
    from stocks.web import auth  # deferred: keeps this module streamlit-free

    return auth


def _run_favorite(act: Action, path: Path) -> None:
    _auth().set_favorite(act.ticker, True, path)


def _run_unfavorite(act: Action, path: Path) -> None:
    _auth().set_favorite(act.ticker, False, path)


def _run_set_alerts(act: Action, path: Path) -> None:
    from dataclasses import asdict

    holding = _holding(act.ticker, path)
    existing = [
        {k: v for k, v in asdict(a).items() if v is not None}
        for a in (holding.alerts if holding else [])
    ]
    new = [a for a in act.alerts if a not in existing]
    _auth().set_alerts(act.ticker, existing + new, path)


def _run_tag(act: Action, path: Path) -> None:
    holding = _holding(act.ticker, path)
    _auth().set_tags(act.ticker, (holding.tags if holding else []) + act.tags, path)


def _run_untag(act: Action, path: Path) -> None:
    holding = _holding(act.ticker, path)
    drop = {t.lower() for t in act.tags}
    kept = [t for t in (holding.tags if holding else []) if t.lower() not in drop]
    _auth().set_tags(act.ticker, kept, path)


def _run_add_ticker(act: Action, path: Path) -> None:
    _auth().add_entry(act.ticker, act.args.get("name", ""), path)


def _run_remove_ticker(act: Action, path: Path) -> None:
    _auth().remove_entry(act.ticker, path)


def _run_set_position(act: Action, path: Path) -> None:
    _auth().set_position(
        act.ticker, act.args.get("shares"), act.args.get("cost"), path
    )


# ----------------------------------------------------------------- registry


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str  # the catalog line the router reads, fields included
    parse: Callable[[dict], dict | None]
    run: Callable[[Action, Path], None]
    reply_key: str  # locale key of the confirmation bubble


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "favorite",
            "add the ticker to favorites (pins it to the top of the dashboard).",
            _no_args, _run_favorite, "chat.action_favorited",
        ),
        Tool(
            "unfavorite",
            "remove the ticker from favorites.",
            _no_args, _run_unfavorite, "chat.action_unfavorited",
        ),
        Tool(
            "set_alerts",
            'price thresholds. Fill "alerts": [{"type": "above"|"below", '
            '"price": <number>}]. Direction from wording (below/under/drops/'
            "falls/baja/cae -> below; above/over/rises/hits/sube/llega -> "
            "above). Two bare prices -> the lower one below, the higher one "
            "above. One bare price -> above.",
            _parse_alerts, _run_set_alerts, "chat.action_alerts_set",
        ),
        Tool(
            "tag",
            'add the ticker to one or more groups. Fill "tags": ["<group>"]. '
            "Reuse the exact spelling of an existing group from the context "
            "when the user means it.",
            _parse_tags, _run_tag, "chat.action_tagged",
        ),
        Tool(
            "untag",
            'remove the ticker from groups it is in. Fill "tags": ["<group>"].',
            _parse_tags, _run_untag, "chat.action_untagged",
        ),
        Tool(
            "add_ticker",
            'put a symbol on the watchlist so the app tracks it. Optionally '
            'fill "name" with the company name.',
            _parse_name, _run_add_ticker, "chat.action_added",
        ),
        Tool(
            "remove_ticker",
            "stop tracking a symbol — drops it from the watchlist entirely.",
            _no_args, _run_remove_ticker, "chat.action_removed",
        ),
        Tool(
            "set_position",
            'record how much of the ticker the user holds. Fill "shares" '
            'and/or "cost" (average buy price per share) with numbers; omit '
            "the one the user didn't state, and use 0 to clear it.",
            _parse_position, _run_set_position, "chat.action_position_set",
        ),
    )
}

KINDS = tuple(TOOLS)


def _catalog() -> str:
    return "\n".join(f'- "{t.name}": {t.summary}' for t in TOOLS.values())


_SYSTEM = f"""You turn requests from a stock-tracker chat into ONE app action.
Reply with ONLY a JSON object, no prose, no code fences:
{{"action": "<action name>" or null, "ticker": "<symbol>", <action fields>}}

Actions:
{_catalog()}

Rules:
- "action" is null when the message is a question, opinion or analysis
  request rather than a request to change the app. When in doubt, null.
- "ticker": the symbol the action targets. Resolve "this"/"it" ("esto") from
  the context's ticker in focus; resolve company names against the watchlist
  listing and reuse the symbol exactly as listed there.
- Include only the fields the chosen action names; leave the rest out.
"""


class ActionCall(structured.Contract):
    """The router's contract: which tool, on which symbol.

    ``extra="allow"`` because the tool-specific fields ("alerts", "tags",
    "shares"…) are the registry's business, not this model's — TOOLS grows
    without a field being added here, exactly like Action.args. Both keys are
    optional at this level so a shapeless answer still decodes and gets
    rejected by _action_from with a reason, instead of burning a repair call
    on a model that correctly answered "no action" ({"action": null}).
    """

    model_config = ConfigDict(extra="allow")

    action: str | None = None
    ticker: str | None = None


def _action_from(data: dict) -> Action | None:
    """An Action out of the router's decoded object, or None to reject it.

    None covers the whole "not an app operation" family — action null, an
    unknown tool, an unusable ticker, missing required fields — and sends the
    message down the normal answer path."""
    tool = TOOLS.get(data.get("action"))
    if tool is None:
        return None

    ticker = str(data.get("ticker") or "").strip().upper()
    if not _TICKER_RE.match(ticker):
        return None

    args = tool.parse(data)
    if args is None:
        return None
    return Action(tool.name, ticker, args)


def parse_action(raw: str) -> Action | None:
    """An Action out of a classifier reply, defensively.

    The tolerant reading, for callers holding a reply and no provider:
    anything missing, unknown or malformed yields None."""
    try:
        call = structured.decode(raw, ActionCall)
    except structured.OffContract:
        return None
    return _action_from(call.model_dump())


def detect(
    provider: Provider, api_key: str, message: str, context: str = ""
) -> Action | None:
    """Parse a message into an Action via the provider's cheapest model.

    None on any failure — network, bad JSON, no action — and the caller
    proceeds with the normal answer. Same BYOK key as the conversation.

    A reply that is not the requested object at all gets one repair turn
    (chat/structured.py); a reply that *is* the object and says "no action"
    does not, so the common case (a question, not a command) stays one call."""
    user = (context + "\n\n" if context else "") + f"User message: {message}"
    try:
        call = structured.ask(provider, api_key, _SYSTEM, user, ActionCall)
    except Exception:
        return None
    return _action_from(call.model_dump())


def execute(action: Action, path: Path | None = None) -> None:
    """Apply an Action to the account's watchlist."""
    p = path or _auth().watchlist_path()
    TOOLS[action.kind].run(action, p)


def reply(action: Action, translate: Callable[..., str]) -> str:
    """The confirmation bubble for an executed action.

    `translate(key, **kwargs) -> str` is the caller's own translator — the web
    panel passes its session-language `tr`, the Telegram bot one bound to the
    recipient's language — so this stays the single place that knows which
    locale key and which slots each tool's confirmation needs.
    """
    key = TOOLS[action.kind].reply_key
    if action.kind == "set_alerts":
        rules = ", ".join(
            translate(f"chat.action_alert_{a['type']}", price=f"{a['price']:g}")
            for a in action.alerts
        )
        return translate(key, ticker=action.ticker, rules=rules)
    if action.kind in ("tag", "untag"):
        return translate(key, ticker=action.ticker, groups=", ".join(action.tags))
    if action.kind == "set_position":
        parts = [
            translate(f"chat.action_position_{f}", value=f"{action.args[f]:g}")
            for f in ("shares", "cost")
            if f in action.args
        ]
        return translate(key, ticker=action.ticker, details=" · ".join(parts))
    return translate(key, ticker=action.ticker)
