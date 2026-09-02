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

    def __init__(self, reply=None, boom=False, replies=None):
        self.reply, self.boom = reply, boom
        self.replies = list(replies) if replies else None
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append((api_key, model, system, messages))
        if self.boom:
            raise RuntimeError("network down")
        return self.replies.pop(0) if self.replies else self.reply


def test_plan_passes_context_and_parses():
    p = _StubProvider('{"queries": ["NVDA news today"]}')
    got = plan(p, "key", "any news on nvidia?", "The ticker in focus is NVDA.")
    assert got == ["NVDA news today"]
    (_, model, system, messages), = p.calls
    assert model == "stub-mini"
    assert "web search" in system
    assert "ticker in focus is NVDA" in messages[0]["content"]
    assert "User message: any news on nvidia?" in messages[0]["content"]


def test_plan_falls_back_to_heuristics_when_the_planner_dies():
    # A dead classifier model must cost relevance, not internet access.
    assert plan(_StubProvider(boom=True), "key", "any NVDA news?") == [
        "any NVDA news"
    ]


def test_plan_repairs_an_off_contract_reply_before_giving_up():
    p = _StubProvider(replies=["I would search for NVDA news",
                               '{"queries": ["NVDA earnings news"]}'])
    assert plan(p, "k", "NVDA news?") == ["NVDA earnings news"]
    assert len(p.calls) == 2


def test_plan_falls_back_when_the_planner_answers_off_contract_twice():
    # The heuristic decides only after the repair turn also comes back unusable.
    p = _StubProvider("I cannot help with that")
    assert plan(p, "k", "NVDA news?") == ["NVDA news"]
    assert len(p.calls) == 2


def test_plan_does_not_spend_a_repair_call_on_an_empty_plan():
    p = _StubProvider('{"queries": []}')
    assert plan(p, "k", "any news today?") == []
    assert len(p.calls) == 1


def test_plan_respects_an_explicit_empty_plan():
    # A well-formed "no search needed" is obeyed — no heuristic second-guess.
    assert plan(_StubProvider('{"queries": []}'), "k", "any news today?") == []


# ------------------------------------------------------------- heuristics


def test_heuristics_fire_on_time_sensitive_wording():
    assert chat_web.heuristic_queries("¿alguna noticia de Telefónica?") == [
        "alguna noticia de Telefónica"
    ]


def test_heuristics_add_focus_ticker_and_year():
    ctx = "Today is 2026-08-28.\nCurrent view: The ticker in focus is NVDA."
    assert chat_web.heuristic_queries("any news?", ctx) == [
        "NVDA any news 2026"
    ]


def test_heuristics_stay_quiet_on_timeless_questions():
    # "PER" is caps but not a ticker — the shared stop-list screens it.
    assert chat_web.heuristic_queries("¿qué es un PER?") == []
    assert chat_web.heuristic_queries("hola") == []


def test_heuristics_fire_on_a_bare_ticker():
    assert chat_web.heuristic_queries("ASML?") == ["ASML"]


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


# ----------------------------------------------------------- page reading

_PAGE = b"""<html><head><title>t</title><style>x{}</style></head><body>
<nav>Home About Subscribe</nav>
<article><p>Nvidia guided to 54 billion dollars in revenue for the quarter,
above the 52 billion consensus, and said data-centre demand stays ahead of
supply.</p><p>Shares rose 4% in after-hours trading on the print.</p></article>
<footer>Cookies</footer></body></html>"""


def test_extract_keeps_paragraphs_and_drops_chrome():
    text = chat_web._extract(_PAGE)
    assert "54 billion dollars" in text
    assert "after-hours" in text
    assert "Subscribe" not in text and "Cookies" not in text


_JUNKY = b"""<html><body>
<nav>Home About Subscribe</nav>
<div class="cookie-banner">We value your privacy. Accept all cookies to
continue reading this page and others like it on our network of sites.</div>
<article><h1>ASML books record orders</h1>
<p>ASML reported 9.2 billion euros of net bookings for the quarter, roughly
double the consensus estimate, and kept its full-year revenue guidance
unchanged at between 30 and 35 billion euros for the period.</p>
<p>Management said EUV demand from logic customers stayed firm while memory
orders remained the swing factor for the second half of the year.</p></article>
<aside><h2>Related</h2><p>Sign up for our free daily markets newsletter to get
this analysis in your inbox every single morning before the opening bell.</p>
</aside><footer>Cookies. Terms. Privacy.</footer></body></html>"""


