"""Verständliche Meldungen statt Technik-Kauderwelsch.

Instagram antwortet bei abgelaufener Anmeldung oder Drosselung mit einer
HTML-Seite statt JSON. Instaloader meldet dann „JSON Query to graphql/query:
Expecting value: line 1 column 1 (char 0)" – für Nutzer unverständlich.
Geprüft wird die Übersetzung in Klartext (kein Netz, kein Instagram).
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
    yield downloader
    db.close()


BAD_JSON = Exception(
    "JSON Query to graphql/query: Expecting value: line 1 column 1 (char 0)"
)


def test_bad_json_is_explained_in_plain_language(dl):
    hint = dl._explain_bad_response(BAD_JSON)
    assert hint is not None
    assert "abgewiesen" in hint
    assert "anmelden" in hint.lower()          # sagt, was zu tun ist
    assert "Expecting value" not in hint       # keine Technik-Meldung


def test_rate_limit_is_explained(dl):
    hint = dl._explain_bad_response(
        Exception("429 Please wait a few minutes before you try again.")
    )
    assert hint is not None and "warten" in hint.lower()


def test_other_errors_are_not_swallowed(dl):
    # Unbekannte Fehler bleiben unverändert (None = keine Sonderbehandlung).
    assert dl._explain_bad_response(Exception("Timeout beim Verbinden")) is None


def test_fetch_error_uses_plain_text(dl):
    err = dl._fetch_error(BAD_JSON, "Abo-Liste")
    assert "Expecting value" not in str(err)
    assert "abgewiesen" in str(err)
