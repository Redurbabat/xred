"""Bereits auf der Platte liegende Beiträge gelten als „geladen".

Nach einer App-Neuinstallation ist die Datenbank leer, die Dateien liegen
aber weiter unter ``<download_dir>/<konto>/…``. ``/api/posts`` darf solche
Beiträge dann NICHT als „nicht geladen" (rot) anzeigen. Kein Netz/Instagram –
der Downloader ist ein Fake.
"""

from pathlib import Path

import pytest

from app.database import Database
from app.downloader import PostInfo
from app.settings import Settings
from app.web import create_app


class _FakeDownloader:
    is_logged_in = True
    logged_in_user = "me"

    def __init__(self, posts):
        self._posts = posts

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def list_recent_posts(self, username, count):
        return list(self._posts)


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    dl_dir = tmp_path / "dl"
    settings = Settings(db, default_download_dir=str(dl_dir))
    yield db, settings, dl_dir
    db.close()


def test_file_on_disk_counts_as_downloaded(env):
    db, settings, dl_dir = env
    posts = [
        PostInfo(shortcode="ABC", username="u", post_type="Bild",
                 posted_at="", url=""),
        PostInfo(shortcode="XYZ", username="u", post_type="Bild",
                 posted_at="", url=""),
    ]
    app = create_app(db, settings, _FakeDownloader(posts), access_key=None)
    # Datei für ABC liegt auf der Platte, ist aber NICHT in der Datenbank
    # vermerkt (wie nach einer Neuinstallation).
    f = dl_dir / "u" / "Fotos" / "ABC_1.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")

    res = app.test_client().get("/api/posts?username=u&count=2").get_json()
    by = {p["shortcode"]: p for p in res["posts"]}
    assert by["ABC"]["downloaded"] is True    # Datei vorhanden → geladen
    assert by["XYZ"]["downloaded"] is False   # keine Datei, nicht in DB


def test_db_mark_still_wins_without_file(env):
    db, settings, dl_dir = env
    posts = [PostInfo(shortcode="DB1", username="u", post_type="Bild",
                      posted_at="", url="")]
    app = create_app(db, settings, _FakeDownloader(posts), access_key=None)
    db.add_post("DB1", "u", "Bild", "")
    db.mark_downloaded("DB1", "u")            # nur DB, keine Datei
    res = app.test_client().get("/api/posts?username=u&count=1").get_json()
    assert res["posts"][0]["downloaded"] is True
