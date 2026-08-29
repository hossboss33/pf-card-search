"""Bulk-archive backfill: choosing the right zip, deriving provenance from
paths, and ingesting without losing a season to one bad file."""
import io
import zipfile
from pathlib import Path

import pytest

from carddb.bulk_sync import (choose_archive, ingest_archive,
                              school_team_from_path)
from carddb.config import load_config
from carddb.db import open_db
from carddb.ingest import IngestStats

from fixtures.docx_builders import build_verbatim, docx_bytes  # noqa: E402


def build_verbatim_docx():
    return docx_bytes(build_verbatim())


# --- picking an archive -----------------------------------------------------

def test_prefers_the_full_season_over_a_weekly_slice():
    """A -weekly- zip holds one week of changes; backfilling from it would
    silently miss the season."""
    files = [
        {"name": "hspf25-weekly-2026-08-25.zip", "url": "u1"},
        {"name": "hspf25-all-2026-08-18.zip", "url": "u2"},
    ]
    assert choose_archive(files)["url"] == "u2"


def test_picks_the_newest_full_archive():
    files = [
        {"name": "hspf25-all-2026-08-04.zip", "url": "old"},
        {"name": "hspf25-all-2026-08-25.zip", "url": "new"},
        {"name": "hspf25-all-2026-08-11.zip", "url": "mid"},
    ]
    assert choose_archive(files)["url"] == "new"


def test_falls_back_to_any_archive_when_no_full_one_exists():
    files = [{"name": "hspf25-weekly-2026-08-25.zip", "url": "w"}]
    assert choose_archive(files)["url"] == "w"


def test_no_archives_returns_none():
    assert choose_archive([]) is None


# --- provenance from paths --------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("Millburn/AB/1AC-Grid.docx", ("Millburn", "AB")),
    ("hspf25/Millburn/AB/1AC.docx", ("Millburn", "AB")),
    ("Millburn/1AC.docx", ("Millburn", None)),
    ("1AC.docx", (None, None)),
])
def test_school_and_team_come_from_the_path(path, expected):
    assert school_team_from_path(path) == expected


def test_a_flat_archive_invents_nothing():
    """No path structure means unknown school, not a guessed one."""
    assert school_team_from_path("just-a-file.docx") == (None, None)


# --- ingesting --------------------------------------------------------------

def _zip_with(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_ingests_cards_and_attaches_provenance(tmp_path):
    docx = build_verbatim_docx()
    zbytes = _zip_with({"Millburn/AB/1AC.docx": docx})
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(zbytes)

    conn = open_db(tmp_path / "t.sqlite")
    stats = IngestStats()
    ingest_archive(conn, load_config(), "hspf25", zpath, stats,
                   tmp_path / "raw", season=2025)

    assert stats.new_cards > 0
    school = conn.execute("SELECT name FROM schools").fetchone()
    team = conn.execute("SELECT name FROM teams").fetchone()
    assert school["name"] == "Millburn"
    assert team["name"] == "AB"
    # every card must be reachable from the school it came from
    n = conn.execute("""
        SELECT COUNT(DISTINCT v.card_id) FROM card_variants v
        JOIN rounds r ON r.id = v.round_id
        JOIN teams t ON t.id = r.team_id
        JOIN schools s ON s.id = t.school_id WHERE s.name = 'Millburn'
    """).fetchone()[0]
    assert n == stats.new_cards


def test_one_corrupt_file_does_not_lose_the_season(tmp_path):
    good = build_verbatim_docx()
    zbytes = _zip_with({
        "A/AB/broken.docx": b"this is not a docx at all",
        "B/CD/good.docx": good,
    })
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(zbytes)

    conn = open_db(tmp_path / "t.sqlite")
    stats = IngestStats()
    ingest_archive(conn, load_config(), "hspf25", zpath, stats, tmp_path / "raw")

    assert stats.failed == 1
    assert stats.parsed == 1
    assert stats.new_cards > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE parse_status='failed'"
    ).fetchone()[0] == 1


def test_rerunning_an_archive_adds_nothing(tmp_path):
    """The prime invariant still holds through the bulk path."""
    zbytes = _zip_with({"A/AB/1AC.docx": build_verbatim_docx()})
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(zbytes)
    conn = open_db(tmp_path / "t.sqlite")
    cfg = load_config()

    first = IngestStats()
    ingest_archive(conn, cfg, "hspf25", zpath, first, tmp_path / "raw")
    second = IngestStats()
    ingest_archive(conn, cfg, "hspf25", zpath, second, tmp_path / "raw")

    assert first.new_cards > 0
    assert second.new_cards == 0
    assert second.new_variants == 0
    assert second.units_skipped == 1


def test_non_card_files_are_ignored(tmp_path):
    zbytes = _zip_with({
        "A/AB/notes.txt": b"not a card",
        "A/AB/1AC.docx": build_verbatim_docx(),
    })
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(zbytes)
    conn = open_db(tmp_path / "t.sqlite")
    stats = IngestStats()
    ingest_archive(conn, load_config(), "hspf25", zpath, stats, tmp_path / "raw")
    assert stats.units_seen == 1        # the .txt was never a candidate
