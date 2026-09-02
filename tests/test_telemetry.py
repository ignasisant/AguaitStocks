"""Run binding and the sign-in events (stocks.web.telemetry)."""

from __future__ import annotations

import logging

import pytest

from stocks import obs
from stocks.web import telemetry


@pytest.fixture
def events(monkeypatch) -> list[str]:
    """Event names emitted during the test, in order."""
    names: list[str] = []

    class Sink(logging.Handler):
        def emit(self, record):
            names.append(getattr(record, "event", record.getMessage()))

    monkeypatch.setattr(telemetry.obs, "setup", lambda *a, **k: None)
    monkeypatch.setattr(obs, "_ctx", type(obs._ctx)("test_ctx", default=None))
    sink = Sink()
    obs.log.addHandler(sink)
    obs.log.setLevel(logging.DEBUG)
    old, obs.log.propagate = obs.log.propagate, False
    yield names
    obs.log.removeHandler(sink)
    obs.log.propagate = old


@pytest.fixture
def session(monkeypatch) -> dict:
    """A fake Streamlit session, signed in as jane@example.com."""
    state: dict = {}
    monkeypatch.setattr(telemetry.st, "session_state", state, raising=False)
    monkeypatch.setattr(
        telemetry.st, "user", type("U", (), {"email": "jane@example.com"}),
        raising=False,
    )
    monkeypatch.setattr(telemetry.auth, "is_logged_in", lambda: True)
    return state


def test_signup_verdict_becomes_an_event(events, session):
    session["_login_kind"] = "signup"  # set by auth.resolve_user()
    telemetry.bind_run("Home")
    assert events == ["session.start", "auth.signup"]
    assert obs.current()["user"] == telemetry.auth.slug("jane@example.com")


def test_login_event_fires_once_per_session_not_per_rerun(events, session):
    session["_login_kind"] = "login"
    telemetry.bind_run("Home")
    telemetry.bind_run("Ticker")  # rerun / page change, same identity
    assert events == ["session.start", "auth.login"]


def test_guest_run_emits_no_auth_event(events, session, monkeypatch):
    monkeypatch.setattr(telemetry.auth, "is_logged_in", lambda: False)
    session["_login_kind"] = ""  # cleared by auth.resolve_user()
    telemetry.bind_run("Home")
    assert events == ["session.start"]
