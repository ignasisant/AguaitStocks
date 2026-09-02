"""llm: the per-provider tool loops behind Provider.run_tools."""

import json
from types import SimpleNamespace

import pytest

from stocks.web import llm
from stocks.web.llm import ToolSpec

_TOOLS = [ToolSpec("get_quotes", "live prices",
                   {"type": "object", "properties": {}})]


def _echo(name, args):
    return f"{name}:{sorted(args.items())}"


# ------------------------------------------------------------------ shapes


def test_only_the_providers_with_a_loop_report_tool_support():
    assert llm.PROVIDERS["anthropic"]._tools is not None
    assert llm.PROVIDERS["openai"]._tools is not None
    assert llm.PROVIDERS["free"]._tools is not None
    # Gemini's function calling has its own message shape and is not wired up;
    # it keeps the fixed pre-flight rather than half a loop.
    assert llm.PROVIDERS["gemini"]._tools is None
    assert not llm.PROVIDERS["gemini"].supports_tools()


def test_run_tools_on_a_provider_without_a_loop_is_a_clear_error():
    with pytest.raises(NotImplementedError):
        llm.PROVIDERS["gemini"].run_tools("k", "m", "s", [], _TOOLS, _echo)


# --------------------------------------------------------------- anthropic


class _AnthropicClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.sent.append(kw)
        return self.responses.pop(0)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _use(tid, name, args):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=args)


@pytest.fixture
def anthropic_client(monkeypatch):
    import anthropic

    holder = {}

    def factory(*responses):
        client = _AnthropicClient(*responses)
        holder["client"] = client
        monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: client)
        return client

    return factory


def test_anthropic_stops_when_the_model_asks_for_nothing(anthropic_client):
    client = anthropic_client(SimpleNamespace(content=[_text("DONE")]))
    run = llm._anthropic_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                               _TOOLS, _echo, 3)
    assert run.text == "DONE" and run.calls == []
    assert len(client.sent) == 1
    assert client.sent[0]["tools"][0]["name"] == "get_quotes"
    assert "input_schema" in client.sent[0]["tools"][0]  # not "parameters"


def test_anthropic_runs_a_tool_and_feeds_the_result_back(anthropic_client):
    client = anthropic_client(
        SimpleNamespace(content=[_use("tu_1", "get_quotes", {"tickers": ["NVDA"]})]),
        SimpleNamespace(content=[_text("DONE")]),
    )
    run = llm._anthropic_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                               _TOOLS, _echo, 3)
    assert [c.name for c in run.calls] == ["get_quotes"]
    assert run.calls[0].args == {"tickers": ["NVDA"]}
    sent = client.sent[1]["messages"]
    assert sent[-2]["role"] == "assistant"
    assert sent[-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1",
         "content": "get_quotes:[('tickers', ['NVDA'])]"}]}


def test_anthropic_returns_parallel_results_in_one_user_message(anthropic_client):
    # Splitting them across messages teaches the model to stop asking for
    # parallel calls at all.
    client = anthropic_client(
        SimpleNamespace(content=[_use("a", "get_quotes", {}),
                                 _use("b", "get_quotes", {})]),
        SimpleNamespace(content=[_text("DONE")]),
    )
    llm._anthropic_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                         _TOOLS, _echo, 3)
    last = client.sent[1]["messages"][-1]
    assert last["role"] == "user" and len(last["content"]) == 2


def test_anthropic_stops_at_the_round_cap(anthropic_client):
    forever = [SimpleNamespace(content=[_use(f"t{i}", "get_quotes", {})])
               for i in range(10)]
    client = anthropic_client(*forever)
    run = llm._anthropic_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                               _TOOLS, _echo, 2)
    assert len(client.sent) == 2 and len(run.calls) == 2


def test_an_empty_tool_result_is_still_sent_back(anthropic_client):
    # Anthropic rejects an empty tool_result content, and a dropped result
    # leaves the tool_use block unanswered.
    client = anthropic_client(
        SimpleNamespace(content=[_use("tu_1", "get_quotes", {})]),
        SimpleNamespace(content=[_text("DONE")]),
    )
    llm._anthropic_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                         _TOOLS, lambda n, a: "", 3)
    assert client.sent[1]["messages"][-1]["content"][0]["content"] == "(no result)"


