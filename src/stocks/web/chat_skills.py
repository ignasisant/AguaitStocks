"""Skill library for the assistant panel (web/chat_core.py).

A *skill* is a markdown file in web/skills/ holding an analysis framework the
model should apply to a question — a sector lens (tech, energy…), a style lens
(value, technical…) or a task recipe (earnings review, Spain tax…). The file's
frontmatter carries the id, an English display name (used as the heading inside
the system prompt) and a one-line description that doubles as the classifier
catalog entry; the body is the framework itself. Bodies are English because the
system prompt is English regardless of UI language — the UI label is localized
separately under the ``chat.skill.<id>`` locale keys.

Auto mode routes every message through the provider's cheapest model
(``Provider.classifier_model``): the catalog plus the question go in, a JSON
list of at most MAX_AUTO skill ids comes out. Any failure — network, bad JSON,
unknown ids — degrades to "no skills", never blocks the answer.

This module is deliberately streamlit-free so it stays trivially testable; all
UI (mode picker, multiselect, lens captions) lives in chat_core.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stocks.web.llm import Provider

_SKILLS_DIR = Path(__file__).parent / "skills"

MAX_AUTO = 2  # skills the classifier may apply per message
MAX_MANUAL = 3  # skills the user may pin in manual mode


@dataclass(frozen=True)
class Skill:
    id: str
    name: str  # English heading used inside the system prompt
    description: str  # one line; the classifier catalog entry
    body: str  # the framework text injected into the system prompt


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def _parse(text: str, path: Path) -> Skill:
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path.name}: missing frontmatter")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if value:
            meta[key.strip()] = value.strip()
    body = m.group(2).strip()
    for field in ("id", "name", "description"):
        if not meta.get(field):
            raise ValueError(f"{path.name}: missing '{field}' in frontmatter")
    if not body:
        raise ValueError(f"{path.name}: empty body")
    return Skill(meta["id"], meta["name"], meta["description"], body)


@lru_cache(maxsize=1)
def catalog() -> tuple[Skill, ...]:
    """All skills, sorted by id (files are named <id>.md)."""
    return tuple(
        _parse(p.read_text(encoding="utf-8"), p)
        for p in sorted(_SKILLS_DIR.glob("*.md"))
    )


def valid_ids() -> set[str]:
    return {s.id for s in catalog()}


def skills_block(ids: list[str]) -> str:
    """The system-prompt section for the chosen skills ('' when none).

    Emitted in catalog order regardless of the order ids arrive in, so the
    same set always yields byte-identical text (keeps provider prompt caches
    warm across turns)."""
    chosen = [s for s in catalog() if s.id in set(ids)]
    if not chosen:
        return ""
    return (
        "\n\nApply these analysis frameworks where they fit the question; "
        "skip parts that don't apply.\n\n"
        + "\n\n".join(f"## {s.name}\n{s.body}" for s in chosen)
    )


# ------------------------------------------------------------- auto mode

_CLASSIFIER_SYSTEM = (
    "You route questions from a stock-tracker chat to analysis skills. "
    'Reply with ONLY a JSON object of the form {"skills": [...]} listing at '
    f"most {MAX_AUTO} skill ids from the catalog that clearly fit the user's "
    "latest message — or an empty list when none clearly applies (casual talk, "
    "app questions, greetings). No prose, no code fences.\n\nCatalog:\n"
)


def parse_skill_ids(raw: str, limit: int = MAX_AUTO) -> list[str]:
    """Skill ids out of a classifier reply, defensively.

    Strict path: first {...} blob parsed as JSON. Fallback: scan the text for
    known ids in order of appearance. Anything unknown is dropped; result is
    deduped and capped."""
    valid = valid_ids()
    ids: list[str] = []
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            data = json.loads(m.group())
            got = data.get("skills", []) if isinstance(data, dict) else []
            ids = [i for i in got if isinstance(i, str) and i in valid]
        except json.JSONDecodeError:
            ids = []
    if not ids:
        ids = [t for t in re.findall(r"[a-z][a-z-]*[a-z]", raw) if t in valid]
    out: list[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out[:limit]


def classify(
    provider: Provider, api_key: str, question: str, context: str = ""
) -> list[str] | None:
    """Skill ids for a message, via the provider's cheapest model.

    Returns None when the classifier call itself fails (caller may fall back
    to the previous turn's skills) and [] when it ran and decided no skill
    applies. Uses the same BYOK key as the conversation."""
    cat = "\n".join(f"- {s.id}: {s.description}" for s in catalog())
    user = (context + "\n\n" if context else "") + f"User message: {question}"
    try:
        raw = provider.complete(
            api_key,
            provider.classifier_model,
            _CLASSIFIER_SYSTEM + cat,
            [{"role": "user", "content": user}],
        )
    except Exception:
        return None  # any SDK/network error -> caller falls back, answer proceeds
    return parse_skill_ids(raw)
