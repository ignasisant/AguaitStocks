"""Keyless macro sources — CSV/JSON-stat parsing, caching, and the two
landmines that cost real debugging time: FRED's User-Agent block and
Eurostat's frozen HICP dataset.

No network: every test drives `stocks.data.macro` through a stubbed
`get_bytes`, so the parsing and the cache behaviour are pinned even when the
upstream hosts are unreachable or throttling.
"""

import json
import os
import time

import numpy as np
import pandas as pd
import pytest

from stocks.data import macro

FRED_CSV = b"""observation_date,DGS10
2026-08-31,4.75
2026-09-01,4.79
2026-09-02,
2026-09-03,4.81
"""


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Every test gets an empty cache directory of its own."""
    monkeypatch.setattr(macro, "CACHE_DIR", tmp_path / "macro")
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    """Record every `get_bytes` call and serve a scripted reply per URL fragment.

    `calls.replies` maps a substring of the URL to bytes, to an exception to
    raise, or to a callable taking the URL; `calls.log` holds the
    (url, user_agent) pairs, which is how the User-Agent test below sees what
    actually went out. An unscripted URL fails the test rather than reaching
    the network.
    """

    class Recorder:
        def __init__(self):
            self.replies: dict[str, object] = {}
            self.log: list[tuple[str, str]] = []

        def __call__(self, url, *, user_agent=macro.USER_AGENT, timeout=30):
            self.log.append((url, user_agent))
            for fragment, reply in self.replies.items():
                if fragment in url:
                    if isinstance(reply, Exception):
                        raise reply
                    return reply(url) if callable(reply) else reply
            raise AssertionError(f"unscripted URL: {url}")

    recorder = Recorder()
    monkeypatch.setattr(macro, "get_bytes", recorder)
    return recorder


# ---------------------------------------------------------------- FRED parsing
def test_fred_parses_and_drops_blank_observations(calls):
    calls.replies["id=DGS10"] = FRED_CSV
    series = macro.fred("DGS10")
    # 2026-09-02 is blank in the fixture: FRED's "no observation" for a market
    # holiday, which must not become a zero or a forward-filled duplicate.
    assert list(series.index.strftime("%Y-%m-%d")) == [
        "2026-08-31", "2026-09-01", "2026-09-03"
    ]
    assert series.iloc[-1] == pytest.approx(4.81)
    assert series.name == "DGS10"


def test_fred_sends_the_user_agent_fred_does_not_block(calls):
    """FRED tarpits the package default UA — this is not a cosmetic header.

    A request headed `stocks-toolkit` is accepted and then never answered, so
    it surfaces as a read timeout that looks like a network fault. Verified
    deterministic against the live host. If someone drops the argument to
    "use the shared default", every rates card on the page goes blank after a
    30-second hang, so the header is pinned by a test.
    """
    calls.replies["id=DGS10"] = FRED_CSV
    macro.fred("DGS10")
    _url, agent = calls.log[0]
    assert agent == macro.USER_AGENT
    assert agent != "stocks-toolkit"


def test_fred_trims_the_download_to_the_window(calls):
    calls.replies["id=DGS10"] = FRED_CSV
    macro.fred("DGS10", years=2)
    url, _agent = calls.log[0]
    # `cosd` keeps a 16k-row full history off the wire.
    assert "cosd=" in url


def test_fred_failure_is_an_empty_series(calls):
    calls.replies["id=DGS10"] = OSError("boom")
    assert macro.fred("DGS10").empty


def test_fred_garbage_body_is_an_empty_series(calls):
    calls.replies["id=DGS10"] = b"observation_date,DGS10\n"
    assert macro.fred("DGS10").empty


def test_fred_many_returns_one_entry_per_id(calls):
    calls.replies["id=DGS10"] = FRED_CSV
    calls.replies["id=NOPE"] = OSError("boom")
    result = macro.fred_many(["DGS10", "NOPE"])
    assert set(result) == {"DGS10", "NOPE"}
    assert not result["DGS10"].empty
    assert result["NOPE"].empty


def test_fred_many_with_no_ids_makes_no_request(calls):
    assert macro.fred_many([]) == {}
    assert calls.log == []


# ----------------------------------------------------------------- disk cache
def test_second_read_is_served_from_disk(calls):
    calls.replies["id=DGS10"] = FRED_CSV
    macro.fred("DGS10")
    macro.fred("DGS10")
    assert len(calls.log) == 1


def test_a_failed_refresh_serves_the_stale_copy(calls):
    """A throttled host should show yesterday's curve, not an empty card."""
    calls.replies["id=DGS10"] = FRED_CSV
    fresh = macro.fred("DGS10")

    # Age the entry past its ttl so the next read attempts a refresh, then make
    # that refresh fail: the stored body must still be what comes back.
    path = next(macro.CACHE_DIR.glob("fred_DGS10_3y.cache"))
    os.utime(path, (time.time() - 10 * macro.TTL_S,) * 2)
    calls.replies["id=DGS10"] = OSError("throttled")
    pd.testing.assert_series_equal(macro.fred("DGS10"), fresh)


