"""LLM provider registry for the chat page — TopStocks AI (free), Claude, ChatGPT,
Gemini.

The named providers are bring-your-own-key: the user supplies their own API key
(own billing). "TopStocks AI" is keyless for the user — it chains through
operator-funded free-tier backends configured in the ``[free_llm]`` secrets
section, hopping to the next backend when one is rate-limited, until all are
exhausted. A provider exposes a streaming generator plus an error classifier
that maps its SDK's exceptions onto the page's shared locale keys
(``chat.invalid_key`` / ``chat.no_credits`` / ``chat.api_error``); an unmapped
exception returns ``None`` so the caller can re-raise it.

SDKs are imported lazily inside each function, so a missing optional dependency
only disables that one provider (``Provider.available()``) instead of breaking
the whole page.
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

from stocks import obs

MAX_TOKENS = 4096

# Rounds of "model asks for tools, we run them" before the loop is cut off. The
# gather step is a means to an answer, not the answer: three rounds is enough
# for search -> read -> check a quote, and a cap is what stops a model that
# keeps asking for one more page from owning the whole turn.
MAX_TOOL_ROUNDS = 3


@dataclass(frozen=True)
class ToolSpec:
    """One tool offered to the model: what it is called, what it does, and the
    JSON Schema of its arguments. Provider-neutral — each backend's own wire
    shape is built from this."""

    name: str
    description: str
    schema: dict


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    result: str


@dataclass(frozen=True)
class ToolRun:
    """What one tool-using exchange produced: the model's closing text and
    every tool call it made along the way, in order."""

    text: str
    calls: list[ToolCall]


@dataclass(frozen=True)
class Provider:
    id: str
    label: str  # brand name, shown in the selector (not translated)
    models: tuple[str, ...]  # selectable models; the first is the default
    key_placeholder: str
    console_url: str
    _module: str  # top-level import name used for the availability probe
    _stream: Callable[[str, str, str, list[dict]], Iterator[str]]
    _error_key: Callable[[Exception], str | None]
    needs_key: bool = True  # False = keyless (server-side keys); no BYOK gate
    _available: Callable[[], bool] | None = None  # overrides the SDK probe
    # Cheapest model, used by the skill auto-router (web/chat_skills.py). ""
    # falls back to default_model — right for the free chain, which ignores the
    # model argument and picks per backend.
    classifier_model: str = ""
    # Brand website, for the selector logo (mirrored same-origin like broker
    # logos). None = no external brand; the keyless TopStocks provider ships its
    # own bundled icon instead.
    domain: str | None = None
    # Native tool use, when this backend has it wired up. None means the caller
    # keeps the fixed pre-flight (chat/engine.py) — a provider without tools
    # loses the model-directed lookup, not the answer.
    _tools: Callable[..., ToolRun] | None = None

    @property
    def default_model(self) -> str:
        return self.models[0]

    def available(self) -> bool:
        """True when this provider's SDK is installed (or its override says so)."""
        if self._available is not None:
            return self._available()
        return importlib.util.find_spec(self._module) is not None

    def stream(
        self, api_key: str, model: str, system: str, messages: list[dict]
    ) -> Iterator[str]:
        return self._stream(api_key, model, system, messages)

    def complete(
        self, api_key: str, model: str, system: str, messages: list[dict]
    ) -> str:
        """One short non-interactive completion (the skill router's call).

        Joins the streaming generator rather than adding a second code path per
        provider — for the tiny replies involved the cost is identical."""
        return "".join(self._stream(api_key, model or self.default_model,
                                    system, messages))

    def error_key(self, exc: Exception) -> str | None:
        """Locale key for a known SDK error, or None to let the caller re-raise."""
        return self._error_key(exc)

    def supports_tools(self) -> bool:
        """Whether this provider can run the model-directed lookup."""
        return self._tools is not None and self.available()

    def run_tools(
        self,
        api_key: str,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec],
        execute: Callable[[str, dict], str],
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> ToolRun:
        """Let the model call `tools` until it stops asking, then hand back what
        it said and what it ran.

        `execute(name, args) -> str` is the caller's dispatcher; the loop itself
        (and every backend's message shape) stays in here. Raises the SDK's own
        exceptions — the caller decides whether a failed lookup is fatal (it is
        not: chat/agent.py falls back to the fixed pre-flight).
        """
        if self._tools is None:
            raise NotImplementedError(f"{self.id} has no tool support")
        return self._tools(api_key, model or self.default_model, system,
                           messages, tools, execute, max_rounds)


