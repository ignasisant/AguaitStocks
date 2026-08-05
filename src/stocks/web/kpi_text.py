"""Web-side localization for KPI metadata.

`stocks.analysis.fundamentals.KPI_SOURCES` is the canonical, English source of
truth — the CLI shares it, so it stays English. The web layer localizes its
label / description / note / reliability-level and the sources table through
the i18n catalog (locales/<lang>/kpi.json), keyed by the same KPI keys. Any key
missing from the catalog falls back to the English value on the dataclass, so a
new KPI degrades gracefully instead of showing a raw key.
"""

from __future__ import annotations

import pandas as pd

from stocks.analysis.fundamentals import KPI_SOURCES
from stocks.web.i18n import t as tr


def _tr_or(key: str, fallback: str) -> str:
    """tr(key), but fall back to the given English string when the catalog has
    no entry (tr returns the raw key on a miss)."""
    s = tr(key)
    return fallback if s == key else s


def kpi_label(key: str) -> str:
    return _tr_or(f"kpi.{key}.label", KPI_SOURCES[key].label)


def kpi_desc(key: str) -> str:
    return _tr_or(f"kpi.{key}.desc", KPI_SOURCES[key].desc)


def kpi_note(key: str) -> str:
    note = KPI_SOURCES[key].note
    return _tr_or(f"kpi.{key}.note", note) if note else ""


def kpi_level(level: str) -> str:
    return _tr_or(f"kpi.level.{level}", level)


def sources_table() -> pd.DataFrame:
    """Localized version of fundamentals.sources_table(): translated KPI label,
    reliability level and column headers; loader/verify stay as tool names."""
    return pd.DataFrame(
        {
            tr("kpi.col_kpi"): [kpi_label(k) for k in KPI_SOURCES],
            tr("kpi.col_level"): [kpi_level(s.level) for s in KPI_SOURCES.values()],
            tr("kpi.col_loaded"): [s.loader for s in KPI_SOURCES.values()],
            tr("kpi.col_verify"): [s.verify for s in KPI_SOURCES.values()],
            tr("kpi.col_note"): [kpi_note(k) for k in KPI_SOURCES],
        }
    )
