"""Tests für die automatische Auffrischung der Gefolgt-/Follower-Listen.

Hintergrund: Die Fälligkeitsprüfung verglich einen naiven ``datetime.now()``
mit dem zeitzonenbehafteten Zeitstempel aus der Datenbank. Das warf einen
TypeError, der nicht abgefangen wurde – ab dem ersten gespeicherten Stand
schlug JEDE Prüfung fehl und die Listen wurden nie wieder automatisch
aufgefrischt. Diese Tests halten das fest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.social_cache import SocialCache


class _Downloader:
    """Reicht als Gegenstelle: Die Fälligkeitsprüfung geht nie ins Netz."""

    is_logged_in = True


@pytest.fixture()
def cache(tmp_path: Path):
    db = Database(tmp_path / "s.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    yield SocialCache(db, settings, _Downloader(), tmp_path / "avatars"), db, settings
    db.close()


def _store(db: Database, when: str) -> None:
    """Setzt den Stand beider Listen auf einen festen Zeitpunkt."""
    for kind in ("following", "followers"):
        db.replace_social_list(kind, [{"username": "a"}])
        db.set_setting(f"social_{kind}_fetched_at", when)


def test_never_fetched_is_due(cache):
    social, _db, _settings = cache
    assert social._refresh_due() is True


def test_a_fresh_list_is_not_due_and_raises_nothing(cache):
    """Der eigentliche Fehler: Hier flog früher ein TypeError."""
    social, db, _settings = cache
    _store(db, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert social._refresh_due() is False


def test_an_old_list_becomes_due_again(cache):
    """Und hier zeigte sich die Folge: Es wurde nie wieder aufgefrischt."""
    social, db, settings = cache
    settings.social_refresh_minutes = 60
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    _store(db, old.isoformat(timespec="seconds"))
    assert social._refresh_due() is True


def test_timestamps_without_a_zone_are_read_as_utc(cache):
    """Altbestand aus früheren Versionen hat keine Zeitzone."""
    social, db, settings = cache
    settings.social_refresh_minutes = 60
    naive_old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None)
    _store(db, naive_old.isoformat(timespec="seconds"))
    assert social._refresh_due() is True

    naive_fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    _store(db, naive_fresh.isoformat(timespec="seconds"))
    assert social._refresh_due() is False


def test_unreadable_timestamp_triggers_a_refresh(cache):
    social, db, _settings = cache
    _store(db, "kein-datum")
    assert social._refresh_due() is True


def test_switched_off_or_logged_out_is_never_due(cache, tmp_path: Path):
    social, db, settings = cache
    _store(db, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    settings.social_auto_refresh = False
    assert social._refresh_due() is False

    settings.social_auto_refresh = True
    logged_out = SocialCache(db, settings, _Downloader(), tmp_path / "a2")
    logged_out._downloader.is_logged_in = False
    assert logged_out._refresh_due() is False


def test_the_database_stores_a_zone_aware_timestamp(tmp_path: Path):
    """Sichert die Annahme ab, auf der die Prüfung beruht."""
    db = Database(tmp_path / "t.db")
    db.replace_social_list("following", [{"username": "a"}])
    stored = db.social_fetched_at("following")
    assert datetime.fromisoformat(stored).tzinfo is not None
    db.close()
