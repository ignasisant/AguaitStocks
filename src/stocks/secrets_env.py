"""Env-first secret resolution shared by the web app and headless jobs.

The Streamlit deploy reads secrets.toml; cron jobs (GitHub Actions) only have
environment variables. `secret()` checks the environment first, then falls
back to the matching st.secrets section — the same precedence storage.py
already uses — so one code path serves both runtimes. Streamlit is imported
lazily and failure-tolerant: with no secrets.toml at all (bare CI run) the
fallback is simply empty.
"""

from __future__ import annotations

import os


def secret(env_var: str, section: str, key: str, default: str = "") -> str:
    """`os.environ[env_var]`, else `st.secrets[section][key]`, else `default`."""
    val = (os.environ.get(env_var) or "").strip()
    if val:
        return val
    try:
        import streamlit as st

        return str(st.secrets.get(section, {}).get(key, default)).strip() or default
    except Exception:
        return default
