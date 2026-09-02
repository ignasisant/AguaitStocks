"""structured: reply-to-contract decoding and the single repair turn."""

import pytest
from pydantic import field_validator

from stocks.chat import structured
from stocks.chat.structured import OffContract


class _Plan(structured.Contract):
    queries: list[str]


class _Loose(structured.Contract):
    """A contract that cleans instead of rejecting, like the real ones."""

    ids: list[str]

    @field_validator("ids", mode="before")
    @classmethod
    def _clean(cls, v):
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else v


class _Provider:
    """Stand-in for llm.Provider: hands back scripted replies, counts calls."""

    classifier_model = "cheap-model"

    def __init__(self, *replies, exc=None):
        self.replies, self.exc = list(replies), exc
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append((api_key, model, system, messages))
        if self.exc:
            raise self.exc
        return self.replies.pop(0) if self.replies else ""


# ------------------------------------------------------------------ decode


def test_decode_reads_the_object_out_of_prose_and_fences():
    got = structured.decode('Sure! ```json\n{"queries": ["ASML news"]}\n```', _Plan)
    assert got.queries == ["ASML news"]


def test_decode_ignores_fields_the_contract_does_not_name():
    got = structured.decode('{"queries": [], "reasoning": "nothing recent"}', _Plan)
    assert got.queries == []


@pytest.mark.parametrize(
    "raw, why",
    [
        ("I cannot help with that", "no JSON object"),
        ("", "no JSON object"),
        ('["bare", "list"]', "no JSON object"),
        ('{"queries": [oops]}', "not valid JSON"),
        ("{broken json", "no JSON object"),  # no closing brace, no blob
        ('{"queries": "not a list"}', "queries"),
        ('{"nope": 1}', "queries"),
    ],
)
def test_decode_rejects_off_contract_replies_with_a_reason(raw, why):
    with pytest.raises(OffContract) as err:
        structured.decode(raw, _Plan)
    assert why in str(err.value)
    assert err.value.raw == raw  # the caller may still read the text itself


def test_decode_keeps_a_before_validators_cleaning():
    assert structured.decode('{"ids": ["a", 42, "b"]}', _Loose).ids == ["a", "b"]


# --------------------------------------------------------------------- ask


def test_ask_spends_one_call_on_a_good_reply():
    p = _Provider('{"queries": ["NVDA news"]}')
    assert structured.ask(p, "k", "sys", "usr", _Plan).queries == ["NVDA news"]
    (api_key, model, system, messages), = p.calls
    assert (api_key, model, system) == ("k", "cheap-model", "sys")
    assert messages == [{"role": "user", "content": "usr"}]


def test_ask_repairs_an_off_contract_reply():
    p = _Provider("I think NVDA news", '{"queries": ["NVDA news"]}')
    assert structured.ask(p, "k", "sys", "usr", _Plan).queries == ["NVDA news"]
    assert len(p.calls) == 2
    first, repair = (c[3] for c in p.calls)
    assert repair[:1] == first  # the question is asked again, not paraphrased
    assert repair[1] == {"role": "assistant", "content": "I think NVDA news"}
    assert "rejected" in repair[2]["content"]
    assert "no JSON object" in repair[2]["content"]  # says what was wrong
    assert p.calls[1][2] == "sys"  # same system prompt, so same contract


def test_ask_shows_an_empty_reply_back_as_something_the_model_can_read():
    p = _Provider("", '{"queries": []}')
    structured.ask(p, "k", "sys", "usr", _Plan)
    assert p.calls[1][3][1]["content"] == "(empty)"


def test_ask_trims_a_rambling_reply_before_quoting_it_back():
    p = _Provider("x" * 5000, '{"queries": []}')
    structured.ask(p, "k", "sys", "usr", _Plan)
    assert len(p.calls[1][3][1]["content"]) == 500


def test_ask_gives_up_after_the_repair():
    p = _Provider("nope", "still nope")
    with pytest.raises(OffContract) as err:
        structured.ask(p, "k", "sys", "usr", _Plan)
    assert err.value.raw == "still nope"  # the reply the caller may re-read
    assert len(p.calls) == 2


def test_ask_can_be_told_not_to_repair():
    p = _Provider("nope", '{"queries": []}')
    with pytest.raises(OffContract):
        structured.ask(p, "k", "sys", "usr", _Plan, repair=False)
    assert len(p.calls) == 1


def test_ask_lets_provider_errors_through_untouched():
    # A rate limit is not a contract problem: retrying it here would spend a
    # second doomed call and hide the reason from the caller.
    p = _Provider(exc=RuntimeError("rate limited"))
    with pytest.raises(RuntimeError, match="rate limited"):
        structured.ask(p, "k", "sys", "usr", _Plan)
    assert len(p.calls) == 1


def test_ask_honours_an_explicit_model_over_the_cheapest_one():
    p = _Provider('{"queries": []}')
    structured.ask(p, "k", "sys", "usr", _Plan, model="big-model")
    assert p.calls[0][1] == "big-model"
