"""chat/market: ticker resolution, quote batching, prompt block."""

from stocks.chat import market
from stocks.chat.market import Quote, augment, mentioned, quotes

# ------------------------------------------------------------- mentions


def _no_lookup(name):  # a network lookup would be a test bug
    raise AssertionError(f"unexpected Yahoo lookup for {name!r}")


def test_caps_tokens_are_tickers():
    assert mentioned("what do you think of NVDA?", lookup=_no_lookup) == ["NVDA"]


def test_stop_list_screens_caps_that_are_not_tickers():
    assert mentioned("is this ETF ok for my IRPF?", lookup=lambda n: "") == []


def test_watchlist_names_resolve_without_a_lookup():
    known = {"SAN.MC": "SAN.MC", "BANCO SANTANDER": "SAN.MC"}
    assert mentioned("cómo va Banco Santander?", known, lookup=_no_lookup) == [
        "SAN.MC"
    ]


def test_focus_ticker_answers_a_message_with_no_ticker_in_it():
    assert mentioned("y hoy cómo va?", focus="ASML", lookup=_no_lookup) == ["ASML"]


def test_unknown_company_name_falls_back_to_one_lookup():
    seen = []

    def lookup(name):
        seen.append(name)
        return "NVDA" if name == "Nvidia" else ""

    assert mentioned("qué tal Nvidia últimamente?", lookup=lookup) == ["NVDA"]
    assert seen == ["Nvidia"]


def test_lookup_is_skipped_when_something_cheaper_matched():
    assert mentioned("NVDA vs Nvidia", lookup=_no_lookup) == ["NVDA"]


def test_mentions_are_deduped_and_capped():
    msg = "compare AAPL, MSFT, GOOG, AMZN and META"
    assert len(mentioned(msg, lookup=_no_lookup)) == market.MAX_TICKERS


# --------------------------------------------------------------- quotes


def test_quotes_skip_failures_and_keep_order():
    def fetch(ticker):
        if ticker == "BAD":
            raise RuntimeError("Yahoo throttled us")
        return Quote(ticker, price=1.0)

    got = quotes(["NVDA", "BAD", "ASML"], fetch=fetch)
    assert [q.ticker for q in got] == ["NVDA", "ASML"]


def test_quotes_skip_unquotable_symbols():
    got = quotes(["NOPE"], fetch=lambda t: None)
    assert got == []


def test_quotes_dedupe_and_cap():
    seen = []

    def fetch(ticker):
        seen.append(ticker)
        return Quote(ticker, price=1.0)

    quotes(["nvda", "NVDA", "a", "b", "c", "d"], fetch=fetch)
    assert seen == ["NVDA", "A", "B"][: market.MAX_TICKERS]


def test_quotes_without_tickers_skip_the_pool():
    assert quotes([], fetch=_no_lookup) == []


# --------------------------------------------------------------- prompt


def _quote():
    return Quote("NVDA", name="NVIDIA", price=227.98, currency="USD",
                 prev_close=226.15, year_high=236.54, year_low=164.07,
                 market_cap=5.5e12)


def test_line_carries_the_figures():
    line = _quote().line()
    assert "NVDA (NVIDIA)" in line
    assert "last 227.98 USD" in line
    assert "today +0.81%" in line
    assert "52w range 164.07–236.54" in line
    assert "market cap 5,500.0B" in line


def test_partial_quotes_still_render():
    assert Quote("XYZ", price=3.0).line() == "- XYZ: last 3.00"
    assert Quote("XYZ").line() == "- XYZ: no data"


def test_day_pct_needs_a_previous_close():
    assert Quote("XYZ", price=3.0).day_pct is None
    assert Quote("XYZ", price=3.0, prev_close=0).day_pct is None


def test_augment_appends_the_block():
    out = augment("cómo va nvidia?", [_quote()])
    assert out.startswith("cómo va nvidia?")
    assert "Live market data" in out
    assert "- NVDA (NVIDIA): last 227.98 USD" in out


def test_augment_without_quotes_is_identity():
    assert augment("hola", []) == "hola"


def test_lookup_for_swallows_failures(monkeypatch):
    monkeypatch.setattr(market, "quotes", lambda *a, **k: 1 / 0)
    assert market.lookup_for("NVDA?", None) == []
