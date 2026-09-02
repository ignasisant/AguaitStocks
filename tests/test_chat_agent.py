"""agent: the model-directed gather step and what it hands the answer."""

from stocks.chat import agent, toolbox
from stocks.chat.agent import Evidence
from stocks.web.llm import ToolCall, ToolRun


class _Provider:
    """Stand-in for a tool-capable llm.Provider."""

    id = "stub"
    classifier_model = "cheap-model"

    def __init__(self, run=None, exc=None, tools=True):
        self.run, self.exc, self._tools = run, exc, tools
        self.calls = []

    def supports_tools(self):
        return self._tools

    def run_tools(self, api_key, model, system, messages, tools, execute,
                  max_rounds=3):
        self.calls.append((api_key, model, system, messages, tools))
        if self.exc:
            raise self.exc
        return self.run(execute) if callable(self.run) else self.run


_MSGS = [{"role": "user", "content": "why did ASML drop?"}]


# ---------------------------------------------------------------- Evidence


def test_empty_evidence_leaves_the_message_alone():
    assert Evidence().augment("hola") == "hola"
    assert not Evidence()


def test_evidence_appends_what_was_fetched_with_its_arguments():
    ev = Evidence([ToolCall("get_quotes", {"tickers": ["NVDA"]}, "NVDA 226.68")])
    got = ev.augment("is NVDA up?")
    assert got.startswith("is NVDA up?")  # the user's own words stay first
    assert "get_quotes(tickers=['NVDA'])" in got
    assert "NVDA 226.68" in got
    assert "prefer these figures over anything you remember" in got


def test_ok_separates_no_lookup_needed_from_no_lookup_possible():
    assert Evidence().ok  # ran, decided nothing was needed
    assert not Evidence(ok=False).ok  # never ran


def test_sources_are_the_pages_the_gather_read():
    ev = Evidence([
        ToolCall("read_page", {"url": "https://ex.com/a"}, "body"),
        ToolCall("search_web", {"query": "asml"},
                 "[1] T\nhttps://ex.com/b\ntext\n\n[2] U\nhttps://ex.com/b\ntext"),
        ToolCall("get_quotes", {"tickers": ["NVDA"]}, "NVDA 1"),
    ])
    urls = [s["url"] for s in ev.sources()]
    assert urls == ["https://ex.com/a", "https://ex.com/b"]  # deduped, no quotes


# ------------------------------------------------------------------ gather


def test_gather_returns_what_the_model_fetched():
    p = _Provider(ToolRun("DONE", [ToolCall("get_quotes", {}, "NVDA 226")]))
    ev = agent.gather(p, "k", _MSGS, toolbox.Context())
    assert ev.ok and ev.tools_used == ["get_quotes"]


def test_gather_runs_the_tools_the_toolbox_offers():
    p = _Provider(ToolRun("DONE", []))
    agent.gather(p, "k", _MSGS, toolbox.Context())
    (_, model, system, messages, tools) = p.calls[0]
    assert model == "cheap-model"  # the gather is not what the big model is for
    assert [t.name for t in tools] == [s.name for s in toolbox.specs()]
    assert "recall" not in [t.name for t in tools]  # no index on this Context
    assert messages == _MSGS
    assert "Do NOT answer the user's question" in system


def test_gather_that_fetched_nothing_still_counts_as_having_run():
    # The model looked at the question and decided it needed nothing. Falling
    # back to the fixed pre-flight here would undo that decision.
    ev = agent.gather(_Provider(ToolRun("DONE", [])), "k", _MSGS,
                      toolbox.Context())
    assert ev.ok and not ev


def test_gather_drops_a_call_that_produced_nothing():
    p = _Provider(ToolRun("DONE", [ToolCall("search_web", {}, "")]))
    assert not agent.gather(p, "k", _MSGS, toolbox.Context())


def test_a_provider_without_tools_never_gathers():
    p = _Provider(ToolRun("x", []), tools=False)
    ev = agent.gather(p, "k", _MSGS, toolbox.Context())
    assert not ev.ok and p.calls == []


def test_a_dead_provider_falls_back_instead_of_raising():
    ev = agent.gather(_Provider(exc=RuntimeError("rate limited")), "k", _MSGS,
                      toolbox.Context())
    assert not ev.ok


def test_a_gather_that_overruns_is_abandoned():
    import time

    p = _Provider(lambda execute: time.sleep(5) or ToolRun("x", []))
    ev = agent.gather(p, "k", _MSGS, toolbox.Context(), timeout=0.05)
    assert not ev.ok


def test_gather_of_an_empty_thread_does_nothing():
    p = _Provider(ToolRun("x", []))
    assert not agent.gather(p, "k", [], toolbox.Context()).ok
    assert p.calls == []


def test_the_executor_handed_over_dispatches_the_real_tools():
    seen = {}

    def run(execute):
        seen["out"] = execute("nope_not_a_tool", {})
        return ToolRun("DONE", [])

    agent.gather(_Provider(run), "k", _MSGS, toolbox.Context())
    assert "no tool called" in seen["out"].lower()
