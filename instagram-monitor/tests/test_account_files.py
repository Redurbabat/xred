"""Zähler je Typ für die Profil-Anzeige (/api/account-files/<username>).

Zählt die tatsächlich auf der Platte liegenden Dateien getrennt nach
Fotos/Videos/Story/Highlights. Kein Netz/Instagram.
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


@pytest.fixture()
def client(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    dl_dir = tmp_path / "dl"
    settings = Settings(db, default_download_dir=str(dl_dir))
    app = create_app(db, settings, _Dl(), access_key=None)
    yield app.test_client(), dl_dir
    db.close()


def _touch(p: Path, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_counts_per_type(client):
    c, dl_dir = client
    root = dl_dir / "u"
    _touch(root / "Fotos" / "A.jpg")
    _touch(root / "Fotos" / "B_1.jpg")
    _touch(root / "Videos" / "C.mp4", b"xxxx")
    _touch(root / "Story" / "D.jpg")
    _touch(root / "Highlights" / "E.mp4")
    _touch(root / "Highlights" / "F.jpg")

    data = c.get("/api/account-files/u").get_json()
    assert data["counts"] == {"Fotos": 2, "Videos": 1, "Story": 1, "Highlights": 2}
    assert data["total"] == 6
    assert data["bytes"] == 5 * 1 + 4          # fünf 1-Byte + ein 4-Byte


def test_empty_account(client):
    c, _ = client
    data = c.get("/api/account-files/niemand").get_json()
    assert data["total"] == 0
    assert data["counts"]["Fotos"] == 0
