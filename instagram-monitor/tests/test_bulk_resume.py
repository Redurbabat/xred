"""Fortsetzbarer Massen-Download.

Bricht ein Lauf über viele Konten ab, darf der nächste Start nicht wieder
bei Konto 1 beginnen, sondern dort, wo er aufgehört hat. Kein Netz: der
Downloader ist ein Fake.
"""

from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.web import create_app


class _Dl:
    is_logged_in = True
    logged_in_user = "me"

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def list_recent_posts(self, username, count):
        return []          # nichts zu laden → Konten laufen sofort durch


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    app = create_app(db, settings, _Dl(), access_key=None)
    yield app.test_client(), db
    db.close()


def test_resume_info_empty_without_job(env):
    client, _ = env
    data = client.get("/api/download-following/resume-info").get_json()
    assert data["available"] is False
    assert data["remaining"] == 0


def test_resume_continues_where_it_stopped(env):
    client, db = env
    # Einen unterbrochenen Auftrag simulieren: 5 Konten, 2 erledigt.
    import json
    db.set_setting("bulk_job_state", json.dumps({
        "usernames": ["a", "b", "c", "d", "e"], "done": 2,
        "count": 3, "since": "", "types": "all",
    }))
    info = client.get("/api/download-following/resume-info").get_json()
    assert info["available"] is True
    assert info["total"] == 5 and info["done"] == 2 and info["remaining"] == 3

    res = client.post("/api/download-following/resume")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["total"] == 3          # nur die restlichen drei Konten
    assert body["resumed_from"] == 2


def test_resume_rejects_when_nothing_left(env):
    client, db = env
    import json
    db.set_setting("bulk_job_state", json.dumps({
        "usernames": ["a", "b"], "done": 2, "count": 3, "since": "",
        "types": "all",
    }))
    assert client.get(
        "/api/download-following/resume-info").get_json()["available"] is False
    assert client.post("/api/download-following/resume").status_code == 400
