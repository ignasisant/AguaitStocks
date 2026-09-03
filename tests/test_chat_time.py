"""The clock and the activity lines on a chat turn (web/chat_core.py).

The panel now says *when* a turn happened, *how long* an answer took, and
*what it ran* to produce it — Claude Code's working line, tool lines and
timestamps. These pin the three things that are easy to get wrong: the wall
clock is the reader's and not the server's, a turn's stamp survives a re-save,
and the tool lines escape whatever the model put in a tool argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from stocks.chat.agent import Evidence
from stocks.web import chat_core
from stocks.web.llm import ToolCall


@dataclass
class _Ctx:
    """st.context, as much of it as the clock reads."""

    timezone: str | None = None
    timezone_offset: int | None = None


@pytest.fixture
def ctx(monkeypatch):
    def use(**kwargs):
        monkeypatch.setattr(chat_core.st, "context", _Ctx(**kwargs))

    return use


# ------------------------------------------------------------------ clock


def test_the_clock_is_the_readers_not_the_servers(ctx):
    """Cloud Run serves in UTC; a Madrid reader must still see Madrid time."""
    ctx(timezone="Europe/Madrid")
    ts = datetime(2026, 9, 3, 14, 40, tzinfo=UTC).timestamp()
    assert chat_core._clock(ts) == "16:40"  # UTC+2 in September


def test_a_browser_that_only_sends_an_offset_still_gets_its_own_clock(ctx):
    """getTimezoneOffset is minutes *behind* UTC, so -120 means UTC+2."""
    ctx(timezone=None, timezone_offset=-120)
    assert chat_core._viewer_tz().utcoffset(None).total_seconds() == 7200


def test_an_unknown_zone_falls_back_to_the_offset(ctx):
    ctx(timezone="Mars/Olympus", timezone_offset=-60)
    assert chat_core._viewer_tz().utcoffset(None).total_seconds() == 3600


def test_no_browser_context_leaves_the_server_zone(monkeypatch):
    class _Dead:
        def __getattr__(self, name):
            raise RuntimeError("no ScriptRunContext")

    monkeypatch.setattr(chat_core.st, "context", _Dead())
    assert chat_core._viewer_tz() is None


@pytest.mark.parametrize("ts", [None, 0, "", "not a number"])
def test_an_unstamped_turn_shows_no_clock(ctx, ts):
    ctx(timezone="UTC")
    assert chat_core._clock(ts) == ""


# ------------------------------------------------------------------ cost


@pytest.mark.parametrize(
    "seconds,shown",
    [(8.42, "8.4s"), (59.94, "59.9s"), (72, "1m 12s"), (605, "10m 05s"),
     (None, ""), (0, ""), (-3, "")],
)
def test_elapsed_reads_like_the_terminal(seconds, shown):
    assert chat_core._took(seconds) == shown


def test_a_stamp_is_written_once_and_survives_a_resave():
    turn = chat_core._stamp({"role": "user", "content": "hola"})
    first = turn["ts"]
    assert chat_core._stamp(turn)["ts"] == first  # re-saved, not re-clocked
    assert "took" not in turn  # only an answer carries a cost


def test_an_answer_carries_what_it_cost():
    turn = chat_core._stamp({"role": "assistant", "content": "x"}, 12.3456)
    assert turn["took"] == 12.3


# ------------------------------------------------------------- tool lines


def _call(name, args, result=""):
    return ToolCall(name=name, args=args, result=result)


def test_gathered_calls_become_tool_lines():
    ev = Evidence([
        _call("search_web", {"query": "asml guidance 2026"},
              "[1] A\nhttps://a.example\nbody\n\n"
              "[2] B\nhttps://b.example\nbody"),  # toolbox._search_web's shape
        _call("read_page", {"url": "https://a.example/x"}, "y" * 1800),
    ])
    steps = chat_core._steps(ev, [], [])
    assert [s["tool"] for s in steps] == ["search_web", "read_page"]
    assert steps[0]["arg"] == "asml guidance 2026"
    assert steps[0]["out"].startswith("2")  # the two URLs it returned
    assert "1800" in steps[1]["out"]


def test_the_fixed_preflight_reads_the_same_as_a_gather():
    """Which code path fetched is plumbing — the reader sees the same lines."""

    @dataclass
    class _Quote:
        ticker: str

    hits = [chat_core.chat_web.Result("t", "https://a.example", "")] * 3
    steps = chat_core._steps(Evidence(ok=False), hits,
                             [_Quote("NVDA"), _Quote("ASML")])
    assert [s["tool"] for s in steps] == ["search_web", "get_quotes"]
    assert steps[1]["arg"] == "NVDA, ASML"


def test_a_long_argument_is_cut_to_its_line():
    long = _call("search_web", {"query": "q" * 300})
    arg = chat_core._steps(Evidence([long]), [], [])[0]["arg"]
    assert len(arg) == chat_core._STEP_ARG_CHARS


def test_nothing_run_means_no_lines():
    assert chat_core._steps(Evidence(ok=False), [], []) == []


def test_a_tool_argument_cannot_inject_markup(monkeypatch):
    """The arguments come from the model, and the lines are raw HTML."""
    written: list[str] = []
    monkeypatch.setattr(chat_core.st, "html", written.append)
    chat_core._render_steps(
        [{"tool": "search_web", "arg": '<img src=x onerror=alert(1)>',
          "out": "1 results"}])
    assert written and "<img" not in written[0]
    assert "&lt;img" in written[0]