# ----------------------------------------------------------- openai-compat


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls

    def model_dump(self, **kw):
        return {"role": "assistant", "content": self.content}


def _tool_call(cid, name, args):
    return SimpleNamespace(
        id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class _OpenAIClient:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=self.messages.pop(0))])


@pytest.fixture
def openai_client(monkeypatch):
    import openai

    def factory(*messages):
        client = _OpenAIClient(*messages)
        monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
        return client

    return factory


def test_openai_puts_the_system_prompt_in_the_messages(openai_client):
    client = openai_client(_Message(content="DONE"))
    run = llm._openai_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                            _TOOLS, _echo, 3)
    assert run.text == "DONE"
    assert client.sent[0]["messages"][0] == {"role": "system", "content": "sys"}
    fn = client.sent[0]["tools"][0]
    assert fn["type"] == "function" and fn["function"]["name"] == "get_quotes"


def test_openai_answers_each_tool_call_by_id(openai_client):
    client = openai_client(
        _Message(tool_calls=[_tool_call("c1", "get_quotes", {"tickers": ["NVDA"]})]),
        _Message(content="DONE"),
    )
    run = llm._openai_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                            _TOOLS, _echo, 3)
    assert run.calls[0].args == {"tickers": ["NVDA"]}
    assert client.sent[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "c1",
        "content": "get_quotes:[('tickers', ['NVDA'])]"}


def test_openai_survives_unparseable_arguments(openai_client):
    bad = SimpleNamespace(id="c1", function=SimpleNamespace(
        name="get_quotes", arguments="{not json"))
    openai_client(_Message(tool_calls=[bad]), _Message(content="DONE"))
    run = llm._openai_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                            _TOOLS, _echo, 3)
    assert run.calls[0].args == {}  # the tool decides what to do with nothing


def test_openai_stops_at_the_round_cap(openai_client):
    client = openai_client(*[
        _Message(tool_calls=[_tool_call(f"c{i}", "get_quotes", {})])
        for i in range(10)])
    llm._openai_tools("k", "m", "sys", [{"role": "user", "content": "hi"}],
                      _TOOLS, _echo, 2)
    assert len(client.sent) == 2


# ------------------------------------------------------------- free chain


def test_the_free_chain_moves_on_when_a_backend_refuses(monkeypatch):
    tried = []

    def backend(base_url):
        def run(api_key, model, system, messages, tools, execute, rounds):
            tried.append(base_url)
            if base_url == "one":
                raise RuntimeError("no tool support on this model")
            return llm.ToolRun("DONE", [])
        return run

    monkeypatch.setattr(llm, "_openai_compat_tools", backend)
    monkeypatch.setattr(llm, "_free_backends", lambda: [
        llm._FreeBackend("a", "k1", "m1", None, "one"),
        llm._FreeBackend("b", "k2", "m2", None, "two"),
    ])
    run = llm._free_tools("", "", "sys", [], _TOOLS, _echo, 3)
    assert tried == ["one", "two"] and run.text == "DONE"


def test_the_free_chain_raises_when_every_backend_refuses(monkeypatch):
    monkeypatch.setattr(llm, "_openai_compat_tools", lambda url: (
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("429"))))
    monkeypatch.setattr(llm, "_free_backends", lambda: [
        llm._FreeBackend("a", "k", "m", None, "one")])
    with pytest.raises(llm.FreeTierExhausted):
        llm._free_tools("", "", "sys", [], _TOOLS, _echo, 3)


def test_an_unconfigured_free_chain_has_nothing_to_run(monkeypatch):
    monkeypatch.setattr(llm, "_free_backends", lambda: [])
    with pytest.raises(llm.FreeTierExhausted):
        llm._free_tools("", "", "sys", [], _TOOLS, _echo, 3)