def test_a_failure_with_nothing_cached_writes_no_entry(calls):
    calls.replies["id=DGS10"] = OSError("offline")
    assert macro.fred("DGS10").empty
    assert list(macro.CACHE_DIR.glob("*.cache")) == []


# ------------------------------------------------------------------------ yoy
def test_yoy_is_a_percentage_over_twelve_observations():
    levels = pd.Series(
        np.linspace(100.0, 113.0, 14),
        index=pd.period_range("2025-08", periods=14, freq="M").to_timestamp(),
    )
    rate = macro.yoy(levels)
    # 12 months on from 100 the index is 112, so +12% quoted in percent.
    assert rate.iloc[0] == pytest.approx(12.0)
    assert len(rate) == 2


def test_yoy_of_nothing_is_empty():
    assert macro.yoy(pd.Series(dtype=float)).empty


# ------------------------------------------------------------- JSON-stat cube
def _cube(values: dict[str, float]) -> dict:
    """A 1x1x1x2x3 JSON-stat cube: two areas over three months.

    Shaped exactly like Eurostat's HICP reply so the offset arithmetic under
    test is the real thing — the cube is a sparse map from one row-major
    offset into the dimension sizes, and decoding it wrong quietly transposes
    countries against months.
    """
    return {
        "updated": "2026-09-01T23:00:00+0200",
        "id": ["freq", "unit", "coicop18", "geo", "time"],
        "size": [1, 1, 1, 2, 3],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}}},
            "unit": {"category": {"index": {"RCH_A": 0}}},
            "coicop18": {"category": {"index": {"TOTAL": 0}}},
            "geo": {"category": {"index": {"EA": 0, "ES": 1}}},
            "time": {
                "category": {
                    "index": {"2026-06": 0, "2026-07": 1, "2026-08": 2}
                }
            },
        },
        "value": values,
    }


def test_jsonstat_decodes_areas_against_months():
    # Row-major over [geo, time]: EA gets offsets 0-2, ES gets 3-5.
    frame = macro._jsonstat_frame(
        _cube({"0": 2.8, "1": 2.9, "2": 3.3, "3": 3.9, "4": 4.1, "5": 4.5}),
        value_dim="geo",
        row_dim="time",
    )
    assert list(frame.index) == ["2026-06", "2026-07", "2026-08"]
    assert frame.loc["2026-08", "EA"] == pytest.approx(3.3)
    assert frame.loc["2026-08", "ES"] == pytest.approx(4.5)


def test_jsonstat_leaves_unreported_cells_missing():
    frame = macro._jsonstat_frame(
        _cube({"0": 2.8, "3": 3.9}), value_dim="geo", row_dim="time"
    )
    assert frame.loc["2026-08"].isna().all()


def test_jsonstat_ignores_json_key_order():
    """Category maps are {code: position}; JSON key order means nothing.

    Eurostat returns the geo codes in query order, not the cube's order, so
    reading `index` as a sequence instead of sorting by position lines every
    country up against the wrong offsets.
    """
    cube = _cube({"0": 2.8, "1": 2.9, "2": 3.3, "3": 3.9, "4": 4.1, "5": 4.5})
    cube["dimension"]["geo"]["category"]["index"] = {"ES": 1, "EA": 0}
    frame = macro._jsonstat_frame(cube, value_dim="geo", row_dim="time")
    assert frame.loc["2026-08", "ES"] == pytest.approx(4.5)


