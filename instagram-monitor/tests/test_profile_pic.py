"""Profilbild-Download: ``<konto>/Profilbild.jpg`` (einmalig, ohne Extra-Abruf).

Kein Netz: ``_download_file`` wird ersetzt; die CDN-URL kommt aus der beim
Beitrags-Abruf gemerkten Merkliste (``_pic_urls``).
"""

from pathlib import Path

import pytest

from app.database import Database
from app.downloader import InstagramDownloader
from app.settings import Settings


@pytest.fixture()
def dl(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    downloader = InstagramDownloader(settings, session_dir=tmp_path / "s")
    yield downloader, settings
    db.close()


def test_downloads_pic_from_remembered_url(dl, monkeypatch):
    downloader, settings = dl
    downloader._pic_urls["alice"] = "http://cdn/alice.jpg"
    calls = []
    monkeypatch.setattr(
        downloader, "_download_file",
        lambda url, dest: (calls.append(url), dest.write_bytes(b"pic")),
    )
    assert downloader.download_profile_pic("alice") is True
    assert calls == ["http://cdn/alice.jpg"]          # kein Profil-Abruf nötig
    assert (settings.download_dir / "alice" / "Profilbild.jpg").read_bytes() == b"pic"


def test_existing_pic_is_not_redownloaded(dl, monkeypatch):
    downloader, settings = dl
    downloader._pic_urls["bob"] = "http://cdn/bob.jpg"
    dest = settings.download_dir / "bob" / "Profilbild.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"alt")
    calls = []
    monkeypatch.setattr(downloader, "_download_file",
                        lambda url, d: calls.append(url))
    assert downloader.download_profile_pic("bob") is False
    assert calls == []                                 # nichts erneut geladen
    assert dest.read_bytes() == b"alt"


def test_without_known_url_and_offline_returns_false(dl, monkeypatch):
    downloader, _ = dl
    # Kein Eintrag in der Merkliste und der Profil-Abruf schlägt fehl
    # (Offline/Test) → sauberes False statt Absturz.
    monkeypatch.setattr(
        downloader, "_get_profile",
        lambda u: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert downloader.download_profile_pic("carol") is False
