"""Model-directed gathering: let the model fetch what the question needs.

The turn still ends the way it always has — one streamed answer through the
provider chain in web/chat_core.py — because streaming is most of what the chat
feels like. What changes is what that answer is grounded in. Instead of the
fixed pre-flight's guess (route skills, plan a search, fetch quotes for any
ticker mentioned, every single turn), a cheap tool loop runs first and the model
decides: nothing at all for "what's my biggest position?", a search and two page
reads for "why did ASML drop today?", a quote for "is NVDA above 200 yet?".

What it collects is appended to the *outgoing copy* of the user's turn, exactly
like chat_web.augment and market.augment already do — the stored history keeps
the user's own words, the prompt cache keeps its system prefix, and the final
answer is produced by the same streaming call as before. This module adds a
step; it replaces no plumbing.

Degrading is the whole design. No tool support on this provider, a rate limit,
a model that ignores the tools, a gather that runs long — all of them return
"no evidence" and leave the caller on the fixed pre-flight it already had.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from stocks import obs
from stocks.chat import toolbox
from stocks.web.llm import Provider, ToolCall

# The gather runs before a single character is on screen, so it is pure latency.
# Past this the answer goes out on whatever was collected so far.
TIMEOUT = 25.0

GATHER_SYSTEM = (
    "You are the research step of an investing assistant. Do NOT answer the "
    "user's question. Use the tools to fetch only what answering it will "
    "actually require, then stop and reply with the single word DONE.\n\n"
    "- Call nothing when the question needs nothing fetched: small talk, "
    "definitions, arithmetic, or something the conversation already covered.\n"
    "- Prefer one good call over three speculative ones. Every call costs the "
    "user seconds of waiting.\n"
    "- Use portfolio_snapshot for anything about what the user owns, "
    "get_quotes for what something trades at now, and search_web/read_page "
    "for news, filings and anything newer than your training data.\n"
    "Today is {today}."
)


@dataclass(frozen=True)
class Evidence:
    """What the gather step found, ready to ride on the outgoing user turn.

    `ok` separates the two ways of finding nothing, which the caller must not
    confuse: the loop ran and the model decided the question needed no lookup
    (ok, and falling back to the fixed pre-flight would undo the decision), or
    the loop never got to decide (not ok — no tool support, a dead key, a
    timeout — and the fixed pre-flight is exactly what should happen).
    """

    calls: list[ToolCall] = field(default_factory=list)
    ok: bool = True

    def __bool__(self) -> bool:
        return bool(self.calls)

    @property
    def tools_used(self) -> list[str]:
        return [c.name for c in self.calls]

    def sources(self) -> list[dict]:
        """The pages this gather read, in chat_web.sources' shape, so the panel
        can caption them with the widget it already has."""
        out: list[dict] = []
        for call in self.calls:
            url = str(call.args.get("url") or "")
            if call.name == "read_page" and url.startswith("http"):
                out.append({"title": url, "url": url})
            elif call.name == "search_web":
                for line in call.result.splitlines():
                    line = line.strip()
                    if line.startswith("http") and not any(
                            s["url"] == line for s in out):
                        out.append({"title": line, "url": line})
        return out[:6]

    def augment(self, message: str) -> str:
        """`message` with the gathered material appended (unchanged when none).

        Labelled as fetched-at-send-time so the model prefers it over anything
        it remembers — the same contract the system prompt already states for
        web extracts and live quotes."""
        if not self.calls:
            return message
        blocks = []
        for call in self.calls:
            args = ", ".join(f"{k}={v!r}" for k, v in sorted(call.args.items()))
            blocks.append(f"### {call.name}({args})\n{call.result}")
        return (
            message
            + "\n\n---\nMaterial fetched for this message. Ground your answer "
            "in it, prefer these figures over anything you remember, and cite "
            "the source URLs you use:\n\n"
            + "\n\n".join(blocks)
        )


def available(provider: Provider) -> bool:
    """Whether this provider can gather at all."""
    return provider.supports_tools()


def gather(
    provider: Provider,
    api_key: str,
    messages: list[dict],
    ctx: toolbox.Context,
    *,
    model: str = "",
    timeout: float = TIMEOUT,
) -> Evidence:
    """Run the tool loop for the pending turn and return what it fetched.

    Evidence with `ok=False` means the loop never ran (no tool support, dead
    key, timeout) and the caller should fall back to the fixed pre-flight;
    empty-but-ok means the model looked at the question and decided it needed
    nothing fetched, which is a result, not a failure.

    Runs on the provider's cheapest model: the gather step picks tools and
    reads results, which is not what the expensive model is for.
    """
    if not available(provider) or not messages:
        return Evidence(ok=False)
    system = GATHER_SYSTEM.format(today=date.today().isoformat())
    picked = model or provider.classifier_model or provider.default_model

    def run():
        return provider.run_tools(
            api_key, picked, system, messages,
            toolbox.specs(ctx), toolbox.executor(ctx),
        )

    # Same discipline as engine.in_parallel: no `with`, because shutdown would
    # block on exactly the hung fetch the timeout just escaped.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        run_result = pool.submit(run).result(timeout=timeout)
    except Exception as exc:
        obs.warn("chat.gather_failed", provider=provider.id, model=picked,
                 error_type=type(exc).__name__, error=str(exc)[:300])
        return Evidence(ok=False)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    calls = [c for c in run_result.calls if c.result]
    if calls:
        obs.event("chat.gathered", provider=provider.id, model=picked,
                  tools=[c.name for c in calls])
    return Evidence(calls)
