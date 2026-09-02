"""The feedback store (stocks.web.feedback) — submission files and read-back.

The sidebar widget itself is Streamlit chrome; what must not break silently is
the storage contract: a submission survives as a JSON file (mirrored to the
bucket) that `stocks feedback` can read back, including bucket-only copies a
fresh checkout has never seen.
"""

from __future__ import annotations

import json

import pytest

from stocks import storage
from stocks.web import feedback


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(storage, "_cached", {"config": None})  # bucket off
    monkeypatch.setattr(feedback, "active_language", lambda: "en")
    return tmp_path / "feedback"


def test_submit_writes_one_json_file_per_submission(sandbox):
    p = feedback.submit("The chart is upside down", "bug", page="Cartera")
    data = json.loads(p.read_text())
    assert data["kind"] == "bug"
    assert data["text"] == "The chart is upside down"
    assert data["page"] == "Cartera"
    assert data["user"] == "guest"  # no session -> anonymous
    assert p.parent == sandbox


def test_submit_clamps_kind_and_length(sandbox):
    p = feedback.submit("x" * (feedback.MAX_CHARS + 500), "exploit")
    data = json.loads(p.read_text())
    assert data["kind"] == "other"
    assert len(data["text"]) == feedback.MAX_CHARS


def test_stored_merges_local_and_bucket_and_sorts(sandbox, monkeypatch):
    feedback.submit("local one", "idea")
    bucket = {
        "data/feedback/2020-01-01T00-00-00Z-aaaaaa.json": json.dumps(
            {"ts": "2020-01-01T00-00-00Z", "kind": "bug", "text": "old, bucket-only"}
        ).encode(),
        "data/feedback/not-json.txt": b"ignored",
    }
    monkeypatch.setattr(
        storage, "list_keys",
        lambda prefix="": sorted(k for k in bucket if k.startswith(prefix)),
    )
    monkeypatch.setattr(storage, "read_key", bucket.get)
    items = feedback.stored()
    assert len(items) == 2
    assert items[0]["text"] == "old, bucket-only"  # oldest first
    assert items[1]["text"] == "local one"


def test_stored_is_empty_when_nothing_anywhere(sandbox):
    assert feedback.stored() == []


# --------------------------------------------------------------- the modal UI


APP = """
import streamlit as st
from stocks.web import feedback

st.write("page body")
feedback.render_sidebar("Cartera")
"""


@pytest.fixture
def app(sandbox):
    """The sidebar entry point running in a real script run."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(APP, default_timeout=30)
    at.run()
    return at


def _send(at):
    return at.button(key="FormSubmitter:fb_form-Send")


def test_the_button_opens_a_dialog_with_a_writable_text_area(app):
    """The whole point of the modal: the comment field is reachable. The old
    sidebar popover died the moment the pointer left the drawer to reach it."""
    assert [b.key for b in app.sidebar.button] == ["fb_open"]
    assert not app.text_area  # nothing rendered until the button is pressed
    app.sidebar.button(key="fb_open").click().run()
    assert [t.key for t in app.text_area] == ["fb_text"]
    assert [s.key for s in app.segmented_control] == ["fb_kind"]
    assert not app.exception


def test_send_stores_the_comment_closes_the_modal_and_toasts(app, sandbox):
    app.sidebar.button(key="fb_open").click().run()
    app.text_area(key="fb_text").set_value("  the chart is upside down  ")
    app.segmented_control(key="fb_kind").set_value("bug")
    _send(app).click().run()

    stored = json.loads(next(sandbox.glob("*.json")).read_text())
    assert (stored["text"], stored["kind"], stored["page"]) == (
        "the chart is upside down", "bug", "Cartera")
    assert not app.text_area  # modal closed
    assert [t.value for t in app.toast] == ["Thanks — received!"]
    assert not app.exception


def test_an_empty_send_warns_and_keeps_the_modal_open(app, sandbox):
    """A rejected submission must not close the modal or eat the draft —
    the user would have to retype everything."""
    app.sidebar.button(key="fb_open").click().run()
    _send(app).click().run()
    assert [w.value for w in app.warning] == ["Write something first."]
    assert [t.key for t in app.text_area] == ["fb_text"]
    assert not sandbox.exists()  # nothing stored
