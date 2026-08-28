"""chat tools: gate, defensive parsing, detection plumbing, execution."""

import json

import yaml

from stocks.chat import tools
from stocks.chat.tools import Action, detect, execute, maybe_action, parse_action

# ------------------------------------------------------------------ gate


def test_gate_hits_action_vocabulary_en_and_es():
    assert maybe_action("put this in favourites")
    assert maybe_action("add NVDA to favorites")
    assert maybe_action("set alert price for this at 120 at 140")
    assert maybe_action("avísame si baja de 100")
    assert maybe_action("ponlo en el grupo tech")
    assert maybe_action("etiqueta AAPL como dividendos")
    assert maybe_action("tag it as tech")


def test_gate_hits_the_watchlist_and_position_vocabulary():
    assert maybe_action("add NVDA to my watchlist")
    assert maybe_action("añade Telefónica a la lista")
    assert maybe_action("quita AAPL del seguimiento")
    assert maybe_action("stop tracking MSFT, remove it")
    assert maybe_action("I hold 12 shares of NVDA")
    assert maybe_action("tengo 30 acciones de SAN a precio medio 4,10")


def test_gate_skips_plain_questions():
    assert not maybe_action("what do you think of NVDA earnings?")
    assert not maybe_action("¿cómo va mi cartera hoy?")
    assert not maybe_action("is the market overvalued right now?")
    assert not maybe_action("¿qué tal el último informe de resultados?")


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


def test_parse_position_accepts_either_field():
    act = parse_action('{"action": "set_position", "ticker": "NVDA", "shares": "12.5"}')
    assert act.args == {"shares": 12.5}
    act = parse_action('{"action": "set_position", "ticker": "NVDA", "cost": 100}')
    assert act.args == {"cost": 100.0}


def test_parse_position_rejects_when_no_number_survives():
    assert parse_action('{"action": "set_position", "ticker": "NVDA"}') is None
    raw = '{"action": "set_position", "ticker": "NVDA", "shares": "lots"}'
    assert parse_action(raw) is None
    raw = '{"action": "set_position", "ticker": "NVDA", "shares": -3}'
    assert parse_action(raw) is None


def test_parse_add_ticker_name_is_optional():
    assert parse_action('{"action": "add_ticker", "ticker": "NVDA"}') == Action(
        "add_ticker", "NVDA", {})
    act = parse_action('{"action": "add_ticker", "ticker": "NVDA", "name": " NVIDIA "}')
    assert act.args == {"name": "NVIDIA"}


def test_parse_untag_needs_groups():
    assert parse_action('{"action": "untag", "ticker": "NVDA"}') is None
    act = parse_action('{"action": "untag", "ticker": "NVDA", "tags": ["ai"]}')
    assert act.tags == ["ai"]


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
    assert act.args == {} and act.alerts == [] and act.tags == []


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
    execute(Action("set_alerts", "NVDA", {"alerts": [
        {"type": "below", "price": 120.0},  # already there (int/float equal)
        {"type": "above", "price": 140.0},
    ]}), p)
    alerts = _read(p)["NVDA"]["alerts"]
    assert {"type": "rsi_below", "level": 30} in alerts  # history rule kept
    assert {"type": "above", "price": 140.0} in alerts
    assert sum(a["type"] == "below" for a in alerts) == 1  # not duplicated


def test_execute_tags_append(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "tags": ["ai"]}])
    execute(Action("tag", "NVDA", {"tags": ["tech", "AI"]}), p)
    assert _read(p)["NVDA"]["tags"] == ["ai", "tech"]  # dedup case-insensitive


def test_execute_untag_keeps_the_others(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "tags": ["ai", "tech"]}])
    execute(Action("untag", "NVDA", {"tags": ["TECH"]}), p)  # case-insensitive
    assert _read(p)["NVDA"]["tags"] == ["ai"]


def test_execute_untag_last_group_drops_the_key(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "tags": ["ai"]}])
    execute(Action("untag", "NVDA", {"tags": ["ai"]}), p)
    assert "tags" not in _read(p)["NVDA"]


def test_execute_add_ticker_creates_the_entry_with_a_name(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "AAPL"}])
    execute(Action("add_ticker", "NVDA", {"name": "NVIDIA"}), p)
    assert _read(p)["NVDA"]["name"] == "NVIDIA"


def test_execute_add_ticker_never_overwrites_an_existing_name(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "name": "My NVDA"}])
    execute(Action("add_ticker", "NVDA", {"name": "NVIDIA"}), p)
    assert _read(p)["NVDA"]["name"] == "My NVDA"


def test_execute_remove_ticker(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "AAPL"}, {"ticker": "NVDA"}])
    execute(Action("remove_ticker", "NVDA"), p)
    assert set(_read(p)) == {"AAPL"}


def test_execute_remove_ticker_never_creates_one(tmp_path):
    """The other mutators create the entry they touch; removal must not."""
    p = _watchlist(tmp_path, [{"ticker": "AAPL"}])
    execute(Action("remove_ticker", "MSFT"), p)
    assert set(_read(p)) == {"AAPL"}


def test_execute_set_position_leaves_the_field_it_was_not_given(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "shares": 5, "cost": 100}])
    execute(Action("set_position", "NVDA", {"shares": 12.5}), p)
    entry = _read(p)["NVDA"]
    assert entry["shares"] == 12.5 and entry["cost"] == 100


def test_execute_set_position_zero_clears(tmp_path):
    p = _watchlist(tmp_path, [{"ticker": "NVDA", "shares": 5, "cost": 100}])
    execute(Action("set_position", "NVDA", {"shares": 0.0}), p)
    entry = _read(p)["NVDA"]
    assert "shares" not in entry and entry["cost"] == 100


# ------------------------------------------------------------------ replies


def _translate(key, **kwargs):
    """Stand-in translator: shows the key and its filled slots."""
    return f"{key}({', '.join(f'{k}={v}' for k, v in sorted(kwargs.items()))})"


def test_reply_alerts_renders_every_rule():
    act = Action("set_alerts", "NVDA", {"alerts": [
        {"type": "below", "price": 120.0}, {"type": "above", "price": 140.0}]})
    out = tools.reply(act, _translate)
    assert "chat.action_alert_below(price=120)" in out
    assert "chat.action_alert_above(price=140)" in out
    assert out.startswith("chat.action_alerts_set(")


def test_reply_groups_join():
    act = Action("untag", "NVDA", {"tags": ["ai", "tech"]})
    assert tools.reply(act, _translate) == (
        "chat.action_untagged(groups=ai, tech, ticker=NVDA)")


def test_reply_position_lists_only_what_was_set():
    act = Action("set_position", "NVDA", {"shares": 12.0})
    out = tools.reply(act, _translate)
    assert "chat.action_position_shares(value=12)" in out
    assert "position_cost" not in out


def test_every_tool_has_a_locale_key():
    """A tool whose confirmation key is missing would answer with the raw key."""
    import json
    from pathlib import Path

    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"
         / "locales" / "en" / "chat.json").read_text(encoding="utf-8")
    )
    assert all(t.reply_key in catalog for t in tools.TOOLS.values())


# ------------------------------------------------------------------ catalog


def test_system_prompt_lists_every_tool():
    """The router can only pick an action the prompt told it about."""
    assert all(f'"{name}"' in tools._SYSTEM for name in tools.TOOLS)


def test_kinds_tracks_the_registry():
    assert set(tools.KINDS) == set(tools.TOOLS)
