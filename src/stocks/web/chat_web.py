"""Web search for the assistant panel (web/chat_core.py).

Keyless like the free chain: DuckDuckGo via the ``ddgs`` package, no API key
and no per-search billing. A *planner* call through the provider's cheapest
model (the same pattern as the skill auto-router) decides per message whether
the answer needs fresh information from the web and emits at most MAX_QUERIES
search queries; the hits are appended to the outgoing copy of the user's
message — not the system prompt, so provider prompt caches stay warm — and the
system prompt (chat_core._system_prompt) tells the model to ground on them and
cite URLs.

Everything degrades to "no web": a missing ddgs install, a planner failure,
a search error or an empty result set all yield [], and the answer proceeds
on the model's own knowledge plus the app context.

Streamlit-free so it stays trivially testable, like chat_skills/chat_actions.
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stocks.web.llm import Provider

MAX_QUERIES = 2  # searches the planner may request per message
MAX_RESULTS = 6  # hits injected into the prompt, across all queries
_PER_QUERY = 4  # hits fetched per query before the global cap
_SNIPPET_CHARS = 320  # per-hit body text kept in the prompt


def available() -> bool:
    """True when the ddgs package is installed (search is keyless)."""
    return importlib.util.find_spec("ddgs") is not None


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str


# ------------------------------------------------------------- planner

_PLANNER_SYSTEM = (
    "You decide whether answering the latest message in a stock-tracker chat "
    "needs fresh information from the web — news, prices beyond the app "
    "context, current events, recent filings or releases, or anything likely "
    "newer than the model's training data. Reply with ONLY a JSON object of "
    f'the form {{"queries": [...]}} holding at most {MAX_QUERIES} web search '
    "queries — or an empty list when the message needs none (greetings, app "
    "questions, the user's own positions, math, long-settled facts). Write "
    "queries in the language most likely to find good sources (usually "
    "English), include tickers or company names, and put the current year in "
    "time-sensitive queries. No prose, no code fences."
)


def parse_queries(raw: str, limit: int = MAX_QUERIES) -> list[str]:
    """Search queries out of a planner reply, defensively.

    First {...} blob parsed as JSON; non-strings dropped, whitespace collapsed,
    overlong queries truncated, dupes removed, list capped. Anything unparsable
    means no search — never an error."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    got = data.get("queries", []) if isinstance(data, dict) else []
    out: list[str] = []
    for q in got if isinstance(got, list) else []:
        if not isinstance(q, str):
            continue
        q = " ".join(q.split())[:200]
        if q and q not in out:
            out.append(q)
    return out[:limit]


def plan(
    provider: Provider, api_key: str, question: str, context: str = ""
) -> list[str]:
    """Search queries for a message, via the provider's cheapest model.

    [] both when the planner decides no search is needed and when the call
    itself fails — either way the answer proceeds without web results."""
    user = (context + "\n\n" if context else "") + f"User message: {question}"
    try:
        raw = provider.complete(
            api_key,
            provider.classifier_model,
            _PLANNER_SYSTEM,
            [{"role": "user", "content": user}],
        )
    except Exception:
        return []
    return parse_queries(raw)


# ------------------------------------------------------------- search


def search(queries: list[str]) -> list[Result]:
    """DuckDuckGo hits for the queries, deduped by URL and capped.

    Per-query failures are skipped (DDG throttles cloud IPs now and then, like
    Yahoo does); a dead ddgs install or a total failure returns []."""
    if not queries or not available():
        return []
    out: list[Result] = []
    seen: set[str] = set()
    try:
        from ddgs import DDGS

        with DDGS(timeout=8) as ddg:
            for q in queries:
                try:
                    hits = ddg.text(q, max_results=_PER_QUERY)
                except Exception:
                    continue
                for h in hits or []:
                    url = (h.get("href") or h.get("url") or "").strip()
                    title = " ".join((h.get("title") or "").split())
                    body = " ".join((h.get("body") or "").split())
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append(Result(title or url, url, body[:_SNIPPET_CHARS]))
    except Exception:
        pass  # keep whatever was collected before the failure
    return out[:MAX_RESULTS]


# ------------------------------------------------------------- prompt


def augment(message: str, results: list[Result]) -> str:
    """The user message with the search hits appended (unchanged when none).

    Only the outgoing copy of the turn is augmented — the stored history keeps
    the user's original text for display and for later context windows."""
    if not results:
        return message
    lines = [
        f"{i}. {r.title} — {r.url}" + (f"\n   {r.snippet}" if r.snippet else "")
        for i, r in enumerate(results, 1)
    ]
    return (
        message
        + "\n\n---\nWeb search results fetched for this message (ground your "
        "answer in them where relevant and cite the URLs you use):\n"
        + "\n".join(lines)
    )


def sources(results: list[Result]) -> list[dict]:
    """Compact {title, url} dicts for the history turn (JSON-persisted)."""
    return [{"title": r.title, "url": r.url} for r in results]
