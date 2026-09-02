"""Structured replies from the classifier models: validated, and repaired once.

The chat makes three cheap-model calls before it answers — skill routing
(web/chat_skills.py), web planning (web/chat_web.py) and action detection
(chat/tools.py). All three ask for one JSON object, and all three degrade to
"no answer" when something else comes back. Degrading is right: a classifier
must never block the actual answer. Degrading on the *first* attempt is not —
a small model that replied with prose, a trailing comma or the wrong field
name usually gets it right when it is shown its own reply and what was wrong
with it. This module is that second chance, plus the one place the
reply-to-JSON rules live instead of three near-copies of the same regex.

Contracts are pydantic models, which buys the distinction the callers actually
need: "the model answered off-contract" (`OffContract`, so fall back) and "the
model answered, and the answer is empty" (a valid contract holding an empty
list, so obey it) stop being the same value. The hand-rolled parsers could not
tell those apart — an unparseable planner reply looked exactly like a
deliberate "no search needed".

Errors are raised, not swallowed: the caller decides what a dead classifier
costs. A provider/network exception propagates untouched (nothing to repair);
`OffContract` means both attempts came back unusable.

Streamlit-free, like the callers it serves.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from stocks.web.llm import Provider

# Greedy on purpose: first "{" to last "}" spans a nested object, and models
# that wrap the answer in prose put it in one piece.
_BLOB_RE = re.compile(r"\{.*\}", re.S)

# The repair turn. Deliberately not a restatement of the contract — the
# original system prompt is sent again with it, so repeating the rules would
# only add tokens and room to contradict them.
_REPAIR = (
    "That reply was rejected: {error}\n"
    "Reply again with ONLY the JSON object the instructions describe — no "
    "prose, no explanation, no code fences."
)


class Contract(BaseModel):
    """Base for the JSON shapes the classifiers must answer in.

    ``extra="ignore"`` because a model volunteering a "reasoning" field is not
    a violation worth a second call: unknown *fields* are noise, unknown
    *values* are what the subclasses' validators reject.
    """

    model_config = ConfigDict(extra="ignore")


class OffContract(ValueError):
    """The model replied, but not in the requested shape (twice, if repaired).

    Carries the offending reply in `raw` — a caller with a looser last-resort
    reading of a reply (chat_skills scans prose for known skill ids) needs the
    text, not just the verdict.
    """

    def __init__(self, reason: str, raw: str = ""):
        super().__init__(reason)
        self.raw = raw


def blob(raw: str) -> str | None:
    """The JSON object inside a reply, or None if there is none."""
    m = _BLOB_RE.search(raw or "")
    return m.group() if m else None


def decode[C: Contract](raw: str, schema: type[C]) -> C:
    """`raw` as `schema`, or OffContract carrying why it was rejected.

    The message is what gets fed back to the model on the repair turn, so it
    says what is wrong in the model's own terms (missing field, bad value) —
    pydantic's error text already does that better than a hand-written one.
    """
    text = blob(raw)
    if text is None:
        raise OffContract("no JSON object in the reply", raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OffContract(f"not valid JSON ({exc.msg})", raw) from exc
    if not isinstance(data, dict):
        raise OffContract("the JSON value is not an object", raw)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise OffContract(_why(exc), raw) from exc


def _why(exc: ValidationError) -> str:
    """A pydantic error as one short line the model can act on."""
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def ask[C: Contract](
    provider: Provider,
    api_key: str,
    system: str,
    user: str,
    schema: type[C],
    *,
    model: str = "",
    repair: bool = True,
) -> C:
    """One classifier call answered as `schema`, with a single repair retry.

    Runs on the provider's cheapest model (`classifier_model`) unless `model`
    says otherwise. The repair turn costs a second cheap call, but only on the
    replies that were going to be thrown away anyway — a first-try success,
    which is the common case, spends nothing extra.

    Raises OffContract when both tries are unusable, and lets the provider's
    own exceptions through: a rate limit is not a contract problem, and the
    caller (heuristics, previous turn's skills, no action) already knows what
    to do about it.
    """
    picked = model or provider.classifier_model
    messages = [{"role": "user", "content": user}]
    raw = provider.complete(api_key, picked, system, messages)
    try:
        return decode(raw, schema)
    except OffContract as exc:
        if not repair:
            raise
        why = str(exc)
    second = provider.complete(
        api_key,
        picked,
        system,
        messages
        + [
            {"role": "assistant", "content": (raw or "").strip()[:500] or "(empty)"},
            {"role": "user", "content": _REPAIR.format(error=why)},
        ],
    )
    return decode(second, schema)