# ------------------------------------------------------------- Claude (Anthropic)


def _with_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """A copy of the turns with one ephemeral cache breakpoint on the last one.

    Prompt caching is a prefix match: the whole prior-conversation prefix is a
    cache *read* on the next turn (~0.1x cost) instead of full-price reprocessing
    of the entire thread every message. We copy rather than mutate — the caller's
    history is shared with the other providers and persisted to disk as plain
    strings.
    """
    if not messages:
        return messages
    out = [dict(m) for m in messages]
    out[-1]["content"] = [
        {"type": "text", "text": out[-1]["content"],
         "cache_control": {"type": "ephemeral"}}
    ]
    return out


def _anthropic_stream(api_key, model, system, messages):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    # Cache the frozen persona + book snapshot (system) and the conversation
    # prefix. Within a session these repeat verbatim, so caching turns the
    # resend-everything-every-turn cost into cheap cache reads. A miss (e.g. the
    # book snapshot changed, or the prefix is below the model's min) just re-
    # writes — no error, no behaviour change.
    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    with client.messages.stream(
        model=model, max_tokens=MAX_TOKENS, system=cast(Any, system_blocks),
        messages=cast(Any, _with_cache_breakpoint(messages)),
    ) as stream:
        yield from stream.text_stream


def _anthropic_tools(api_key, model, system, messages, tools, execute, rounds):
    """Anthropic's tool loop: create, run every tool_use block, feed the results
    back, repeat until the model stops asking.

    Parallel tool calls arrive as several tool_use blocks in one assistant
    message and their results must go back in ONE user message — splitting them
    teaches the model to stop asking for them in parallel. The assistant turn is
    appended as `resp.content` rather than re-serialized text so nothing (the
    tool_use blocks least of all) is lost on the way back.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    schema = [{"name": t.name, "description": t.description,
               "input_schema": t.schema} for t in tools]
    convo = list(messages)
    calls: list[ToolCall] = []
    text = ""
    for _ in range(max(1, rounds)):
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=system,
            messages=convo, tools=cast(Any, schema),
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            break
        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for block in uses:
            args = dict(block.input or {})
            out = execute(block.name, args)
            calls.append(ToolCall(block.name, args, out))
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out or "(no result)"})
        convo.append({"role": "user", "content": results})
    return ToolRun(text, calls)


def _anthropic_error(exc):
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "chat.invalid_key"
    if isinstance(exc, anthropic.BadRequestError):
        return "chat.no_credits" if "credit balance" in str(exc) else "chat.api_error"
    if isinstance(exc, anthropic.APIError):
        return "chat.api_error"
    return None


# ------------------------------------------------------------- ChatGPT (OpenAI)


def _openai_compat_stream(base_url: str | None = None):
    """Streaming generator for OpenAI and any OpenAI-compatible endpoint
    (Groq, Cerebras, OpenRouter, ...) — only the base_url differs."""

    def _stream(api_key, model, system, messages):
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, *messages],
            stream=True,
        )
        for chunk in stream:
            # OpenRouter interleaves comment frames with no choices; skip them.
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    return _stream


_openai_stream = _openai_compat_stream()


def _openai_compat_tools(base_url: str | None = None):
    """The same loop over any OpenAI-compatible host: OpenAI itself, and every
    backend in the free chain (groq, cerebras, openrouter all speak it).

    Tool results go back as one "tool" message per call, each keyed by the
    tool_call_id from the assistant turn — the wire equivalent of Anthropic's
    single user message of tool_result blocks.
    """

    def run(api_key, model, system, messages, tools, execute, rounds):
        import json

        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        schema = [{"type": "function",
                   "function": {"name": t.name, "description": t.description,
                                "parameters": t.schema}} for t in tools]
        convo = [{"role": "system", "content": system}] + list(messages)
        calls: list[ToolCall] = []
        text = ""
        for _ in range(max(1, rounds)):
            resp = client.chat.completions.create(
                model=model, max_tokens=MAX_TOKENS, messages=convo,
                tools=cast(Any, schema),
            )
            msg = resp.choices[0].message
            text = msg.content or ""
            if not msg.tool_calls:
                break
            convo.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                # Only function tools are ever sent, so a custom-tool call
                # (which carries no `.function`) can't be one of ours.
                if tc.type != "function":
                    continue
                # Never string-match the serialized arguments: models escape
                # them differently. Bad JSON is the model's error to see.
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    args = {}
                out = execute(tc.function.name, args if isinstance(args, dict) else {})
                calls.append(ToolCall(tc.function.name,
                                      args if isinstance(args, dict) else {}, out))
                convo.append({"role": "tool", "tool_call_id": tc.id,
                              "content": out or "(no result)"})
        return ToolRun(text, calls)

    return run


_openai_tools = _openai_compat_tools()


def _openai_error(exc):
    import openai

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return "chat.invalid_key"
    if isinstance(exc, openai.RateLimitError):
        # OpenAI returns 429 for both rate limits and an exhausted quota.
        code = str(getattr(exc, "code", "") or "")
        no_credit = code == "insufficient_quota" or "quota" in str(exc).lower()
        return "chat.no_credits" if no_credit else "chat.api_error"
    if isinstance(exc, openai.APIError):
        return "chat.api_error"
    return None


# ------------------------------------------------------------- Gemini (Google)


def _gemini_stream(api_key, model, system, messages):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    # Gemini roles are "user"/"model"; map the assistant turns accordingly.
    contents = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in messages
    ]
    stream = client.models.generate_content_stream(
        model=model,
        contents=cast(Any, contents),
        config=types.GenerateContentConfig(system_instruction=system),
    )
    for chunk in stream:
        if chunk.text:
            yield chunk.text


def _gemini_error(exc):
    from google.genai import errors

    if isinstance(exc, errors.ClientError):
        code = getattr(exc, "code", None)
        msg = str(exc).lower()
        if code in (401, 403) or "api key" in msg or "api_key" in msg:
            return "chat.invalid_key"
        if code == 429 or "quota" in msg or "billing" in msg:
            return "chat.no_credits"
        return "chat.api_error"
    if isinstance(exc, errors.APIError):
        # 503 UNAVAILABLE "experiencing high demand": the model is saturated,
        # not broken — tell the user it's the provider and that switching
        # helps, instead of the generic "assistant is unavailable".
        if getattr(exc, "code", None) == 503:
            return "chat.provider_busy"
        return "chat.api_error"
    return None


# ------------------------------------------------------- TopStocks AI (free chain)
# Keyless for the user: a fixed-order chain of free-tier backends billed to the
# operator's keys ([free_llm] in secrets.toml). Each entry is (backend id,
# default model, OpenAI-compatible base_url) — all backends speak the OpenAI
# wire format. The model can be overridden per backend with a "<id>_model"
# secret, so a retired free model is a config change, not a release.

_FREE_BACKEND_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    # groq retired this slug some time before 2026-09-01 (prod logs: 404
    # model_not_found on every request). It stays as the first thing tried
    # because the chain now recovers by itself — see _free_live_model above:
    # the 404 costs one call per boot, then /models names the replacement.
    ("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    # Checked live 2026-08-31 against each backend's /models and a real
    # completion. cerebras: key valid (/models is 200) but every model answers
    # 402 payment_required — free quota is account-level and spent. openrouter:
    # the google/* free slugs 429 with limit_source "upstream_provider_shared_pool"
    # (Google AI Studio's pool, shared by all OpenRouter users — nothing this
    # account can raise), so the default moved to a non-Google slug that
    # streams today. A retired default is still a config fix — "<id>_model" in
    # [free_llm] overrides these.
    ("cerebras", "gpt-oss-120b", "https://api.cerebras.ai/v1"),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free",
     "https://openrouter.ai/api/v1"),
)


class FreeTierExhausted(Exception):
    """Every configured free backend failed before producing any output."""


@dataclass(frozen=True)
class _FreeBackend:
    id: str
    api_key: str
    model: str
    stream: Callable[[str, str, str, list[dict]], Iterator[str]]
    base_url: str = ""  # for the /models lookup when `model` has been retired


# Free tiers retire model slugs without notice, and the chain's only symptom is
# a 404 "model does not exist" on every request until an operator edits a
# secret. These let it recover on its own: on a retired-model failure the
# backend's own /models list (every OpenAI-compatible host serves one) picks a
# replacement, and the one that answers is remembered for the rest of the
# process, so the dead slug costs one call per boot rather than one per chat.
_free_live_model: dict[str, str] = {}

# Not chat models — Groq alone serves speech, guard and embedding slugs.
_NON_CHAT_MODEL_HINTS = ("whisper", "tts", "guard", "embed", "rerank", "moderat")

# Substring preferences, best first: hold the tier the retired defaults were
# (a large instruct model) before dropping to the small/fast ones.
_CHAT_MODEL_PREFS = (
    "versatile", "120b", "-70b", "kimi", "qwen3-32b", "maverick", "scout",
    "instant", "20b", "mini",
)


def _retired_model(exc: Exception) -> bool:
    """True when a failure says the *model* is gone rather than the tier being
    rate-limited, out of credit or the key being wrong."""
    if getattr(exc, "status_code", None) not in (None, 404):
        return False
    text = str(exc).lower()
    return "model_not_found" in text or "does not exist" in text


def _pick_chat_model(ids: list[str]) -> str | None:
    chat = [m for m in sorted(ids)
            if not any(h in m.lower() for h in _NON_CHAT_MODEL_HINTS)]
    for pref in _CHAT_MODEL_PREFS:
        for mid in chat:
            if pref in mid.lower():
                return mid
    return chat[0] if chat else None


def _live_model(b: _FreeBackend) -> str | None:
    """A model this backend actually serves right now, or None if it won't say."""
    from openai import OpenAI

    try:
        client = OpenAI(api_key=b.api_key, base_url=b.base_url or None)
        return _pick_chat_model([m.id for m in client.models.list()])
    except Exception as exc:  # a backend that hides /models keeps its retired slug
        obs.warn("llm.free.model_list_failed", backend=b.id,
                 error_type=type(exc).__name__, error=str(exc)[:200])
        return None


