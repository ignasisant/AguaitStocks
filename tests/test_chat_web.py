"""chat_web: defensive query parsing, planner plumbing, prompt assembly."""

import json

from stocks.web import chat_web
from stocks.web.chat_web import Result, augment, parse_queries, plan, sources

# ------------------------------------------------------------------ parse


def test_parse_valid_json():
    raw = '{"queries": ["NVDA earnings 2026", "fed rate decision"]}'
    assert parse_queries(raw) == ["NVDA earnings 2026", "fed rate decision"]


def test_parse_tolerates_fences_and_prose():
    raw = 'Sure! ```json\n{"queries": ["ASML news"]}\n```'
    assert parse_queries(raw) == ["ASML news"]


def test_parse_empty_list_means_no_search():
    assert parse_queries('{"queries": []}') == []


def test_parse_garbage_means_no_search():
    assert parse_queries("no json here") == []
    assert parse_queries('{"queries": "not a list"}') == []
    assert parse_queries('["bare", "list"]') == []
    assert parse_queries("{broken json") == []


def test_parse_cleans_dedupes_and_caps():
    raw = json.dumps({
        "queries": [
            "  spaced   out\nquery  ",
            "spaced out query",  # dup after whitespace collapse
            42,  # non-string
            "",  # empty
            "x" * 500,  # overlong -> truncated
            "one too many",  # beyond MAX_QUERIES
        ]
    })
    got = parse_queries(raw)
    assert got[0] == "spaced out query"
    assert len(got) == chat_web.MAX_QUERIES
    assert all(len(q) <= 200 for q in got)


# ------------------------------------------------------------------ plan


class _StubProvider:
    classifier_model = "stub-mini"

    def __init__(self, reply=None, boom=False):
        self.reply, self.boom = reply, boom
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append((api_key, model, system, messages))
        if self.boom:
            raise RuntimeError("network down")
        return self.reply


def test_plan_passes_context_and_parses():
    p = _StubProvider('{"queries": ["NVDA news today"]}')
    got = plan(p, "key", "any news on nvidia?", "The ticker in focus is NVDA.")
    assert got == ["NVDA news today"]
    (_, model, system, messages), = p.calls
    assert model == "stub-mini"
    assert "web search" in system
    assert "ticker in focus is NVDA" in messages[0]["content"]
    assert "User message: any news on nvidia?" in messages[0]["content"]


def test_plan_swallows_provider_errors():
    assert plan(_StubProvider(boom=True), "key", "news?") == []


# ------------------------------------------------------------------ search


def test_search_empty_queries_skips_network():
    assert chat_web.search([]) == []


# ------------------------------------------------------------------ prompt


def _hits():
    return [
        Result("Nvidia Q2 results", "https://example.com/nvda", "Beat estimates."),
        Result("Fed holds rates", "https://news.example.org/fed", ""),
    ]


def test_augment_appends_numbered_hits():
    out = augment("any news?", _hits())
    assert out.startswith("any news?")
    assert "1. Nvidia Q2 results — https://example.com/nvda" in out
    assert "Beat estimates." in out
    assert "2. Fed holds rates — https://news.example.org/fed" in out
    assert "cite the URLs" in out


def test_augment_without_hits_is_identity():
    assert augment("hola", []) == "hola"


def test_sources_compact_dicts():
    assert sources(_hits()) == [
        {"title": "Nvidia Q2 results", "url": "https://example.com/nvda"},
        {"title": "Fed holds rates", "url": "https://news.example.org/fed"},
    ]
