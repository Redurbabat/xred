"""Tests für die Suche-Knöpfe: „Folgen"-Umschalter (lokale Liste) + ⭐-Zustand.

Fasst kein Instagram/Netzwerk an: ``search_profiles`` wird durch einen
Fake ersetzt. Geprüft werden die neuen Endpunkte
``/api/following/add`` + ``/api/following/remove`` und die Anreicherung von
``/api/search`` um ``following``/``favorite``.
"""

from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.web import create_app


class _FakeDownloader:
    is_logged_in = True
    logged_in_user = "me"

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}

    def search_profiles(self, query, limit=12):
        # Feste Trefferliste – unabhängig von der Anfrage.
        return [
            {"username": "alice", "full_name": "Alice", "is_verified": False},
            {"username": "bob", "full_name": "Bob", "is_verified": True},
            {"username": "charlie", "full_name": "Charlie", "is_verified": False},
        ]


@pytest.fixture()
def client_and_db(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    app = create_app(db, settings, _FakeDownloader(), access_key=None)
    yield app.test_client(), db
    db.close()


def _by_name(results):
    return {r["username"]: r for r in results}


def test_search_marks_following_and_favorite(client_and_db):
    client, db = client_and_db
    db.replace_social_list("following", [{"username": "alice", "pic": ""}])
    db.set_favorite("bob", True)

    res = _by_name(client.get("/api/search?q=a").get_json()["results"])
    assert res["alice"]["following"] is True      # steht in der lokalen Liste
    assert res["bob"]["following"] is False
    assert res["bob"]["favorite"] is True         # angepinnt
    assert res["alice"]["favorite"] is False


def test_following_add_and_remove(client_and_db):
    client, db = client_and_db
    # charlie ist noch nicht in der Liste.
    assert client.get("/api/search?q=c").get_json()["results"]
    r = client.post("/api/following/add", json={"username": "charlie",
                                                "full_name": "Charlie"})
    assert r.status_code == 200
    assert "charlie" in [a["username"] for a in db.get_social_list("following")]
    # Jetzt meldet die Suche following=True.
    res = _by_name(client.get("/api/search?q=c").get_json()["results"])
    assert res["charlie"]["following"] is True

    # Entfernen wirkt sofort.
    r = client.post("/api/following/remove", json={"username": "charlie"})
    assert r.status_code == 200
    assert "charlie" not in [a["username"] for a in db.get_social_list("following")]
    res = _by_name(client.get("/api/search?q=c").get_json()["results"])
    assert res["charlie"]["following"] is False


def test_following_add_rejects_empty_username(client_and_db):
    client, _ = client_and_db
    assert client.post("/api/following/add", json={"username": ""}).status_code == 400
