"""Tests für die Anfragen-Bremse und das Zurückweichen bei Drosselung.

Diese Tests fassen KEIN Instagram an – sie prüfen nur die interne Logik
(_pace / _register_throttle / _register_success) der Downloader-Klasse.
"""

import time
from pathlib import Path

import pytest

from app.database import Database
from app.downloader import InstagramDownloader, TemporaryError
from app.settings import Settings


@pytest.fixture()
def downloader(tmp_path: Path) -> InstagramDownloader:
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    dl = InstagramDownloader(settings, session_dir=tmp_path / "sessions")
    yield dl
    db.close()


class _Fake(Exception):
    """Platzhalter für eine Instaloader-Verbindungs-Ausnahme."""


def test_throttle_starts_backoff(downloader: InstagramDownloader):
    downloader._register_throttle(_Fake("HTTP Error 429 Too Many Requests"))
    st = downloader.backoff_status()
    assert st["active"] is True
    assert 110 <= st["remaining_seconds"] <= 121   # erste Stufe ~2 Minuten


def test_backoff_doubles(downloader: InstagramDownloader):
    downloader._register_throttle(_Fake("429"))
    downloader._register_throttle(_Fake("please wait a few minutes"))
    st = downloader.backoff_status()
    assert 230 <= st["remaining_seconds"] <= 241   # verdoppelt auf ~4 Minuten


def test_pace_blocks_during_backoff(downloader: InstagramDownloader):
    downloader._register_throttle(_Fake("429"))
    with pytest.raises(TemporaryError):
        downloader._pace()


def test_success_resets_backoff(downloader: InstagramDownloader):
    downloader._register_throttle(_Fake("429"))
    downloader._register_success()
    assert downloader.backoff_status()["active"] is False


def test_normal_error_no_backoff(downloader: InstagramDownloader):
    # Ein gewöhnlicher Netzwerkfehler (ohne Drossel-Marker) darf KEIN
    # Zurückweichen auslösen.
    downloader._register_throttle(_Fake("connection reset by peer"))
    assert downloader.backoff_status()["active"] is False


def test_minimum_gap_between_requests(downloader: InstagramDownloader):
    t0 = time.monotonic()
    downloader._pace()
    downloader._pace()
    assert time.monotonic() - t0 >= 1.0   # Mindestabstand eingehalten


def test_checkpoint_becomes_login_error(downloader: InstagramDownloader):
    # Instagrams "checkpoint_required" muss zu einem klaren LoginError mit
    # Anleitung werden – nicht zu einem Absturz.
    from app.downloader import LoginError

    exc = _Fake('400 Bad Request - "fail" status, message "checkpoint_required"')
    result = downloader._fetch_error(exc, "Abo-Liste")
    assert isinstance(result, LoginError)
    assert "Sicherheitsbest" in str(result) or "Checkpoint" in str(result)


def test_normal_fetch_error_is_temporary(downloader: InstagramDownloader):
    from app.downloader import TemporaryError

    exc = _Fake("irgendein netzwerkfehler")
    result = downloader._fetch_error(exc, "Abo-Liste")
    assert isinstance(result, TemporaryError)
