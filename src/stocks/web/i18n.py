"""Lightweight i18n for the web app — per-session, no global locale.

Standard Python i18n (gettext / Babel) leans on a process-global locale
(`gettext.install`, `locale.setlocale`); this server runs many user sessions
in one process, so a global locale would leak one visitor's language into
another's session. Instead the active language is resolved per run and stashed
in session state, and `t()` is a pure dict lookup — session-safe, no `.mo`
compile step.

Catalogs live under locales/<lang>/*.json as flat {key: string} fragments,
one fragment per page (plus common.json for nav/shared widgets); the loader
merges every fragment for a language into one dict. Keys are dotted and
page-prefixed (`ticker.price`, `home.movers`, `common.save`) so fragments
never collide. English is the source language and the fallback for any key a
translation is missing.

Resolution order (see resolve_language): explicit Profile preference
(prefs.json "language") > browser navigator locale (st.context.locale) > "en".
app.py calls set_active_language() once per run, after auth.resolve_user() and
before page.run(), so every rerun re-resolves fresh (a Profile change takes
effect on its rerun) and pages/widgets only ever read the session value.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

_LOCALES = Path(__file__).parent / "locales"

# code -> native language name (shown in the Profile selector). English is the
# source catalog; add a code here and drop a locales/<code>/ folder to extend.
LANGUAGES = {"en": "English", "es": "Español"}
DEFAULT_LANG = "en"


def _catalog(lang: str) -> dict[str, str]:
    """Merge every locales/<lang>/*.json fragment into one flat dict.

    Cached per (language, fragment mtimes): the mtime key costs a handful of
    stats per rerun but means an edited fragment is picked up on the next run
    — Streamlit's file watcher doesn't reload JSON, so a plain per-language
    cache served stale keys in dev until a server restart.
    """
    d = _LOCALES / lang
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    return _catalog_cached(lang, tuple(f.stat().st_mtime for f in files))


@st.cache_data(show_spinner=False)
def _catalog_cached(lang: str, mtimes: tuple[float, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    d = _LOCALES / lang
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                out.update(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # a broken fragment falls back to English per-key
    return out


def supported(lang: str | None) -> str | None:
    """Normalize a locale tag ('es-ES', 'en_US') to a supported code, or None.

    Takes the primary subtag ('es-ES' -> 'es') so browser locales and stored
    prefs both resolve; returns None when the language isn't shipped.
    """
    if not lang:
        return None
    code = str(lang).replace("_", "-").split("-")[0].lower()
    return code if code in LANGUAGES else None


def resolve_language() -> str:
    """Active language: Profile pref > browser locale > English.

    Reads prefs.json (cheap) and st.context.locale; call once per run via
    set_active_language(), not per t() — t() reads the cached session value.
    """
    from stocks.web import auth

    pref = supported(auth.load_prefs().get("language"))
    if pref:
        return pref
    browser = supported(getattr(st.context, "locale", None))
    if browser:
        return browser
    return DEFAULT_LANG


def set_active_language() -> str:
    """Resolve and store the run's language in session state; returns the code.

    app.py calls this once per rerun before page.run(); pages and widgets then
    read it (via t()) without re-resolving, and a Profile change lands on the
    next rerun because app.py re-runs this first.
    """
    lang = resolve_language()
    st.session_state["active_lang"] = lang
    return lang


def active_language() -> str:
    return st.session_state.get("active_lang") or DEFAULT_LANG


def translate(key: str, lang: str, /, **kwargs) -> str:
    """t() with an explicit language — no session state, headless-safe.

    Used by the notification cron (digest/alert messages), where the per-user
    language comes from prefs.json instead of a Streamlit session.
    """
    code = supported(lang) or DEFAULT_LANG
    s = _catalog(code).get(key)
    if s is None:
        s = _catalog(DEFAULT_LANG).get(key, key)
    return s.format(**kwargs) if kwargs else s


def t(key: str, /, **kwargs) -> str:
    """Translate a key for the run's active language.

    Falls back to the English catalog, then to the raw key, so a missing
    translation degrades gracefully instead of raising. Pass format values as
    kwargs for placeholder strings, e.g. t("ticker.loading", ticker="AAPL")
    against a catalog value "Loading {ticker}…". Only formatted when kwargs are
    given, so literal-brace strings without placeholders stay untouched.
    """
    lang = active_language()
    s = _catalog(lang).get(key)
    if s is None:
        s = _catalog(DEFAULT_LANG).get(key, key)
    return s.format(**kwargs) if kwargs else s
