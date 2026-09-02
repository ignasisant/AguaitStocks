"""In-app feedback: a sidebar button opening a modal, stored durably.

Two sinks, deliberately redundant:

* A JSON file per submission under data/feedback/, mirrored to the bucket
  (key data/feedback/<stamp>-<id>.json) — the full text, kept until deleted.
  `stocks feedback` reads them back, local or straight from the bucket.
* An obs event ("feedback") — so submissions show up in the production log
  timeline next to whatever the user was doing when they hit the button, and
  `stocks logs tail --event feedback` works with no extra tooling.

Who sent it: the account's data-dir name (the same pseudonymous slug the logs
use), "guest" for anonymous visitors. The text itself is user input — treat it
as untrusted when reading it back anywhere.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import streamlit as st

from stocks import obs, storage
from stocks.config import DATA_DIR, PROJECT_ROOT
from stocks.web import ratelimit
from stocks.web.i18n import active_language
from stocks.web.i18n import t as tr

FEEDBACK_DIR = DATA_DIR / "feedback"
KINDS = ("bug", "idea", "other")
MAX_CHARS = 4000

# Feedback is cheap to store but a spam vector like any free-text endpoint.
_MAX_PER_HOUR = 5


def _sender() -> str:
    """The pseudonymous account slug the logs already use, or "guest"."""
    try:
        paths = st.session_state.get("user_paths")
    except Exception:  # outside a script run (CLI, tests): anonymous
        paths = None
    if paths is None:
        return "guest"
    root = paths.root
    return "owner" if root == PROJECT_ROOT else root.name


def submit(text: str, kind: str, page: str = "") -> Path:
    """Persist one submission (disk + bucket) and log the event."""
    kind = kind if kind in KINDS else "other"
    text = text.strip()[:MAX_CHARS]
    stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    path = FEEDBACK_DIR / f"{stamp}-{uuid.uuid4().hex[:6]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ts": stamp,
        "kind": kind,
        "text": text,
        "page": page,
        "user": _sender(),
        "lang": active_language(),
    }, ensure_ascii=False, indent=2))
    storage.persist(path)
    obs.event("feedback", kind=kind, page=page, chars=len(text))
    return path


def render_sidebar(page_title: str) -> None:
    """The entry point: one sidebar button on every page, opening the modal.

    A modal, not a sidebar popover: the collapsed desktop sidebar is a
    hover-expanded overlay rail, and app.py hides the drawer's own content
    (`stSidebarUserContent`) whenever the rail is not hovered — so a popover
    anchored to a button in there died the moment the pointer left the sidebar
    to reach it, i.e. before the user could type a word. It was also capped at
    the drawer's width. The dialog is centered in the page, independent of the
    sidebar's hover state, and roomy enough to write in.
    """
    # The success toast belongs to the run AFTER the modal closes — a toast
    # emitted inside the dialog dies with the rerun that shuts it.
    if st.session_state.pop("_fb_sent", False):
        st.toast(tr("feedback.sent"), icon=":material/favorite:")
    if st.sidebar.button(
        tr("feedback.button"),
        icon=":material/rate_review:",
        width="stretch",
        key="fb_open",
    ):
        st.session_state["_fb_open"] = True
    if not st.session_state.get("_fb_open"):
        return
    # Kept open by a session flag rather than by the button's own run: every
    # full rerun in this app (top-bar search, chat panel, page nav) would
    # otherwise drop the modal mid-sentence. Dismissing clears the flag —
    # without the callback the next full rerun would pop it straight back up.
    # Built at call time (not @st.dialog) so the title resolves in the run's
    # active language rather than freezing at import — same as the login and
    # investor-profile modals.
    st.dialog(
        tr("feedback.button"), width="small", on_dismiss=_close
    )(_dialog_body)(page_title)


def _close() -> None:
    st.session_state["_fb_open"] = False


def _dialog_body(page_title: str) -> None:
    """The modal's body: type picker + comment + send, batched in a form.

    A form on purpose: outside one, the text area's blur rerun eats the first
    click on the send button (the classic type-then-click miss), so the user
    has to press Send twice. Submitting commits both widgets in one go.
    """
    st.caption(tr("feedback.caption"))
    with st.form("fb_form", border=False, enter_to_submit=False):
        kind = st.segmented_control(
            tr("feedback.kind"),
            KINDS,
            default="idea",
            format_func=lambda k: tr(f"feedback.kind_{k}"),
            key="fb_kind",
        )
        text = st.text_area(
            tr("feedback.text"),
            placeholder=tr("feedback.placeholder"),
            max_chars=MAX_CHARS,
            height=200,
            key="fb_text",
        )
        sent = st.form_submit_button(
            tr("feedback.send"), type="primary",
            icon=":material/send:", width="stretch",
        )
    if not sent:
        return
    if not text.strip():
        st.warning(tr("feedback.empty"))
        return
    if not ratelimit.allow(f"feedback::{_sender()}",
                           max_events=_MAX_PER_HOUR, window_s=3600):
        st.warning(tr("feedback.rate_limited"))
        return
    try:
        submit(text, kind or "other", page_title)
    except Exception:
        # The local file may have been written even if the mirror failed; the
        # user shouldn't retry into a duplicate. Their text stays in the form.
        st.error(tr("feedback.failed"))
        obs.event("feedback.store_failed")
        return
    # Stored: drop the draft so the next open starts blank, then close the
    # modal with a full rerun (which also renders the toast above).
    st.session_state.pop("fb_text", None)
    st.session_state["_fb_sent"] = True
    _close()
    st.rerun()


# ------------------------------------------------------------------ read side


def stored(prefix: str = "data/feedback/") -> list[dict]:
    """Every stored submission, oldest first — local files plus bucket-only
    ones (a headless checkout sees the bucket, a dev checkout sees both)."""
    items: dict[str, dict] = {}
    if FEEDBACK_DIR.is_dir():
        for f in sorted(FEEDBACK_DIR.glob("*.json")):
            try:
                items[f.name] = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
    for key in storage.list_keys(prefix):
        name = key.removeprefix(prefix)
        if name in items or not name.endswith(".json"):
            continue
        raw = storage.read_key(key)
        if raw:
            try:
                items[name] = json.loads(raw)
            except ValueError:
                continue
    return [items[k] for k in sorted(items)]
