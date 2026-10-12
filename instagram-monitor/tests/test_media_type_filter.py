"""Test: Der Medientyp-Filter („nur Fotos“ / „nur Videos“) trifft alle Typen.

Hintergrund: Der Filter prüfte auf den Typ „Video“, den die App gar nicht
vergibt – ``_classify_post`` liefert für ein normales Video „Video/Reel“.
„Nur Videos“ überging deshalb fast jedes Video und lud allein Reels.

Kein Netzwerk: Ein Fake-Downloader liefert feste Beiträge und merkt sich,
welche heruntergeladen wurden.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.database import Database
from app.downloader import PostInfo
from app.settings import Settings
from app.web import create_app

# Genau die vier Werte, die _classify_post erzeugen kann.
ALLE_TYPEN = ("Bild", "Album", "Video/Reel", "Reel")


class _FakeDownloader:
    is_logged_in = False        # hält den Hintergrund-Auffrischer ruhig
    logged_in_user = None

    def __init__(self) -> None:
        self.downloaded: list[str] = []

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def list_recent_posts(self, username, count):
        return [
            PostInfo(
                shortcode=f"code{index}",
                username=username,
                post_type=post_type,
                posted_at="2026-10-01T12:00:00",
                url=f"https://instagram.com/p/code{index}/",
            )
            for index, post_type in enumerate(ALLE_TYPEN)
        ]

    def download_post(self, post) -> bool:
        self.downloaded.append(post.post_type)
        return True


def _geladene_typen(tmp_path: Path, types: str) -> list[str]:
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    dl = _FakeDownloader()
    client = create_app(db, settings, dl, access_key=None).test_client()
    resp = client.post(
        "/api/download-following",
        json={"usernames": ["konto"], "types": types, "count": 10},
    )
    assert resp.status_code == 200
    ende = time.monotonic() + 3
    while time.monotonic() < ende:
        if client.get("/api/download-following/status").get_json()["finished"]:
            break
        time.sleep(0.01)
    db.close()
    return dl.downloaded


def test_only_videos_includes_plain_videos(tmp_path: Path):
    """Der eigentliche Fehler: „Video/Reel“ fehlte im Filter."""
    assert sorted(_geladene_typen(tmp_path, "videos")) == ["Reel", "Video/Reel"]


def test_only_photos_takes_pictures_and_albums(tmp_path: Path):
    assert sorted(_geladene_typen(tmp_path, "photos")) == ["Album", "Bild"]


@pytest.mark.parametrize("types", ["all", "everything"])
def test_without_a_filter_everything_comes_through(tmp_path: Path, types: str):
    assert sorted(_geladene_typen(tmp_path, types)) == sorted(ALLE_TYPEN)