def _free_secrets() -> dict:
    try:
        import streamlit as st

        cfg = dict(st.secrets.get("free_llm", {}))
    except Exception:  # no secrets.toml at all (bare local run / CI)
        cfg = {}
    # Env overlay so headless jobs (GitHub Actions digest) can run the chain
    # without a secrets.toml: FREE_LLM_GROQ, FREE_LLM_GROQ_MODEL, ...
    import os

    for bid, _model, _url in _FREE_BACKEND_DEFAULTS:
        for k in (bid, f"{bid}_model"):
            env = os.environ.get(f"FREE_LLM_{k.upper()}")
            if env:
                cfg[k] = env
    return cfg


def _free_backends() -> list[_FreeBackend]:
    """The configured slice of the chain, in fixed fallback order.

    A backend joins only when its [free_llm] key is set and its SDK is
    installed, so an unconfigured deploy simply has no free provider."""
    cfg = _free_secrets()
    if importlib.util.find_spec("openai") is None:
        return []
    out = []
    for bid, default_model, base_url in _FREE_BACKEND_DEFAULTS:
        key = (cfg.get(bid) or "").strip()
        if not key:
            continue
        model = (_free_live_model.get(bid)
                 or cfg.get(f"{bid}_model", default_model))
        out.append(_FreeBackend(bid, key, model,
                                _openai_compat_stream(base_url), base_url))
    return out