# ------------------------------------------------------------------ HICP + mix
def test_hicp_pins_the_live_dataset_and_item_codes():
    """The frozen-dataset landmine, pinned.

    `prc_hicp_manr` (ECOICOP v1) stopped updating at 2025-12 with the February
    2026 methodology change and still answers 200 with stale data, so querying
    it looks healthy and is nine months behind. The live dataset is
    `prc_hicp_minr`, whose item dimension is `coicop18`.
    """
    assert macro.HICP_DATASET == "prc_hicp_minr"
    assert macro.HICP_HEADLINE == "TOTAL"
    assert macro.HICP_CORE == "TOT_X_NRG_FOOD"


def test_hicp_query_carries_the_pinned_codes(calls):
    calls.replies["prc_hicp_minr"] = json.dumps(
        _cube({"0": 2.8, "1": 2.9, "2": 3.3, "3": 3.9, "4": 4.1, "5": 4.5})
    ).encode()
    frame = macro.hicp(("EA", "ES"))
    url, _agent = calls.log[0]
    assert "coicop18=TOTAL" in url and "unit=RCH_A" in url
    assert "geo=EA" in url and "geo=ES" in url
    assert frame.loc["2026-08", "ES"] == pytest.approx(4.5)


def test_hicp_error_payload_is_an_empty_frame(calls):
    calls.replies["prc_hicp_minr"] = json.dumps(
        {"error": [{"status": 400, "label": "INVALID_QUERY_DIMENSION"}]}
    ).encode()
    assert macro.hicp(("EA",)).empty


def test_hicp_with_no_areas_makes_no_request(calls):
    assert macro.hicp(()).empty
    assert calls.log == []


def test_hicp_updated_reads_the_cached_stamp(calls):
    calls.replies["prc_hicp_minr"] = json.dumps(
        _cube({"0": 2.8, "1": 2.9, "2": 3.3, "3": 3.9, "4": 4.1, "5": 4.5})
    ).encode()
    macro.hicp(("EA", "ES"), periods=14)
    assert macro.hicp_updated(("EA", "ES"), periods=14).startswith("2026-09-01")


def test_inflation_keeps_each_area_on_its_own_reference_month(calls):
    """Eurostat and FRED do not publish together, so no shared month column.

    The euro-area flash estimate lands weeks before the US CPI print. A single
    "latest month" header would misdate one of them; each row carries its own.
    """
    headline = json.dumps(
        _cube({"0": 2.8, "1": 2.9, "2": 3.3, "3": 3.9, "4": 4.1, "5": 4.5})
    ).encode()
    core = json.dumps(
        _cube({"0": 2.2, "1": 2.3, "2": 2.4, "3": 3.3, "4": 3.4, "5": 3.5})
    ).encode()
    us_levels = b"observation_date,CPIAUCNS\n" + b"".join(
        f"2025-{m:02d}-01,{100 + m}\n".encode() for m in range(1, 13)
    ) + b"2026-01-01,115\n2026-02-01,116\n"

    calls.replies[macro.HICP_CORE] = core
    calls.replies[f"coicop18={macro.HICP_HEADLINE}"] = headline
    calls.replies["CPIAUCNS"] = us_levels
    calls.replies["CPILFENS"] = us_levels
    frame = macro.inflation(("EA", "ES"))
    rows = frame.set_index("area")
    assert rows.loc["EA", "period"] == "2026-08"
    assert rows.loc["ES", "headline"] == pytest.approx(4.5)
    assert rows.loc["ES", "core"] == pytest.approx(3.5)
    assert rows.loc["ES", "prior"] == pytest.approx(4.1)
    assert rows.loc["US", "period"] == "2026-02"
    assert list(rows.loc["ES", "path"])[-1] == pytest.approx(4.5)


def test_inflation_areas_exclude_the_uk():
    """Eurostat returns NaN for every UK observation post-Brexit.

    Asking for it only buys an empty row, so the area list leaves it out
    rather than rendering a blank line and looking broken.
    """
    assert "UK" not in macro.INFLATION_AREAS
    assert "EA" in macro.INFLATION_AREAS and "ES" in macro.INFLATION_AREAS
