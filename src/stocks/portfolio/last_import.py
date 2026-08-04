"""Record of the most recent statement import (data/last_import.json).

Stores which ledger rows the last commit inserted, so the web Import page can
survive a reload (show what was imported without a re-upload) and undo exactly
that batch — "clear last import" — without touching the rest of the ledger.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from stocks.config import DATA_DIR

RECORD_PATH = DATA_DIR / "last_import.json"


@dataclass
class ImportRecord:
    filename: str
    imported_at: str  # ISO-8601 UTC
    tx_ids: list[int]
    wiped: bool = False  # ledger was wiped right before this import
    platform: str = "revolut"  # platforms.py key; default covers pre-field records


def save(record: ImportRecord, path: Path = RECORD_PATH) -> None:
    path.write_text(json.dumps(asdict(record), indent=2))


def load(path: Path = RECORD_PATH) -> ImportRecord | None:
    """The last import record, or None if absent/corrupt."""
    if not path.exists():
        return None
    try:
        return ImportRecord(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError):
        return None


def forget(path: Path = RECORD_PATH) -> None:
    path.unlink(missing_ok=True)