def _free_stream(api_key, model, system, messages):
    del api_key, model  # the chain supplies its own key and model per backend
    backends = _free_backends()
    if not backends:
        raise FreeTierExhausted("no free backend configured")
    started = False
    for attempt, b in enumerate(backends):
        # Grows by at most one entry: a retired slug appends the replacement
        # /models named, so the same backend gets a second shot before the
        # chain moves on.
        candidates = [b.model]
        while candidates:
            model = candidates.pop(0)
            t0 = time.perf_counter()
            try:
                for chunk in b.stream(b.api_key, model, system, messages):
                    started = True
                    yield chunk
                if model != b.model:
                    _free_live_model[b.id] = model  # proven: skip the dead slug
                obs.event("llm.free.answered", backend=b.id, model=model,
                          attempt=attempt,
                          duration_ms=round((time.perf_counter() - t0) * 1000))
                return
            except Exception as exc:
                if started:
                    obs.error("llm.free.mid_answer_failure", exc, backend=b.id,
                              model=model)
                    raise  # mid-answer failure: text already on screen, can't switch
                # rate limit / bad key / retired model — try the next one, but say
                # which one died and why: this is the operator's only signal that a
                # free tier went paid or a model was retired. Structured, because
                # "which backend has been failing all week" is a query, not a read:
                #   stocks logs stats --event llm.free.backend_failed --by backend
                obs.warn("llm.free.backend_failed", backend=b.id, model=model,
                         attempt=attempt, error_type=type(exc).__name__,
                         error=str(exc)[:300],
                         status=getattr(exc, "status_code", None))
                if not _retired_model(exc):
                    break
                alt = _live_model(b)
                if not alt or alt == model:
                    break
                obs.warn("llm.free.model_substituted", backend=b.id,
                         retired=model, model=alt)
                candidates.append(alt)
    obs.error("llm.free.exhausted", backends=[b.id for b in backends])
    raise FreeTierExhausted("all free backends failed")


