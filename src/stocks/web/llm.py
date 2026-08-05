"""LLM provider registry for the chat page — Claude, ChatGPT, Gemini.

Every provider is bring-your-own-key: the user supplies their own API key (own
billing). A provider exposes a streaming generator plus an error classifier that
maps its SDK's exceptions onto the page's shared locale keys
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

    @property
    def default_model(self) -> str:
        return self.models[0]

    def available(self) -> bool:
        """True when this provider's SDK is installed."""
        return importlib.util.find_spec(self._module) is not None

    def stream(
        self, api_key: str, model: str, system: str, messages: list[dict]
    ) -> Iterator[str]:
        return self._stream(api_key, model, system, messages)

    def error_key(self, exc: Exception) -> str | None:
        """Locale key for a known SDK error, or None to let the caller re-raise."""
        return self._error_key(exc)


# ------------------------------------------------------------- Claude (Anthropic)


def _anthropic_stream(api_key, model, system, messages):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    with client.messages.stream(
        model=model, max_tokens=MAX_TOKENS, system=system, messages=messages
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


def _openai_stream(api_key, model, system, messages):
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, *messages],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


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


# ------------------------------------------------------------- registry

PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            "anthropic", "Claude",
            ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"),
            "sk-ant-...", "https://console.anthropic.com/settings/keys",
            "anthropic", _anthropic_stream, _anthropic_error,
        ),
        Provider(
            "openai", "ChatGPT",
            ("gpt-5", "gpt-4o", "gpt-4o-mini"),
            "sk-...", "https://platform.openai.com/api-keys",
            "openai", _openai_stream, _openai_error,
        ),
        Provider(
            "gemini", "Gemini",
            ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"),
            "AIza...", "https://aistudio.google.com/apikey",
            "google.genai", _gemini_stream, _gemini_error,
        ),
    )
}

DEFAULT_PROVIDER = "anthropic"


def available_providers() -> list[Provider]:
    """Installed providers, registry order (Claude, ChatGPT, Gemini)."""
    return [p for p in PROVIDERS.values() if p.available()]
