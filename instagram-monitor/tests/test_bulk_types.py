"""Test: Download-Auswahl „everything" holt Beiträge + Stories + Highlights.

Kein Netzwerk: ein Fake-Downloader protokolliert nur, welche Story-Arten
angefordert werden.
"""

import time
from pathlib import Path

from app.database import Database
from app.settings import Settings
from app.web import create_app


class _FakeDownloader:
    is_logged_in = False        # hält den Hintergrund-Auffrischer ruhig
    logged_in_user = None

    def __init__(self):
        self.story_kinds = []
        self.post_calls = 0

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def list_recent_posts(self, username, count):
        self.post_calls += 1
        return []               # keine Beiträge nötig für diesen Test

    def list_story_items(self, username, kind):
        self.story_kinds.append(kind)
        return []


def _run_bulk(tmp_path: Path, types: str):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    dl = _FakeDownloader()
    app = create_app(db, settings, dl, access_key=None)
    client = app.test_client()
    resp = client.post("/api/download-following",
                       json={"usernames": ["kcn.eliff"], "types": types,
                             "count": 3})
    assert resp.status_code == 200
    # Auf den Hintergrund-Worker warten (Fake ist instant, ein Konto → keine
    # Zwischen-Konten-Pause).
    for _ in range(200):
        st = client.get("/api/download-following/status").get_json()
        if st.get("finished"):
            break
        time.sleep(0.02)
    db.close()
    return dl


def test_everything_downloads_stories_and_highlights(tmp_path: Path):
    dl = _run_bulk(tmp_path, "everything")
    assert "stories" in dl.story_kinds
    assert "highlights" in dl.story_kinds
    assert dl.post_calls == 1        # Beiträge wurden ebenfalls abgefragt


def test_all_does_not_fetch_stories(tmp_path: Path):
    dl = _run_bulk(tmp_path, "all")
    assert dl.story_kinds == []      # „Alles" holt bewusst keine Stories
    assert dl.post_calls == 1
