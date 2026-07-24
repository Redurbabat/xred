"""Test für GET /save (Instagram-Link speichern) mit einem Fake-Downloader.

Fasst kein Instagram/Netzwerk an: ``download_by_shortcode`` wird ersetzt.
Geprüft wird die Endpunkt-Logik (Shortcode-Erkennung, DB-Eintrag, Antwort).
"""

from pathlib import Path

import pytest

from app.database import Database
from app.downloader import ProfileNotFound
from app.settings import Settings
from app.web import create_app


class _FakeDownloader:
    is_logged_in = False
    logged_in_user = None

    def __init__(self):
        self.calls = []

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def download_by_shortcode(self, shortcode):
        self.calls.append(shortcode)
        if shortcode == "MISSING":
            raise ProfileNotFound("Beitrag MISSING wurde nicht gefunden.")
        return {"shortcode": shortcode, "username": "elif.erbeyy",
                "type": "Reel", "posted_at": "2026-07-16T08:00:00"}


@pytest.fixture()
def client_and_deps(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    dl = _FakeDownloader()
    app = create_app(db, settings, dl, access_key=None)
    yield app.test_client(), db, dl
    db.close()


def test_save_without_link_is_rejected(client_and_deps):
    client, db, dl = client_and_deps
    resp = client.get("/save?u=nur+text+ohne+link")
    assert resp.status_code == 400
    assert dl.calls == []            # es wurde nichts geladen


def test_save_reel_link_downloads_and_records(client_and_deps):
    client, db, dl = client_and_deps
    resp = client.get(
        "/save?u=https://www.instagram.com/reel/ABC123/?igsh=xyz")
    assert resp.status_code == 200
    assert dl.calls == ["ABC123"]                        # Shortcode korrekt erkannt
    assert db.is_downloaded("ABC123", "elif.erbeyy")     # in der DB vermerkt
    # Reel → Antwort nennt den Videos-Ordner.
    assert "Videos" in resp.get_data(as_text=True)


def test_save_shares_text_field(client_and_deps):
    # Android-Teilen liefert den Link oft im Feld "text".
    client, db, dl = client_and_deps
    resp = client.get("/save?text=Schau: https://instagram.com/p/XyZ_9/")
    assert resp.status_code == 200
    assert dl.calls == ["XyZ_9"]


def test_save_reports_not_found(client_and_deps):
    client, db, dl = client_and_deps
    resp = client.get("/save?u=https://www.instagram.com/p/MISSING/")
    assert resp.status_code == 404
    assert not db.is_downloaded("MISSING", "elif.erbeyy")
