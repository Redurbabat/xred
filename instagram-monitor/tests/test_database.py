"""Tests für die Datenbank: soziale Listen, NEU-Logik, Favoriten, Verlauf, Backup."""

from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "monitor.db")
    yield database
    database.close()


def _names(rows):
    return [r["username"] for r in rows]


def test_social_list_roundtrip(db: Database):
    db.replace_social_list("following", [
        {"username": "Alice", "full_name": "Alice A", "pic": "u1"},
        {"username": "bob", "full_name": "Bob B", "pic": ""},
    ])
    rows = db.get_social_list("following")
    assert _names(rows) == ["alice", "bob"]           # kleingeschrieben, Reihenfolge erhalten
    assert rows[0]["full_name"] == "Alice A"
    assert db.social_fetched_at("following") != ""    # Zeitstempel gesetzt


def test_social_version_increases(db: Database):
    v0 = db.social_version()
    db.replace_social_list("followers", [{"username": "x", "pic": ""}])
    assert db.social_version() > v0


def test_is_new_needs_seen_marker(db: Database):
    db.replace_social_list("following", [{"username": "alice", "pic": ""}])
    # Beitrag bekannt, aber nie „gesehen“ → gilt NICHT als neu (Baseline-Prinzip).
    db.add_post("P1", "alice", "Bild", "2026-07-10T10:00:00")
    row = db.get_social_list("following")[0]
    assert row["seen_post_at"] is None

    # Gesehen setzen, dann ein NEUERER Beitrag → is_new-Bedingung erfüllt.
    db.mark_account_seen("alice")
    db.add_post("P2", "alice", "Bild", "2026-07-15T10:00:00")
    row = db.get_social_list("following")[0]
    assert row["last_post_at"] == "2026-07-15T10:00:00"
    assert row["seen_post_at"] == "2026-07-10T10:00:00"
    assert row["last_post_at"] > row["seen_post_at"]   # → is_new im Web-Layer


def test_favorite_and_group(db: Database):
    db.replace_social_list("following", [{"username": "carol", "pic": ""}])
    db.set_favorite("carol", True)
    db.set_group("carol", "Familie")
    row = db.get_social_list("following")[0]
    assert row["favorite"] == 1
    assert row["group_name"] == "Familie"
    db.set_favorite("carol", False)
    assert db.get_social_list("following")[0]["favorite"] == 0


def test_story_flag_is_exclusive(db: Database):
    db.replace_social_list("following", [
        {"username": "a", "pic": ""}, {"username": "b", "pic": ""},
    ])
    db.set_story_usernames(["a"])
    rows = {r["username"]: r["has_story"] for r in db.get_social_list("following")}
    assert rows["a"] == 1 and rows["b"] == 0
    # Neue Story-Leiste ohne "a": das Flag muss dort verschwinden.
    db.set_story_usernames(["b"])
    rows = {r["username"]: r["has_story"] for r in db.get_social_list("following")}
    assert rows["a"] == 0 and rows["b"] == 1


def test_social_events_added_and_removed(db: Database):
    db.replace_social_list("following", [
        {"username": "a", "pic": ""}, {"username": "b", "pic": ""},
    ])
    # Erste Liste erzeugt KEINE Events (sonst wäre alles „neu“).
    assert db.social_events() == []
    db.replace_social_list("following", [
        {"username": "a", "pic": ""}, {"username": "c", "pic": ""},
    ])  # b weg, c neu
    events = {(e["event"], e["username"]) for e in db.social_events()}
    assert ("added", "c") in events
    assert ("removed", "b") in events


def test_pending_downloads(db: Database):
    db.add_post("S1", "alice", "Reel", "2026-07-16T08:00:00", download_pending=True)
    db.add_post("S2", "bob", "Bild", "2026-07-16T09:00:00")   # nicht vorgemerkt
    pending = db.all_pending_downloads()
    assert _names(pending) == ["alice"]
    db.mark_downloaded("S1", "alice")
    assert db.all_pending_downloads() == []


def test_find_post_reconstructs_after_restart(db: Database):
    # Nach einem Neustart ist der In-Memory-Cache leer; find_post liefert
    # aus der DB genug, um Anzeige/Download wieder zu ermöglichen.
    db.add_post("ABC", "alice", "Reel", "2026-07-16T08:00:00")
    row = db.find_post("ABC")
    assert row is not None
    assert row["username"] == "alice"
    assert row["post_type"] == "Reel"
    assert row["posted_at"] == "2026-07-16T08:00:00"
    assert db.find_post("does-not-exist") is None


def test_backup_creates_valid_copy(db: Database, tmp_path: Path):
    db.add_post("S1", "alice", "Bild", "2026-07-16T08:00:00")
    target = tmp_path / "backups" / "copy.db"
    db.backup(target)
    assert target.exists()
    # Kopie öffnen und Inhalt prüfen.
    copy = Database(target)
    try:
        assert copy.is_post_known("S1", "alice")
    finally:
        copy.close()


def test_wal_mode_enabled(db: Database):
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
