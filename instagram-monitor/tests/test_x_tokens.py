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


# --- Die Sitzung muss den Neustart überleben -------------------------------


def _neuer_dienst(tmp_path: Path) -> XService:
    """Wie ein Neustart der App: alles frisch, nur die Platte bleibt."""
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "dl"))
    return XService(db, settings, tmp_path / "session")


def test_a_stored_session_survives_a_restart(tmp_path: Path):
    """Der Fehler: Nach jedem Start stand wieder „nicht angemeldet“ da."""
    erst = _neuer_dienst(tmp_path)
    erst.login_with_tokens(AUTH, CT0)
    erst.db.close()

    nach_neustart = _neuer_dienst(tmp_path)
    assert nach_neustart.has_manual_session() is True
    assert nach_neustart.status()["session_confirmed"] is True
    assert nach_neustart.status()["manual_session"] is True
    nach_neustart.db.close()


def test_without_tokens_a_restart_is_not_signed_in(tmp_path: Path):
    dienst = _neuer_dienst(tmp_path)
    assert dienst.status()["session_confirmed"] is False
    dienst.db.close()


# --- X tauscht ct0 aus; die Ablage muss nachziehen -------------------------


def _browser_cookies(auth: str, ct0: str) -> list[dict]:
    return [
        {"name": "auth_token", "value": auth, "domain": ".x.com"},
        {"name": "ct0", "value": ct0, "domain": ".x.com"},
        {"name": "guest_id", "value": "v1%3A123", "domain": ".x.com"},
    ]


def test_a_rotated_ct0_is_picked_up(service: XService):
    service.login_with_tokens(AUTH, CT0)
    neues_ct0 = "c" * 64
    service._refresh_manual_session(_browser_cookies(AUTH, neues_ct0))
    gespeichert = json.loads(
        (service.session_dir / "manual_session.json").read_text(encoding="utf-8")
    )
    assert gespeichert == {"auth_token": AUTH, "ct0": neues_ct0}


def test_incomplete_browser_cookies_keep_the_old_session(service: XService):
    """Lieber den alten Stand behalten als die Anmeldung wegwerfen."""
    service.login_with_tokens(AUTH, CT0)
    service._refresh_manual_session([{"name": "ct0", "value": "d" * 64}])
    gespeichert = json.loads(
        (service.session_dir / "manual_session.json").read_text(encoding="utf-8")
    )
    assert gespeichert == {"auth_token": AUTH, "ct0": CT0}


def test_without_a_manual_session_nothing_is_written(service: XService):
    service._refresh_manual_session(_browser_cookies(AUTH, CT0))
    assert not (service.session_dir / "manual_session.json").exists()
    assert service.has_manual_session() is False


# --- Startfehler sollen ihren Grund nennen, nicht verdecken ----------------


def test_a_missing_chromium_is_named_as_such():
    from app.x_service import describe_launch_failure

    text = describe_launch_failure(RuntimeError(
        "BrowserType.launch: Executable doesn't exist at C:\\...\\chrome.exe"
    ))
    assert "fehlt oder startet nicht" in text
    assert "playwright install chromium" in text


def test_a_blocked_binary_is_not_mistaken_for_a_missing_one():
    """Windows-Sperre (Smart App Control) hat eine eigene Erklärung."""
    from app.x_service import describe_launch_failure

    text = describe_launch_failure(RuntimeError(
        "Failed to launch: status 0xc0e90002"
    ))
    assert "blockiert" in text
    assert "Smart App Control" in text
    assert "playwright install" not in text


def test_an_unknown_reason_is_passed_through():
    """Früher verdeckte „Chromium fehlt“ jede Meldung mit dem Wort browser."""
    from app.x_service import describe_launch_failure

    text = describe_launch_failure(RuntimeError("browser process crashed: no space left"))
    assert "no space left" in text
    assert "fehlt" not in text


def test_the_original_message_is_always_included():
    from app.x_service import describe_launch_failure

    for roh in ("Executable doesn't exist", "status 0xc0e90002", "irgendwas anderes"):
        assert roh in describe_launch_failure(RuntimeError(roh))
