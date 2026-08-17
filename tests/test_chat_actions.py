"""chat_actions: gate, defensive parsing, detection plumbing, execution."""

import json

import yaml

from stocks.web import chat_actions
from stocks.web.chat_actions import Action, detect, execute, maybe_action, parse_action


# ------------------------------------------------------------------ gate


def test_gate_hits_action_vocabulary_en_and_es():
    assert maybe_action("put this in favourites")
    assert maybe_action("add NVDA to favorites")
    assert maybe_action("set alert price for this at 120 at 140")
    assert maybe_action("avísame si baja de 100")
    assert maybe_action("ponlo en el grupo tech")
    assert maybe_action("etiqueta AAPL como dividendos")
    assert maybe_action("tag it as tech")


def test_gate_skips_plain_questions():
    assert not maybe_action("what do you think of NVDA earnings?")
    assert not maybe_action("¿cómo va mi cartera hoy?")


# ------------------------------------------------------------------ parse


def test_parse_favorite():
    act = parse_action('{"action": "favorite", "ticker": "nvda"}')
    assert act == Action("favorite", "NVDA")


def test_parse_alerts_dedupes_and_validates():
    raw = json.dumps({
        "action": "set_alerts",
        "ticker": "NVDA",
        "alerts": [
            {"type": "below", "price": 120},
            {"type": "above", "price": "140"},
            {"type": "above", "price": 140},  # dup
            {"type": "sideways", "price": 1},  # unknown type
            {"type": "above", "price": -5},  # non-positive
            {"type": "above"},  # no price
        ],
    })
    act = parse_action(raw)
    assert act.alerts == [
        {"type": "below", "price": 120.0},
        {"type": "above", "price": 140.0},
    ]


def test_parse_tags_cleaned():
    raw = '{"action": "tag", "ticker": "AAPL", "tags": [" tech ", "Tech", ""]}'
    act = parse_action(raw)
    assert act.tags == ["tech"]


def test_parse_ignores_surrounding_prose_and_fences():
    raw = 'Sure! ```json\n{"action": "favorite", "ticker": "BTC-EUR"}\n```'
    assert parse_action(raw) == Action("favorite", "BTC-EUR")


def test_parse_rejects_garbage():
    assert parse_action("no json here") is None
    assert parse_action('{"action": null}') is None
    assert parse_action('{"action": "favorite"}') is None  # no ticker
    assert parse_action('{"action": "favorite", "ticker": "not a ticker!"}') is None
    assert parse_action('{"action": "set_alerts", "ticker": "NVDA"}') is None
    assert parse_action('{"action": "tag", "ticker": "NVDA", "tags": []}') is None
    assert parse_action('{"action": "delete_account", "ticker": "NVDA"}') is None


def test_parse_drops_cross_kind_fields():
    raw = ('{"action": "favorite", "ticker": "NVDA", '
           '"alerts": [{"type": "above", "price": 1}], "tags": ["x"]}')
    act = parse_action(raw)
    assert act.alerts == [] and act.tags == []


# ------------------------------------------------------------------ detect


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


def test_detect_passes_context_and_parses():
    p = _StubProvider('{"action": "favorite", "ticker": "NVDA"}')
    act = detect(p, "key", "fav this", "The ticker in focus is NVDA.")
    assert act == Action("favorite", "NVDA")
    (_, model, system, messages), = p.calls
    assert model == "stub-mini"
    assert "ONE app action" in system
    assert "ticker in focus is NVDA" in messages[0]["content"]
    assert "User message: fav this" in messages[0]["content"]


def test_detect_swallows_provider_errors():
    assert detect(_StubProvider(boom=True), "key", "fav this") is None


# ------------------------------------------------------------------ execute


def _watchlist(tmp_path, items):
    p = tmp_path / "watchlist.yaml"
    p.write_text(yaml.safe_dump({"watchlist": items}))
    return p


def _read(p):
    return {i["ticker"]: i for i in yaml.safe_load(p.read_text())["watchlist"]}


def test_execute_favorite_idempotent_and_creates_entry(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "AAPL", "favorite": True}])
    execute(Action("favorite", "AAPL"), p)  # already set — no flip
    execute(Action("favorite", "NVDA"), p)  # not listed — entry created
    items = _read(p)
    assert items["AAPL"].get("favorite") is True
    assert items["NVDA"].get("favorite") is True
    execute(Action("unfavorite", "AAPL"), p)
    assert "favorite" not in _read(p)["AAPL"]


def test_execute_alerts_merge_with_existing(tmp_path):
    p = _watchlist(tmp_path, [
        {"ticker": "NVDA", "alerts": [{"type": "rsi_below", "level": 30},
                                      {"type": "below", "price": 120}]},
    ])
    execute(Action("set_alerts", "NVDA", alerts=[
        {"type": "below", "price": 120.0},  # already there (int/float equal)
        {"type": "above", "price": 140.0},
    ]), p)
    alerts = _read(p)["NVDA"]["alerts"]
    assert {"type": "rsi_below", "level": 30} in alerts  # history rule kept
    assert {"type": "above", "price": 140.0} in alerts
    assert sum(a["type"] == "below" for a in alerts) == 1  # not duplicated


def test_execute_tags_append(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "tags": ["ai"]}])
    execute(Action("tag", "NVDA", tags=["tech", "AI"]), p)
    assert _read(p)["NVDA"]["tags"] == ["ai", "tech"]  # dedup case-insensitive
