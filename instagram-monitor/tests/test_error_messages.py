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


# --- X-Downloadfehler verständlich machen ---------------------------------


def test_terminal_colour_codes_never_reach_the_interface():
    """yt-dlp färbt seine Meldungen ein; in der Oberfläche stand dann

    „[0;31mERROR:[0m“ mitten im Text.
    """
    from app.x_service import public_error

    roh = "\x1b[0;31mERROR:\x1b[0m [twitter] 123: Something went wrong"
    sauber = public_error(RuntimeError(roh))
    assert "\x1b" not in sauber
    assert "[0;31m" not in sauber
    assert "Something went wrong" in sauber


def test_no_video_found_names_all_three_likely_causes():
    from app.x_service import XService

    text = XService._map_download_error(
        RuntimeError("ERROR: [twitter] 2109614135096848599: No video could be found in this tweet")
    )
    assert "KONTOEINSTELLUNG" in text   # der haeufigste Grund bei sensiblen Inhalten
    assert "sensible Inhalte" in text   # genau der Schalter bei X
    assert "abgelaufen" in text         # zweiter moeglicher Grund
    assert "Fotos" in text              # dritter: reiner Bildbeitrag
    assert "No video could be found" not in text


def test_an_expired_session_is_named_as_such():
    from app.x_service import XService

    for roh, erwartet in [
        ("ERROR: 401 Unauthorized", "abgelaufen"),
        ("ERROR: This tweet does not exist", "gibt es nicht mehr"),
        ("ERROR: rate limit exceeded", "gebremst"),
    ]:
        assert erwartet in XService._map_download_error(RuntimeError(roh))


def test_an_unknown_error_is_still_passed_through():
    from app.x_service import XService

    text = XService._map_download_error(RuntimeError("Irgendein neuer Fehler von X"))
    assert "Irgendein neuer Fehler von X" in text
