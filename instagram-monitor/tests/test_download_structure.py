"""Tests für die typgetrennte Ablage: <konto>/Fotos und <konto>/Videos.

Fasst KEIN Instagram und kein Netzwerk an – ``media_urls`` und der
eigentliche Datei-Download werden ersetzt; geprüft wird nur die
Einsortier-Logik (welche Datei landet in welchem Unterordner).
"""

from pathlib import Path

import pytest

from app.database import Database
from app.downloader import InstagramDownloader, PostInfo, _media_subdir
from app.settings import Settings


@pytest.fixture()
def dl(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    downloader = InstagramDownloader(settings, session_dir=tmp_path / "sessions")
    yield downloader, settings
    db.close()


def test_media_subdir_classification():
    for name in ("a.jpg", "a.jpeg", "a.png", "a.webp", "a_3.JPG"):
        assert _media_subdir(name) == "Fotos"
    for name in ("a.mp4", "a.MOV", "a.m4v", "a_2.mp4"):
        assert _media_subdir(name) == "Videos"


def test_album_is_split_by_type(dl, monkeypatch):
    downloader, settings = dl
    root = settings.download_dir
    # Gemischtes Album: ein Bild + ein Video → getrennte Ordner.
    monkeypatch.setattr(downloader, "media_urls", lambda pi: [
        ("http://cdn/x.jpg", "SC_1.jpg"),
        ("http://cdn/x.mp4", "SC_2.mp4"),
    ])
    monkeypatch.setattr(downloader, "_download_file",
                        lambda url, dest: dest.write_bytes(b"data"))

    post = PostInfo(shortcode="SC", username="elif.erbeyy",
                    post_type="Album", posted_at="", url="")
    assert downloader.download_post(post) is True
    assert (root / "elif.erbeyy" / "Fotos" / "SC_1.jpg").is_file()
    assert (root / "elif.erbeyy" / "Videos" / "SC_2.mp4").is_file()


def test_existing_files_are_not_redownloaded(dl, monkeypatch):
    downloader, settings = dl
    root = settings.download_dir
    monkeypatch.setattr(downloader, "media_urls",
                        lambda pi: [("http://cdn/x.jpg", "SC.jpg")])
    calls = []
    monkeypatch.setattr(downloader, "_download_file",
                        lambda url, dest: (calls.append(dest),
                                           dest.write_bytes(b"new")))
    target = root / "u" / "Fotos" / "SC.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")

    post = PostInfo(shortcode="SC", username="u", post_type="Bild",
                    posted_at="", url="")
    assert downloader.download_post(post) is True
    assert calls == []                       # nichts erneut geladen
    assert target.read_bytes() == b"old"     # Datei unverändert


def test_story_goes_to_story_folder(dl, monkeypatch):
    downloader, settings = dl
    root = settings.download_dir

    class FakeStoryItem:
        is_video = True
        video_url = "http://cdn/s.mp4"
        url = "http://cdn/s.jpg"

    monkeypatch.setattr(downloader, "_download_file",
                        lambda url, dest: dest.write_bytes(b"v"))
    post = PostInfo(shortcode="123", username="u", post_type="Story · Video",
                    posted_at="", url="", raw=FakeStoryItem())
    assert downloader.download_story_item(post) is True
    # Stories landen im eigenen Ordner „Story", NICHT in Videos/Fotos.
    assert (root / "u" / "Story" / "123.mp4").is_file()
    assert not (root / "u" / "Videos" / "123.mp4").exists()


def test_highlight_goes_to_highlights_folder(dl, monkeypatch):
    downloader, settings = dl
    root = settings.download_dir

    class FakeHighlightItem:
        is_video = False
        url = "http://cdn/h.jpg"

    monkeypatch.setattr(downloader, "_download_file",
                        lambda url, dest: dest.write_bytes(b"p"))
    # list_story_items setzt bei Highlights post_type = "Highlight: <Titel>".
    post = PostInfo(shortcode="H1", username="u", post_type="Highlight: Sommer",
                    posted_at="", url="", raw=FakeHighlightItem())
    assert downloader.download_story_item(post) is True
    assert (root / "u" / "Highlights" / "H1.jpg").is_file()
    assert not (root / "u" / "Fotos" / "H1.jpg").exists()