def test_extract_drops_the_furniture_around_the_article():
    text = chat_web._extract(_JUNKY)
    assert "9.2 billion euros" in text
    assert "EUV demand" in text
    for junk in ("Subscribe", "value your privacy", "newsletter", "Terms"):
        assert junk not in text


def test_extract_keeps_paragraphs_apart():
    text = chat_web._extract(_JUNKY)
    assert "period.Management" not in text  # glued paragraphs read as one claim
    assert len([ln for ln in text.splitlines() if ln]) >= 2


def test_extract_falls_back_to_lxml_when_trafilatura_finds_nothing(monkeypatch):
    monkeypatch.setattr(chat_web, "_extract_trafilatura", lambda raw: "")
    text = chat_web._extract(_PAGE)
    assert "54 billion dollars" in text
    assert "Subscribe" not in text


def test_extract_keeps_a_short_trafilatura_hit_when_the_fallback_has_nothing():
    tiny = b"<html><body><article><p>Fed holds rates.</p></article></body></html>"
    assert "Fed holds rates" in chat_web._extract(tiny)


def test_extract_survives_bytes_that_are_not_html():
    """A PDF that lied about its Content-Type is "no text", never a traceback."""
    assert isinstance(chat_web._extract(b"%PDF-1.4 \x00\x01 binary"), str)
    assert chat_web._extract(b"") == ""


def test_extract_without_trafilatura_installed(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_trafilatura(name, *a, **kw):
        if name == "trafilatura":
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_trafilatura)
    assert "54 billion dollars" in chat_web._extract(_PAGE)


def test_extract_falls_back_to_whole_document_without_paragraphs():
    text = chat_web._extract(b"<html><body><div>" + b"bare text " * 30 +
                             b"</div></body></html>")
    assert "bare text" in text


def test_extract_truncates_to_the_prompt_budget():
    long = b"<html><body><p>" + b"word " * 5000 + b"</p></body></html>"
    assert len(chat_web._extract(long)) <= chat_web._PAGE_CHARS


def test_read_pages_fills_text_and_leaves_failures_alone(monkeypatch):
    monkeypatch.setattr(chat_web, "read_page",
                        lambda url, timeout=0: "article body" if "ok" in url else "")
    got = chat_web.read_pages([Result("A", "https://ok.example/a", "snip"),
                               Result("B", "https://paywall.example/b", "snip")])
    assert got[0].text == "article body"
    assert got[0].body == "article body"
    assert got[1].text == ""
    assert got[1].body == "snip"  # the DDG snippet still carries the hit


def test_read_pages_only_opens_the_budget(monkeypatch):
    opened = []
    monkeypatch.setattr(chat_web, "read_page",
                        lambda url, timeout=0: opened.append(url) or "text")
    hits = [Result(f"h{i}", f"https://e.example/{i}", "") for i in range(6)]
    chat_web.read_pages(hits, limit=2)
    assert len(opened) == 2


# ---------------------------------------------------------- pasted links


def test_urls_in_finds_dedupes_and_trims_punctuation():
    msg = ("mira https://example.com/a, y también https://example.com/a "
           "(https://ex.org/b).")
    assert chat_web.urls_in(msg) == ["https://example.com/a", "https://ex.org/b"]


def test_urls_in_ignores_plain_text():
    assert chat_web.urls_in("qué opinas de NVDA?") == []


def test_collect_reads_pasted_links_first_and_shares_the_budget(monkeypatch):
    monkeypatch.setattr(chat_web, "read_page", lambda url, timeout=0: "body")
    monkeypatch.setattr(
        chat_web, "search",
        lambda queries, read_limit=chat_web.READ_PAGES: [
            Result("hit", "https://found.example/1", "snip", "read"
                   if read_limit else "")
        ])
    got = chat_web.collect(["nvda news"], "read https://pasted.example/x please")
    assert got[0].url == "https://pasted.example/x"
    assert got[0].text == "body"
    assert got[1].url == "https://found.example/1"
