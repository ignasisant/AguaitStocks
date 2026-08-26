"""Picker search ordering — which of the two searched tiers leads, and how a
worldwide row is labelled. Pure functions, no Streamlit runtime, no network.
"""

import pytest

from stocks.web.widgets import _world_first, _world_label

# What sec_matches actually returns for each query, real rows from the SEC map.
MIPS_SEC = [
    ("VIPS", "Vipshop Holdings Ltd"),
    ("CMPS", "COMPASS Pathways plc"),
    ("EMIS", "Emmis Acquisition Corp."),
    ("MVIS", "Microvision, Inc."),
]
IWDA_SEC = [
    ("IDA", "Idacorp Inc"),
    ("WDAY", "Workday, Inc."),
    ("IDYA", "IDEAYA Biosciences, Inc."),
    ("IACO", "Idea Acquisition Corp."),
]


@pytest.mark.parametrize(
    "q, sec",
    [
        # Nothing US-listed is close — the SEC rows are pure fuzz and the one
        # real answer (MIPS.ST, IWDA.AS) lives in the worldwide tier.
        ("MIPS", MIPS_SEC),
        ("IWDA", IWDA_SEC),
        # The query buried mid-name proves nothing: Hermès is RMS.PA, not the
        # asset manager whose title happens to contain the word.
        ("HERMES", [("FHI", "Federated Hermes, Inc.")]),
        ("7A1", []),
    ],
)
def test_world_leads_when_sec_only_fuzzed(q, sec):
    assert _world_first(q, sec) is True


@pytest.mark.parametrize(
    "q, sec",
    [
        ("NVDA", [("NVDA", "Nvidia Corp")]),  # exact symbol
        ("AIRBNB", [("ABNB", "Airbnb, Inc.")]),  # name starts with the query
        ("SANDISC", [("SNDK", "Sandisk Corp"), ("LVS", "Las Vegas Sands Corp")]),
        ("ORACEL", [("ORCL", "Oracle Corp")]),  # typo on the opening word
        ("BANK OF AMRICA", [("BAC", "BANK OF AMERICA CORP /DE/")]),  # multi-word
    ],
)
def test_sec_leads_when_it_nailed_the_query(q, sec):
    assert _world_first(q, sec) is False


def test_label_carries_exchange_and_clips_long_names():
    assert _world_label("MIPS.ST", "Mips AB (publ)", "Stockholm") == (
        "🌐 **MIPS.ST**  Mips AB (publ) · Stockholm"
    )
    long = "Hermès International Société en commandite par actions"
    label = _world_label("RMS.PA", long, "Paris")
    assert label.endswith("… · Paris")
    assert len(label.split("**  ")[1]) <= 34 + len(" · Paris")