def _free_tools(api_key, model, system, messages, tools, execute, rounds):
    """The chain again, for the tool loop: first backend that gets through wins.

    Simpler than _free_stream on purpose — nothing has been shown to the user
    yet, so any failure (rate limit, a backend whose model has no tool support,
    a retired slug) is just the next backend's turn. When they all refuse, the
    caller falls back to the fixed pre-flight, so this raising is not the end of
    the answer.
    """
    del api_key, model  # the chain supplies its own key and model per backend
    backends = _free_backends()
    if not backends:
        raise FreeTierExhausted("no free backend configured")
    for attempt, b in enumerate(backends):
        try:
            return _openai_compat_tools(b.base_url or None)(
                b.api_key, b.model, system, messages, tools, execute, rounds)
        except Exception as exc:
            obs.warn("llm.free.tools_failed", backend=b.id, model=b.model,
                     attempt=attempt, error_type=type(exc).__name__,
                     error=str(exc)[:300],
                     status=getattr(exc, "status_code", None))
    raise FreeTierExhausted("no free backend ran the tool loop")


def _free_error(exc):
    if isinstance(exc, FreeTierExhausted):
        return "chat.free_exhausted"
    # Backends are operator-configured; nothing here is actionable by the user.
    return "chat.api_error"


# ------------------------------------------------------------- registry

PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            "free", "TopStocks AI",
            ("auto",),  # the chain picks the backend; no user-facing model list
            "", "",  # keyless: no placeholder, no console link
            "openai", _free_stream, _free_error,
            needs_key=False,
            _available=lambda: bool(_free_backends()),
            _tools=_free_tools,
        ),
        Provider(
            "anthropic", "Claude",
            ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"),
            "sk-ant-...", "https://console.anthropic.com/settings/keys",
            "anthropic", _anthropic_stream, _anthropic_error,
            _tools=_anthropic_tools,
            classifier_model="claude-haiku-4-5",
            domain="claude.ai",
        ),
        Provider(
            "openai", "ChatGPT",
            ("gpt-5", "gpt-4o", "gpt-4o-mini"),
            "sk-...", "https://platform.openai.com/api-keys",
            "openai", _openai_stream, _openai_error,
            _tools=_openai_tools,
            classifier_model="gpt-4o-mini",
            domain="chatgpt.com",
        ),
        Provider(
            "gemini", "Gemini",
            # Rolling "-latest" aliases so the list can't rot when Google
            # retires a pinned version (2.5-flash/2.5-pro dropped for new keys).
            ("gemini-flash-latest", "gemini-3.6-flash", "gemini-flash-lite-latest"),
            "AQ... or AIza...", "https://aistudio.google.com/apikey",
            # No _tools: google-genai's function calling has its own message
            # shape, and Gemini keeps the fixed pre-flight until it is written
            # and tested against a live key. Everything else on this page works.
            "google.genai", _gemini_stream, _gemini_error,
            classifier_model="gemini-flash-lite-latest",
            domain="gemini.google.com",
        ),
    )
}

DEFAULT_PROVIDER = "anthropic"  # BYOK default when the free chain is not deployed


def default_provider_id() -> str:
    """Free chain when the deploy configures it (zero-setup chat), else BYOK."""
    return "free" if PROVIDERS["free"].available() else DEFAULT_PROVIDER


def available_providers() -> list[Provider]:
    """Usable providers, registry order (TopStocks AI, Claude, ChatGPT, Gemini)."""
    return [p for p in PROVIDERS.values() if p.available()]
