"""Tests für Einstellungen, insbesondere die Ruhezeit-Logik (Nachtpause)."""

from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    db = Database(tmp_path / "s.db")
    yield Settings(db, default_download_dir=str(tmp_path / "dl"))
    db.close()


def test_defaults(settings: Settings):
    assert settings.check_interval_minutes == 5
    assert settings.social_auto_refresh is True
    assert settings.social_refresh_minutes == 60


def test_refresh_minutes_minimum(settings: Settings):
    settings.social_refresh_minutes = 1  # unter dem Minimum
    assert settings.social_refresh_minutes == 10


def test_quiet_hours_overnight(settings: Settings):
    settings.quiet_enabled = True
    settings.quiet_from = "23:00"
    settings.quiet_until = "07:00"
    assert settings.in_quiet_hours("23:30") is True
    assert settings.in_quiet_hours("03:00") is True
    assert settings.in_quiet_hours("06:59") is True
    assert settings.in_quiet_hours("07:00") is False  # Ende = nicht mehr Pause
    assert settings.in_quiet_hours("12:00") is False


def test_quiet_hours_same_day_window(settings: Settings):
    settings.quiet_enabled = True
    settings.quiet_from = "01:00"
    settings.quiet_until = "05:00"
    assert settings.in_quiet_hours("02:00") is True
    assert settings.in_quiet_hours("06:00") is False


def test_quiet_hours_disabled(settings: Settings):
    settings.quiet_enabled = False
    settings.quiet_from = "23:00"
    settings.quiet_until = "07:00"
    assert settings.in_quiet_hours("03:00") is False


def test_time_normalization(settings: Settings):
    settings.quiet_from = "9:5"
    assert settings.quiet_from == "09:05"
