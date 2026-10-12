"""Tests für die X-Anmeldung per Sitzungs-Token (auth_token + ct0).

Gegenüber dem Passwort hat sie drei Vorteile: Das Passwort bleibt aussen
vor, X sieht keine neue Anmeldung, und Downloads laufen ohne Browser –
der letzte Punkt zählt besonders auf einem Rechner ohne Bildschirm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.x_service import XService, XValidationError, validate_x_token

AUTH = "a" * 40
CT0 = "b" * 64


@pytest.fixture()
def service(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    yield XService(db, settings, tmp_path / "session")
    db.close()


# --- Prüfung der Eingabe ---------------------------------------------------


def test_tokens_are_normalized():
    assert validate_x_token("  " + AUTH.upper() + "  ", "auth_token") == AUTH
    assert validate_x_token(f'"{CT0}"', "ct0") == CT0


@pytest.mark.parametrize(
    "bad",
    ["", "   ", None, "nicht-hex", "zzzz", "abc", "a" * 500,
     "aaaa\taaaa", "aaaa\naaaa", "aaaa aaaa", "aaaa;DOMAIN=boese.example"],
)
def test_junk_is_rejected(bad):
    with pytest.raises(XValidationError):
        validate_x_token(bad, "auth_token")


def test_the_error_names_the_field_but_never_the_value():
    with pytest.raises(XValidationError) as excinfo:
        validate_x_token("geheim-aber-ungueltig", "ct0")
    text = str(excinfo.value)
    assert "ct0" in text
    assert "geheim" not in text


# --- Speichern und Verwenden ----------------------------------------------


def test_a_session_is_stored_and_reported(service: XService):
    assert service.status()["manual_session"] is False
    result = service.login_with_tokens(AUTH, CT0)
    assert result["ok"] is True
    assert service.has_manual_session() is True
    assert service.status()["manual_session"] is True
    assert service.status()["session_confirmed"] is True


def test_the_stored_file_holds_only_the_two_cookies(service: XService):
    service.login_with_tokens(AUTH, CT0)
    gespeichert = json.loads(
        (service.session_dir / "manual_session.json").read_text(encoding="utf-8")
    )
    assert gespeichert == {"auth_token": AUTH, "ct0": CT0}


def test_the_cookies_are_built_for_x_only(service: XService):
    service.login_with_tokens(AUTH, CT0)
    cookies = service._manual_cookies()
    assert {c["name"] for c in cookies} == {"auth_token", "ct0"}
    assert all(c["domain"] == ".x.com" and c["secure"] for c in cookies)
    # auth_token darf für Skripte unsichtbar bleiben, ct0 braucht X im Browser.
    assert next(c for c in cookies if c["name"] == "auth_token")["httpOnly"] is True


def test_downloads_work_without_an_open_browser(service: XService):
    """Der eigentliche Gewinn: kein Browserfenster für den Download."""
    assert service._export_cookie_file() is None  # ohne Token: nichts

    service.login_with_tokens(AUTH, CT0)
    pfad = service._export_cookie_file()
    assert pfad is not None and pfad.exists()
    inhalt = pfad.read_text(encoding="utf-8")
    assert inhalt.startswith("# Netscape HTTP Cookie File")
    assert AUTH in inhalt and CT0 in inhalt
    assert all(zeile.count("\t") == 6 for zeile in inhalt.splitlines()[1:] if zeile)


def test_forgetting_removes_the_session_and_the_working_copy(service: XService):
    service.login_with_tokens(AUTH, CT0)
    service._export_cookie_file()
    arbeitskopie = service.session_dir / ".yt-dlp-cookies.txt"
    assert arbeitskopie.exists()

    assert service.forget_tokens() is True
    assert service.has_manual_session() is False
    assert not arbeitskopie.exists()
    assert service.forget_tokens() is False  # zweites Mal: nichts mehr da


def test_a_damaged_file_is_simply_ignored(service: XService):
    service.session_dir.mkdir(parents=True, exist_ok=True)
    (service.session_dir / "manual_session.json").write_text("{kaputt", encoding="utf-8")
    assert service.has_manual_session() is False
    assert service._export_cookie_file() is None


def test_a_tampered_file_cannot_inject_extra_cookie_lines(service: XService):
    """Auch von Hand verfälschte Werte dürfen die Datei nicht aufbrechen."""
    service.session_dir.mkdir(parents=True, exist_ok=True)
    (service.session_dir / "manual_session.json").write_text(
        json.dumps({"auth_token": "aaaa\t.boese.example\tTRUE", "ct0": CT0}),
        encoding="utf-8",
    )
    assert service.has_manual_session() is False


def test_the_chromium_hint_points_at_the_running_interpreter():
    """Die alte Meldung riet zu „python -m playwright install“ – das traf

    unter Windows das System-Python statt der .venv der App, und der
    Fehler blieb nach dem Befolgen bestehen.
    """
    import sys

    from app.x_service import chromium_install_hint

    hinweis = chromium_install_hint()
    assert sys.executable in hinweis
    assert "playwright install chromium" in hinweis
    assert "setup-x-browser" in hinweis
