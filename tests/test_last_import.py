"""Last-import record: save/load/forget roundtrip, corrupt-file tolerance."""

from stocks.portfolio.last_import import ImportRecord, forget, load, save


def test_record_roundtrip(tmp_path):
    path = tmp_path / "last_import.json"
    assert load(path) is None  # absent -> None

    rec = ImportRecord(
        filename="statement.csv",
        imported_at="2026-08-03T10:00:00+00:00",
        tx_ids=[4, 5, 6],
        wiped=True,
    )
    save(rec, path)
    got = load(path)
    assert got == rec

    forget(path)
    assert load(path) is None
    forget(path)  # idempotent on missing file


def test_load_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "last_import.json"
    path.write_text("{not json")
    assert load(path) is None
    path.write_text('{"unexpected": "keys"}')
    assert load(path) is None
