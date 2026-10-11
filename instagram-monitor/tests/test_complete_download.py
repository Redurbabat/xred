"""Komplett-Download („wirklich alles") + Erledigt-Liste.

Geprüft werden: der neue Medientyp „highlights", der „complete"-Modus,
die Erledigt-Vermerke in der Datenbank und die zugehörigen Endpunkte.
Kein Netz/Instagram – der Downloader ist ein Fake.
"""

import time
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.web import create_app


class _Dl:
    """Downloader-Attrappe: liefert nichts, damit ein Auftrag sofort fertig
    ist (der Massen-Download läuft in einem Hintergrund-Thread)."""

    is_logged_in = True
    logged_in_user = "me"

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def following_usernames(self):
        return ["a", "b", "c"]

    def list_recent_posts(self, username, count):
        return []

    def list_story_items(self, username, kind):
        return []


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    app = create_app(db, settings, _Dl(), access_key=None)
    yield app.test_client(), db
    db.close()


def test_complete_marks_and_lists_accounts(env):
    _, db = env
    assert db.completed_accounts() == []
    assert db.is_account_complete("alice") is False

    db.add_post("P1", "alice", "Bild", "2026-07-01T10:00:00")
    db.mark_downloaded("P1", "alice")
    db.mark_account_complete("alice")

    assert db.is_account_complete("alice") is True
    done = db.completed_accounts()
    assert [d["username"] for d in done] == ["alice"]
    assert done[0]["files"] == 1        # Zahl der geladenen Beiträge
    assert done[0]["at"]                # Zeitstempel gesetzt


def test_complete_can_be_reset(env):
    client, db = env
    db.mark_account_complete("bob")
    assert db.is_account_complete("bob") is True
    r = client.post("/api/accounts-complete/reset", json={"username": "bob"})
    assert r.status_code == 200
    assert db.is_account_complete("bob") is False


def test_accounts_complete_endpoint(env):
    client, db = env
    db.mark_account_complete("carol")
    data = client.get("/api/accounts-complete").get_json()
    assert data["count"] == 1
    assert data["accounts"][0]["username"] == "carol"


def test_download_all_complete_skips_done(env):
    client, db = env
    db.replace_social_list("following", [
        {"username": "a", "pic": ""}, {"username": "b", "pic": ""},
        {"username": "c", "pic": ""},
    ])
    db.mark_account_complete("a")          # eines schon erledigt
    data = client.post("/api/download-all-complete", json={}).get_json()
    assert data["ok"] is True
    assert data["skipped"] == 1            # „a" übersprungen
    assert data["total"] == 2              # nur b und c
    assert data["types"] == "complete"


def _wait_idle(client, timeout: float = 5.0) -> None:
    """Wartet, bis der Massen-Download-Thread fertig ist.

    Der Auftrag läuft im Hintergrund; ein zweiter Start würde sonst mit
    „läuft bereits" (409) abgelehnt.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get("/api/download-following/status").get_json()["running"]:
            return
        time.sleep(0.02)
    raise AssertionError("Massen-Download wurde nicht fertig.")


def test_new_types_are_accepted(env):
    client, _ = env
    for t in ("highlights", "complete"):
        _wait_idle(client)
        r = client.post("/api/download-following",
                        json={"usernames": ["x"], "count": 1, "types": t})
        assert r.status_code == 200, t
        assert r.get_json()["types"] == t
