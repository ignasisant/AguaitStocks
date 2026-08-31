"""Streamlit glue for stocks.obs — who/what/where for every dashboard run.

app.py calls `bind_run()` once per script run; every log record emitted after
it (from any module, including the page bodies and the chat engine) carries the
same `session`, `user` and `page` fields, so one production incident can be
pulled up as a timeline:

    stocks logs tail --user ignasi484_gmail_com_1a2b3c4d --since 2h

`user` is the auth slug, not the raw address — the same opaque-ish key the
account's data directory uses, so a log line points straight at the files
involved. Set STOCKS_LOG_USER=0 to keep even that out of the logs.
"""

from __future__ import annotations

import os

import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import ScriptControlException

from stocks import obs
from stocks.web import auth

# st.stop() and st.rerun() raise these to unwind the script. They are normal
# control flow, so a page that ends in one is a successful run, not an error.
CONTROL_FLOW: tuple[type[BaseException], ...] = (ScriptControlException,)

LOG_USER = os.getenv("STOCKS_LOG_USER", "1") != "0"


def session_id() -> str:
    """Stable short id for the browser session, or "-" outside a script run.

    Streamlit's own session id is a random routing token; a prefix of it is
    enough to group one visitor's runs without carrying the whole thing around.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        ctx = get_script_run_ctx()
        if ctx is not None and ctx.session_id:
            return str(ctx.session_id)[:12]
    except Exception:  # noqa: BLE001 — never break a page over a log field
        pass
    return "-"


def _user_id() -> str:
    if not LOG_USER:
        return "anon"
    try:
        if not auth.is_logged_in():
            return "guest"
        return auth.slug(str(st.user.email))
    except Exception:  # noqa: BLE001
        return "?"


def bind_run(page: str | None = None) -> None:
    """Install logging (idempotent) and bind this run's ambient fields."""
    obs.setup()
    user = _user_id()
    obs.bind(session=session_id(), user=user)
    if page:
        obs.bind(page=page)
    # Streamlit reruns the whole script on every interaction, so an unguarded
    # event here would log hundreds of times per visit. Fire only when the
    # identity of the session changes: first run, and the run right after a
    # sign-in or sign-out.
    if st.session_state.get("_obs_user") != user:
        st.session_state["_obs_user"] = user
        obs.event("session.start", logged_in=user not in ("guest", "?"))
