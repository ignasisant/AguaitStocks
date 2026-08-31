"""In-app feedback: a small sidebar popover on every page, stored durably.

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
    """The entry point: one popover at the bottom of the sidebar, every page."""
    with st.sidebar.popover(
        f":material/rate_review: {tr('feedback.button')}", width="stretch"
    ):
        st.caption(tr("feedback.caption"))
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
            key="fb_text",
        )
        if st.button(tr("feedback.send"), type="primary",
                     icon=":material/send:", key="fb_send"):
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
                # The local file may have been written even if the mirror
                # failed; the user shouldn't retry into a duplicate.
                st.error(tr("feedback.failed"))
                obs.event("feedback.store_failed")
                return
            st.toast(tr("feedback.sent"), icon=":material/favorite:")


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
