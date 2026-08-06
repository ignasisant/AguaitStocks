"""LLM provider registry for the chat page — Aguait AI (free), Claude, ChatGPT,
Gemini.

The named providers are bring-your-own-key: the user supplies their own API key
(own billing). "Aguait AI" is keyless for the user — it chains through
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass

MAX_TOKENS = 4096


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
    # logos). None = no external brand; the keyless Aguait provider ships its
    # own bundled icon instead.
    domain: str | None = None

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
        model=model, max_tokens=MAX_TOKENS, system=system_blocks,
        messages=_with_cache_breakpoint(messages),
    ) as stream:
        yield from stream.text_stream


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
        contents=contents,
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
        return "chat.api_error"
    return None


# ------------------------------------------------------- Aguait AI (free chain)
# Keyless for the user: a fixed-order chain of free-tier backends billed to the
# operator's keys ([free_llm] in secrets.toml). Each entry is (backend id,
# default model, OpenAI-compatible base_url) — all backends speak the OpenAI
# wire format. The model can be overridden per backend with a "<id>_model"
# secret, so a retired free model is a config change, not a release.

_FREE_BACKEND_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    ("cerebras", "llama-3.3-70b", "https://api.cerebras.ai/v1"),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free",
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


def _free_secrets() -> dict:
    import streamlit as st

    try:
        return dict(st.secrets.get("free_llm", {}))
    except Exception:  # no secrets.toml at all (bare local run)
        return {}


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
        out.append(_FreeBackend(bid, key, cfg.get(f"{bid}_model", default_model),
                                _openai_compat_stream(base_url)))
    return out


def _free_stream(api_key, model, system, messages):
    del api_key, model  # the chain supplies its own key and model per backend
    backends = _free_backends()
    if not backends:
        raise FreeTierExhausted("no free backend configured")
    started = False
    for b in backends:
        try:
            for chunk in b.stream(b.api_key, b.model, system, messages):
                started = True
                yield chunk
            return
        except Exception:
            if started:
                raise  # mid-answer failure: text already on screen, can't switch
            continue  # rate limit / bad key / retired model — try the next one
    raise FreeTierExhausted("all free backends failed")


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
            "free", "Aguait AI",
            ("auto",),  # the chain picks the backend; no user-facing model list
            "", "",  # keyless: no placeholder, no console link
            "openai", _free_stream, _free_error,
            needs_key=False,
            _available=lambda: bool(_free_backends()),
        ),
        Provider(
            "anthropic", "Claude",
            ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"),
            "sk-ant-...", "https://console.anthropic.com/settings/keys",
            "anthropic", _anthropic_stream, _anthropic_error,
            classifier_model="claude-haiku-4-5",
            domain="claude.ai",
        ),
        Provider(
            "openai", "ChatGPT",
            ("gpt-5", "gpt-4o", "gpt-4o-mini"),
            "sk-...", "https://platform.openai.com/api-keys",
            "openai", _openai_stream, _openai_error,
            classifier_model="gpt-4o-mini",
            domain="chatgpt.com",
        ),
        Provider(
            "gemini", "Gemini",
            # Rolling "-latest" aliases so the list can't rot when Google
            # retires a pinned version (2.5-flash/2.5-pro dropped for new keys).
            ("gemini-flash-latest", "gemini-3.6-flash", "gemini-flash-lite-latest"),
            "AQ... or AIza...", "https://aistudio.google.com/apikey",
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
    """Usable providers, registry order (Aguait AI, Claude, ChatGPT, Gemini)."""
    return [p for p in PROVIDERS.values() if p.available()]
