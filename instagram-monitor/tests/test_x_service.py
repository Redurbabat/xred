"""Unit tests for the X integration. No test opens a browser or contacts X."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.x_service import (
    XLoginError,
    XService,
    XServiceError,
    XUnavailableError,
    XValidationError,
    _Command,
    build_search_url,
    extract_post_id,
    is_browser_closed_error,
    normalize_login_identifier,
    normalize_optional_username,
    normalize_post_url,
    normalize_search_query,
    public_error,
    redact_secret,
    safe_media_filename,
    safe_path_component,
    safe_username_component,
    source_label,
    validate_password,
    validate_result_limit,
    validate_sort,
    validate_source,
    write_no_password_manager_prefs,
)


def _wait(job, timeout: float = 3.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = job.snapshot()
        if not state["running"]:
            return state
        time.sleep(0.01)
    raise AssertionError("Hintergrundjob wurde nicht rechtzeitig fertig")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" #fussball ", "#fussball"),
        ("  eine   kurze   Phrase ", "eine kurze Phrase"),
        ("musik", "musik"),
    ],
)
def test_normalize_search_query(raw, expected):
    assert normalize_search_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "#", "#zwei wörter", "x" * 121, None])
def test_invalid_search_query_is_rejected(raw):
    with pytest.raises(XValidationError):
        normalize_search_query(raw)


def test_result_limit_and_sort_validation():
    assert validate_result_limit("50") == 50
    assert validate_sort("top") == "top"
    for value in (0, 51, "viel", None):
        with pytest.raises(XValidationError):
            validate_result_limit(value)
    with pytest.raises(XValidationError):
        validate_sort("popular")


def test_search_url_contains_video_filter_and_latest_mode():
    url = build_search_url("#fussball", "latest", True)
    assert url.startswith("https://x.com/search?")
    assert "filter%3Avideos" in url
    assert "-filter%3Anativeretweets" in url
    assert url.endswith("&f=live")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://twitter.com/name/status/1234567890?s=20",
            "https://x.com/name/status/1234567890",
        ),
        (
            "https://www.x.com/User_1/status/9876543210/video/1",
            "https://x.com/User_1/status/9876543210",
        ),
    ],
)
def test_x_url_normalization_and_post_id(raw, expected):
    assert normalize_post_url(raw) == expected
    assert extract_post_id(raw) == expected.rsplit("/", 1)[-1]


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/name/status/1234567890",
        "https://example.com/name/status/1234567890",
        "https://x.com/name",
        "https://x.com/name/status/not-a-number",
        "https://x.com.evil.example/name/status/1234567890",
        "https://user:pass@x.com/name/status/1234567890",
    ],
)
def test_foreign_domains_and_invalid_status_links_are_rejected(url):
    with pytest.raises(XValidationError):
        normalize_post_url(url)


def test_safe_folder_and_filename_components():
    assert safe_path_component("Fußball 2026") == "Fußball-2026"
    assert ".." not in safe_path_component("../../privat")
    assert "/" not in safe_path_component("a/b")
    assert safe_media_filename("1234567890", "user/name", 2) == "1234567890_user_name_2.mp4"
    with pytest.raises(XValidationError):
        safe_media_filename("../1", "name", 1)


def test_x_database_migration_is_idempotent_and_downloaded_posts_persist(tmp_path: Path):
    db_path = tmp_path / "monitor.db"
    item = {
        "post_id": "1234567890",
        "username": "alice",
        "author_name": "Alice",
        "post_url": "https://x.com/alice/status/1234567890",
        "search_query": "#test",
        "caption": "Video",
        "preview_url": "",
        "published_at": "2026-08-11T10:00:00Z",
    }
    db = Database(db_path)
    db.upsert_x_post(item)
    db.mark_x_post_downloaded(item["post_id"], str(tmp_path / "video.mp4"))
    db.close()

    reopened = Database(db_path)
    assert reopened.is_x_post_known(item["post_id"])
    assert reopened.is_x_post_downloaded(item["post_id"])
    reopened.upsert_x_post({**item, "caption": "Aktualisiert"})
    row = reopened.get_x_post(item["post_id"])
    assert row["caption"] == "Aktualisiert"
    assert row["downloaded"] == 1
    reopened.close()


def test_search_job_deduplicates_post_ids(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))

    def search(**_kwargs):
        base = {
            "post_id": "1234567890",
            "username": "alice",
            "author_name": "Alice",
            "search_query": "#test",
            "caption": "Video",
            "preview_url": "",
            "published_at": "",
        }
        return [
            {**base, "post_url": "https://x.com/alice/status/1234567890"},
            {**base, "post_url": "https://twitter.com/alice/status/1234567890?s=1"},
        ]

    service = XService(db, settings, tmp_path / "session", browser_search=search)
    assert service.start_search("#test", 10, "latest")
    state = _wait(service.search_job)
    assert state["found"] == 1
    assert len(state["items"]) == 1
    assert db.is_x_post_known("1234567890")
    db.close()


def test_search_job_can_be_cancelled(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))

    def search(*, cancel_event, **_kwargs):
        cancel_event.wait(1)
        return []

    service = XService(db, settings, tmp_path / "session", browser_search=search)
    assert service.start_search("test", 5)
    assert service.cancel_search()
    state = _wait(service.search_job)
    assert state["cancelled"] is True
    assert state["finished"] is True
    db.close()


class _FakeYdl:
    def __init__(self, options, fail_id: str):
        self.options = options
        self.fail_id = fail_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=True):
        if self.fail_id in url:
            raise RuntimeError("simulierter Beitragsfehler")
        output = self.options["outtmpl"].replace("%(autonumber)02d", "01").replace("%(ext)s", "mp4")
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return {"id": url.rsplit("/", 1)[-1]}


def test_download_failures_are_isolated_and_existing_posts_skip(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    bad_id, good_id = "1111111111", "2222222222"
    for post_id in (bad_id, good_id):
        db.upsert_x_post(
            {
                "post_id": post_id,
                "username": "alice",
                "author_name": "Alice",
                "post_url": f"https://x.com/alice/status/{post_id}",
                "search_query": "#test",
                "caption": "",
                "preview_url": "",
                "published_at": "",
            }
        )
    service = XService(
        db,
        settings,
        tmp_path / "session",
        ytdlp_factory=lambda options: _FakeYdl(options, bad_id),
    )
    service._export_cookie_file = lambda: None
    assert service.start_download([bad_id, good_id])
    state = _wait(service.download_job)
    assert state["failed"] == 1
    assert state["downloaded"] == 1
    assert not db.is_x_post_downloaded(bad_id)
    assert db.is_x_post_downloaded(good_id)

    assert service.start_download([good_id])
    repeated = _wait(service.download_job)
    assert repeated["skipped"] == 1
    db.close()


def test_missing_chromium_has_actionable_message():
    message = XService._map_browser_error(RuntimeError("Executable doesn't exist"))
    # Der Pfad des laufenden Interpreters, nicht das blosse "python": Unter
    # Windows traf das sonst das System-Python statt der .venv der App.
    assert sys.executable in message
    assert "-m playwright install chromium" in message
    assert "setup-x-browser" in message


def test_secrets_are_removed_from_public_errors():
    message = public_error("Cookie: abc123; token=supersecret sessionid=qwerty")
    assert "abc123" not in message
    assert "supersecret" not in message
    assert "qwerty" not in message
    assert "<redacted>" in message


# ----------------------------------------------------------------------
# Direkte X-Anmeldung (ohne echten Browser, ohne echte Zugangsdaten)
# ----------------------------------------------------------------------

TEST_PASSWORD = "Nur-Ein-Testwert-4711"


class _FakeTimeout(Exception):
    """Steht für playwright.TimeoutError, ohne Playwright zu importieren."""


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_FakeLocator":
        return self

    def _kind(self) -> str:
        if "password" in self.selector:
            return "password"
        if "body" in self.selector:
            return "body"
        if "button" in self.selector or 'role="button"' in self.selector:
            return "button"
        return "text"

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        if self._kind() not in self.page.visible:
            raise _FakeTimeout(f"Timeout waiting for {self.selector}")

    def fill(self, value: str) -> None:
        self.page.filled.setdefault(self._kind(), []).append(value)

    def click(self, timeout: float = 0) -> None:
        self.page.submit()

    def press(self, key: str) -> None:
        self.page.submit()

    def inner_text(self, timeout: float = 0) -> str:
        return self.page.text


class _FakePage:
    """Bildet die X-Anmeldemaske als kleine Zustandsmaschine nach."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.step = 0
        self.url = "about:blank"
        self.text = ""
        self.visible = {"text", "body"}
        self.filled: dict[str, list[str]] = {}
        self.goto_calls: list[dict] = []
        self.brought_to_front = False
        self._apply()

    def _apply(self) -> None:
        state = self.steps[min(self.step, len(self.steps) - 1)]
        self.visible = set(state.get("visible", ("body",)))
        self.text = state.get("text", "")
        self.url = state.get("url", self.url)

    def submit(self) -> None:
        if self.step < len(self.steps) - 1:
            self.step += 1
        self._apply()

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until})
        self.url = url
        self._apply()  # X kann sofort weiterleiten (z. B. wenn schon angemeldet)

    def bring_to_front(self) -> None:
        self.brought_to_front = True

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


def _login_service(tmp_path: Path, page: _FakePage) -> XService:
    """XService, dessen Browserauftrag direkt auf die Fake-Seite geht."""
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session")

    def call(name, payload=None, timeout=30):
        assert name == "login"
        data = payload or {}
        try:
            return service._login_on_page(page, settle_seconds=0, **data)
        finally:
            data.clear()

    service._browser_call = call  # type: ignore[method-assign]
    return service


def _signed_in_page() -> _FakePage:
    return _FakePage(
        [
            {"visible": ("text", "button", "body"), "url": "https://x.com/i/flow/login"},
            {"visible": ("password", "button", "body"), "url": "https://x.com/i/flow/login"},
            {"visible": ("body",), "url": "https://x.com/home"},
        ]
    )


def test_direct_login_fills_the_visible_form_and_confirms_the_session(tmp_path: Path):
    page = _signed_in_page()
    service = _login_service(tmp_path, page)
    result = service.login(" name@example.com ", TEST_PASSWORD)
    assert result["state"] == "signed_in"
    assert result["session_confirmed"] is True
    assert page.filled["text"] == ["name@example.com"]
    assert page.filled["password"] == [TEST_PASSWORD]
    assert page.brought_to_front is True
    service.db.close()


def test_login_navigation_uses_commit(tmp_path: Path):
    page = _signed_in_page()
    service = _login_service(tmp_path, page)
    service.login("name@example.com", TEST_PASSWORD)
    assert page.goto_calls[0]["url"] == "https://x.com/i/flow/login"
    assert page.goto_calls[0]["wait_until"] == "commit"
    service.db.close()


def test_login_asks_for_optional_username_only_when_x_does(tmp_path: Path):
    steps = [
        {"visible": ("text", "button", "body"), "url": "https://x.com/i/flow/login"},
        {
            "visible": ("text", "button", "body"),
            "url": "https://x.com/i/flow/login",
            "text": "Ungewöhnliche Aktivität – gib deinen Benutzernamen ein",
        },
        {"visible": ("password", "button", "body"), "url": "https://x.com/i/flow/login"},
        {"visible": ("body",), "url": "https://x.com/home"},
    ]
    with_hint = _login_service(tmp_path / "a", _FakePage(steps))
    page = _FakePage(steps)
    with_hint._browser_call = lambda name, payload=None, timeout=30: (  # type: ignore[method-assign]
        with_hint._login_on_page(page, settle_seconds=0, **(payload or {}))
    )
    assert with_hint.login("name@example.com", TEST_PASSWORD, "@meinname")["state"] == "signed_in"
    assert page.filled["text"] == ["name@example.com", "meinname"]
    with_hint.db.close()

    # Ohne @Namen bleibt die Abfrage bewusst im sichtbaren Fenster stehen.
    without = _login_service(tmp_path / "b", _FakePage(steps))
    assert without.login("name@example.com", TEST_PASSWORD)["state"] == "verification"
    without.db.close()


def test_login_accepts_an_already_signed_in_session(tmp_path: Path):
    # X leitet ein angemeldetes Profil sofort weiter – dann fehlt die Maske.
    page = _FakePage([{"visible": ("body",), "url": "https://x.com/home"}])
    service = _login_service(tmp_path, page)
    result = service.login("name@example.com", TEST_PASSWORD)
    assert result["state"] == "signed_in"
    assert page.filled == {}  # kein Passwort eingetippt, es war nicht nötig
    service.db.close()


def test_login_leaves_two_factor_and_captcha_in_the_browser(tmp_path: Path):
    page = _FakePage(
        [
            {"visible": ("text", "button", "body"), "url": "https://x.com/i/flow/login"},
            {"visible": ("password", "button", "body"), "url": "https://x.com/i/flow/login"},
            {
                "visible": ("body",),
                "url": "https://x.com/i/flow/login",
                "text": "Gib den Bestätigungscode aus deiner App ein",
            },
        ]
    )
    service = _login_service(tmp_path, page)
    result = service.login("name@example.com", TEST_PASSWORD)
    assert result["state"] == "verification"
    assert result["session_confirmed"] is False
    assert "2FA" in result["message"] or "Bestätigung" in result["message"]
    service.db.close()


def test_account_lock_page_is_not_reported_as_a_finished_login(tmp_path: Path):
    # Bei eingeschränkten Konten zeigt X /account/access – die Zugangsdaten
    # sind zwar akzeptiert, die Anmeldung aber noch nicht fertig. Das darf
    # nicht als „Sitzung bereit“ durchgehen.
    page = _FakePage(
        [
            {"visible": ("text", "button", "body"), "url": "https://x.com/i/flow/login"},
            {"visible": ("password", "button", "body"), "url": "https://x.com/i/flow/login"},
            {
                "visible": ("body",),
                "url": "https://x.com/account/access",
                "text": "Bestätige deine Identität, um fortzufahren.",
            },
        ]
    )
    service = _login_service(tmp_path, page)
    result = service.login("name@example.com", TEST_PASSWORD)
    assert result["state"] == "verification"
    assert result["session_confirmed"] is False
    assert service._already_signed_in(page) is False
    service.db.close()


def test_wrong_credentials_produce_an_error_without_the_password(tmp_path: Path):
    page = _FakePage(
        [
            {"visible": ("text", "button", "body"), "url": "https://x.com/i/flow/login"},
            {"visible": ("password", "button", "body"), "url": "https://x.com/i/flow/login"},
            {
                "visible": ("body",),
                "url": "https://x.com/i/flow/login",
                "text": "Das eingegebene Passwort war falsch.",
            },
        ]
    )
    service = _login_service(tmp_path, page)
    with pytest.raises(XLoginError) as excinfo:
        service.login("name@example.com", TEST_PASSWORD)
    assert TEST_PASSWORD not in str(excinfo.value)
    service.db.close()


def test_login_never_logs_the_password(tmp_path: Path, caplog):
    page = _signed_in_page()
    service = _login_service(tmp_path, page)
    with caplog.at_level(0):
        service.login("name@example.com", TEST_PASSWORD)
    assert TEST_PASSWORD not in caplog.text
    service.db.close()


def test_login_errors_never_carry_the_password(tmp_path: Path):
    page = _FakePage([{"visible": ("body",), "url": "https://x.com/i/flow/login"}])
    service = _login_service(tmp_path, page)
    with pytest.raises(XLoginError) as excinfo:
        service.login("name@example.com", TEST_PASSWORD)
    assert TEST_PASSWORD not in str(excinfo.value)

    mapped = XService._map_login_error(RuntimeError(f"fill failed: {TEST_PASSWORD}"), TEST_PASSWORD)
    assert TEST_PASSWORD not in mapped
    assert "<redacted>" in mapped
    service.db.close()


def test_unavailable_browser_is_not_reported_as_a_credential_error(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session")

    def unavailable(*_args, **_kwargs):
        raise XUnavailableError("Playwright-Chromium fehlt oder startet nicht.")

    service._browser_call = unavailable  # type: ignore[method-assign]
    with pytest.raises(XUnavailableError) as excinfo:
        service.login("name@example.com", TEST_PASSWORD)
    assert not isinstance(excinfo.value, XLoginError)
    assert TEST_PASSWORD not in str(excinfo.value)
    db.close()


def test_browser_call_clears_credentials_from_the_command(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session")
    # _ensure_browser liefert die Auftragsschlange der aktuellen Sitzung.
    service._ensure_browser = lambda: service._commands  # type: ignore[method-assign]
    seen: dict[str, object] = {}

    def worker() -> None:
        command = service._commands.get()
        seen["payload"] = command.payload
        seen["password"] = command.payload.get("password")
        command.result = {"state": "signed_in"}
        command.done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    service._browser_call("login", {"password": TEST_PASSWORD}, timeout=3)
    thread.join(3)
    assert seen["password"] == TEST_PASSWORD
    assert seen["payload"] == {}
    db.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" @Name ", "Name"), ("name@example.com", "name@example.com"), ("Nutzer_1", "Nutzer_1")],
)
def test_login_identifier_normalization(raw, expected):
    assert normalize_login_identifier(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "@", None, 42, "a" * 255, "mit\nZeilenumbruch"])
def test_invalid_login_identifier_is_rejected(raw):
    with pytest.raises(XValidationError):
        normalize_login_identifier(raw)


def test_optional_username_is_normalized_or_empty():
    assert normalize_optional_username("@Name_1") == "Name_1"
    assert normalize_optional_username("  ") == ""
    assert normalize_optional_username(None) == ""
    for bad in ("zu langer name 123456", "@name!", "a" * 16):
        with pytest.raises(XValidationError):
            normalize_optional_username(bad)


def test_password_validation_never_echoes_the_value():
    assert validate_password(TEST_PASSWORD) == TEST_PASSWORD
    for bad in ("", None, 5, "x" * 257, "Geheim\tMitTab"):
        with pytest.raises(XValidationError) as excinfo:
            validate_password(bad)
        if isinstance(bad, str) and bad:
            assert bad not in str(excinfo.value)


def test_redact_secret_removes_the_password_from_any_text():
    assert redact_secret(f"Fehler bei {TEST_PASSWORD} hier", TEST_PASSWORD) == (
        "Fehler bei <redacted> hier"
    )
    assert redact_secret("unverändert", "") == "unverändert"


def test_password_manager_is_disabled_in_the_browser_profile(tmp_path: Path):
    profile = tmp_path / "x_session"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(
        '{"profile": {"exit_type": "Normal"}}', encoding="utf-8"
    )
    path = write_no_password_manager_prefs(profile)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["credentials_enable_service"] is False
    assert data["profile"]["password_manager_enabled"] is False
    assert data["profile"]["password_manager_leak_detection"] is False
    # Bestehende Profileinstellungen bleiben erhalten.
    assert data["profile"]["exit_type"] == "Normal"


def test_closed_browser_is_recognized_and_explained():
    assert is_browser_closed_error("Target page, context or browser has been closed")
    assert is_browser_closed_error(RuntimeError("Browser has been closed"))
    assert not is_browser_closed_error("Timeout 30000ms exceeded")
    message = XService._map_login_error(RuntimeError("Target page has been closed"))
    assert "X-Browser öffnen" in message


@pytest.mark.parametrize(
    "message",
    [
        "net::ERR_CONNECTION_CLOSED at https://x.com/search",
        "Connection closed while reading from the driver",
    ],
)
def test_a_network_error_is_not_mistaken_for_a_closed_window(message):
    # Früher enthielt _CLOSED_MARKERS ein nacktes „closed“. Damit galt jeder
    # Verbindungsabbruch als zugeklicktes Fenster und riss die komplette
    # Chromium-Sitzung ab.
    assert not is_browser_closed_error(message)


def test_closed_window_message_does_not_ask_for_a_playwright_install():
    message = XService._map_browser_error(
        RuntimeError("Target page, context or browser has been closed")
    )
    assert "install" not in message.lower()
    assert "geschlossen" in message
    # Ein wirklich fehlendes Chromium meldet weiterhin den Installationsweg.
    missing = XService._map_browser_error(RuntimeError("Executable doesn't exist"))
    assert "-m playwright install chromium" in missing


def test_closed_window_stops_the_browser_loop(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session")
    service._browser_open = True
    service._session_confirmed = True
    service._on_browser_closed()
    assert service._browser_open is False
    assert service._session_confirmed is False
    assert service._commands.get_nowait() is None
    assert service.status()["browser_open"] is False
    db.close()


def test_missing_ffmpeg_is_only_an_optional_warning(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session")
    # Weder im PATH noch als mitgeliefertes Paket vorhanden.
    monkeypatch.setattr("app.x_service.ffmpeg_path", lambda: "")
    state = service.status()
    assert state["available"] is True
    assert state["ffmpeg"] is False
    assert "ffmpeg" not in state["warning"].lower()
    db.close()


# ----------------------------------------------------------------------
# Lebenszyklus der Browsersitzung. _serve_commands ist bewusst von
# sync_playwright getrennt, damit die Schleife ohne echten Browser
# prüfbar ist – vorher war sie eingemauert und komplett ungetestet.
# ----------------------------------------------------------------------


class _FakeContext:
    def __init__(self, pages: list) -> None:
        self.pages = pages
        self.closed = False
        self.cookie_calls = 0

    def is_closed(self) -> bool:
        return self.closed

    def cookies(self, _urls):
        self.cookie_calls += 1
        return []


class _TickPage:
    """Seite, deren Ereignis-Ping nach n Aufrufen einen Fehler wirft.

    ``closes`` bildet den echten Ablauf nach: Wenn Playwright das geschlossene
    Fenster meldet, ist auch der Kontext zu.
    """

    def __init__(self, fail_after: int = 0, error: str = "", closes=None) -> None:
        self.fail_after = fail_after
        self.error = error
        self.closes = closes
        self.ticks = 0

    def wait_for_timeout(self, _ms: float) -> None:
        self.ticks += 1
        if self.error and self.ticks >= self.fail_after:
            if self.closes is not None:
                self.closes.closed = True
            raise RuntimeError(self.error)


def _loop_service(tmp_path: Path) -> XService:
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    return XService(db, settings, tmp_path / "session")


@pytest.fixture()
def fast_tick(monkeypatch):
    """Takt der Browserschleife verkürzen – Tests warten sonst sekundenlang."""
    monkeypatch.setattr("app.x_service._IDLE_TICK_SECONDS", 0.01)


def test_serve_commands_notices_a_closed_window_while_idle(tmp_path: Path, fast_tick):
    # Der eigentliche Fund: Im Leerlauf blockierte die Schleife in
    # queue.get() und konnte das Schließen deshalb nie bemerken.
    service = _loop_service(tmp_path)
    context = _FakeContext([object()])
    page = _TickPage(
        fail_after=2,
        error="Target page, context or browser has been closed",
        closes=context,
    )
    commands: queue.Queue = queue.Queue()
    service._serve_commands(context, page, commands)   # muss von allein enden
    assert page.ticks == 2          # erster Tick still, zweiter meldet zu
    assert context.closed is True
    service.db.close()


def test_a_network_error_does_not_end_the_session(tmp_path: Path, fast_tick):
    # net::ERR_CONNECTION_CLOSED darf die Sitzung nicht abräumen.
    service = _loop_service(tmp_path)
    page = _TickPage(fail_after=1, error="net::ERR_CONNECTION_CLOSED")
    context = _FakeContext([object()])
    commands: queue.Queue = queue.Queue()

    def stop() -> None:
        while page.ticks < 3:
            time.sleep(0.005)
        commands.put(None)

    threading.Thread(target=stop, daemon=True).start()
    service._serve_commands(context, page, commands)
    assert page.ticks >= 3          # trotz Fehler weitergetickt
    assert context.closed is False
    service.db.close()


def test_a_closed_tab_is_replaced_without_ending_the_session(tmp_path: Path, fast_tick):
    service = _loop_service(tmp_path)
    dead = _TickPage(fail_after=1, error="Target page has been closed")
    fresh = _TickPage()
    context = _FakeContext([fresh])
    commands: queue.Queue = queue.Queue()

    def stop() -> None:
        while fresh.ticks < 1:
            time.sleep(0.005)
        commands.put(None)

    threading.Thread(target=stop, daemon=True).start()
    service._serve_commands(context, dead, commands)
    assert fresh.ticks >= 1          # auf der Ersatzseite weitergemacht
    service.db.close()


def test_an_abandoned_command_is_never_executed(tmp_path: Path):
    # Nach einer Zeitüberschreitung darf ein Auftrag nicht später doch noch
    # loslaufen – sonst navigierte ein verirrtes open_login das Fenster
    # mitten in eine laufende Suche.
    service = _loop_service(tmp_path)
    page = _TickPage()
    context = _FakeContext([page])
    commands: queue.Queue = queue.Queue()
    command = _Command(name="cookies")
    command.abandon()
    commands.put(command)
    commands.put(None)
    service._serve_commands(context, page, commands)
    assert context.cookie_calls == 0
    assert command.done.is_set() is False
    service.db.close()


def test_a_live_command_still_runs(tmp_path: Path):
    service = _loop_service(tmp_path)
    page = _TickPage()
    context = _FakeContext([page])
    commands: queue.Queue = queue.Queue()
    command = _Command(name="cookies")
    commands.put(command)
    commands.put(None)
    service._serve_commands(context, page, commands)
    assert context.cookie_calls == 1
    assert command.done.is_set() is True
    service.db.close()


def test_a_failed_thread_start_leaves_no_broken_state(tmp_path: Path, monkeypatch):
    service = _loop_service(tmp_path)

    def boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    with pytest.raises(XUnavailableError):
        service._ensure_browser()
    # Kein hängender Zustand: Der nächste Versuch darf nicht 45 s warten.
    assert service._browser_thread is None
    assert service._browser_ready.is_set()
    service.db.close()


def test_ensure_browser_starts_a_new_session_after_the_window_was_closed(tmp_path: Path):
    # Nach dem Schließen lebt der alte Thread noch kurz. Wer nur is_alive()
    # prüft, gibt die tote Sitzung zurück und der nächste Klick scheitert.
    service = _loop_service(tmp_path)
    started: list[str] = []

    def fake_loop(commands):
        # Bildet die echte Schleife nach: Sie endet, sobald die Sitzung tot
        # ist – deshalb läuft das join() in _ensure_browser nicht ins Leere.
        started.append("los")
        service._browser_open = True
        service._browser_ready.set()
        while service._browser_open:
            try:
                if commands.get(timeout=0.01) is None:
                    break
            except queue.Empty:
                continue

    service._browser_loop = fake_loop  # type: ignore[method-assign]
    first = service._ensure_browser()
    assert started == ["los"]
    service._browser_open = False          # Nutzer klickt das Fenster zu
    second = service._ensure_browser()
    assert started == ["los", "los"]
    assert second is not first             # frische Schlange je Sitzung
    service.db.close()


class _SpyLock:
    """Merkt sich, ob beim Freigeben der Job-Lock noch gehalten wurde."""

    def __init__(self, inner: threading.Lock, job) -> None:
        self._inner = inner
        self._job = job
        self.held_during_release: bool | None = None

    def acquire(self, blocking: bool = True) -> bool:
        return self._inner.acquire(blocking)

    def release(self) -> None:
        self.held_during_release = self._job._lock.locked()
        self._inner.release()


def test_the_operation_lock_is_free_as_soon_as_the_job_reports_finished(tmp_path: Path):
    # Die Oberfläche stoppt ihr Polling, sobald running=false gemeldet wird,
    # und darf dann sofort neu starten. Läge die Sperre da noch, antwortete der
    # Server auf den eigenen Klick mit 409.
    #
    # Die Zeitlücke ist zu kurz, um sie durch Pollen zuverlässig zu treffen –
    # geprüft wird deshalb die Zusicherung selbst: Freigabe und Veröffentlichung
    # des Endzustands passieren unter EINEM gehaltenen Job-Lock. Wer also
    # running=false sehen kann, findet die Sperre bereits frei.
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session", browser_search=lambda **_k: [])
    spy = _SpyLock(service._operation_lock, service.search_job)
    service._operation_lock = spy  # type: ignore[assignment]

    assert service.start_search("#test", 5)
    _wait(service.search_job)
    assert spy.held_during_release is True

    # Und die Sperre bleibt danach benutzbar – kein Leck.
    for runde in range(20):
        assert service.start_search("#test", 5), f"Runde {runde} bekam die Sperre nicht"
        _wait(service.search_job)
    db.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://pbs.twimg.com/media/abc.jpg", "https://pbs.twimg.com/media/abc.jpg"),
        ("https://video.twimg.com/tweet_video/x.mp4", "https://video.twimg.com/tweet_video/x.mp4"),
        # Fremde Hosts und http bleiben draußen.
        ("https://boese.example/x.jpg", ""),
        ("http://pbs.twimg.com/x.jpg", ""),
        # Ein Anführungszeichen hinter dem geprüften Host könnte im Frontend
        # aus dem src-Attribut ausbrechen – zweite Schicht neben esc().
        ('https://pbs.twimg.com/x" onerror="alert(1)', ""),
        ("https://pbs.twimg.com/x'>", ""),
        ("https://pbs.twimg.com/x\nfoo", ""),
    ],
)
def test_preview_urls_cannot_break_out_of_an_attribute(raw, expected):
    from app.x_service import _safe_preview_url

    assert _safe_preview_url(raw) == expected


def test_one_broken_search_card_does_not_lose_the_others(tmp_path: Path):
    # Vorher warf eine einzige Karte ohne brauchbaren Link die komplette
    # Suche weg – der Nutzer sah null Treffer statt der übrigen Videos.
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    base = {
        "username": "alice",
        "author_name": "Alice",
        "search_query": "#test",
        "caption": "Video",
        "preview_url": "",
        "published_at": "",
    }

    def search(**_kwargs):
        return [
            {**base, "post_url": "https://x.com/alice/status/1111111111"},
            {**base, "post_url": None},                              # fehlt
            {**base, "post_url": "https://boese.example/a/status/2"},  # fremd
            {**base},                                                 # gar keiner
            {**base, "post_url": "https://x.com/alice/status/2222222222"},
        ]

    service = XService(db, settings, tmp_path / "session", browser_search=search)
    assert service.start_search("#test", 10)
    state = _wait(service.search_job)
    assert state["error"] == ""
    assert state["found"] == 2
    assert {item["post_id"] for item in state["items"]} == {"1111111111", "2222222222"}
    db.close()


def test_a_data_problem_is_not_reported_as_a_browser_error():
    # XValidationError erbt jetzt auch von XServiceError – deshalb Klartext
    # statt „X-Browserfehler: …“.
    message = XService._map_browser_error(XValidationError("Der Link ist ungültig."))
    assert message == "Der Link ist ungültig."
    assert "Browserfehler" not in message


@pytest.mark.parametrize(
    ("url", "signed_in"),
    [
        ("https://x.com/home", True),
        ("https://x.com/i/bookmarks", True),
        ("https://x.com/", False),           # ausgeloggte Startseite
        ("https://x.com", False),
        ("chrome-error://chromewebdata/", False),
        ("about:blank", False),
        ("https://boese.example/home", False),
        ("http://x.com/home", False),
        ("https://x.com/i/flow/login", False),
        ("https://x.com/account/access", False),
    ],
)
def test_only_a_real_x_page_counts_as_a_confirmed_session(tmp_path: Path, url, signed_in):
    service = _loop_service(tmp_path)

    class _Page:
        pass

    page = _Page()
    page.url = url
    assert service._already_signed_in(page) is signed_in
    assert service._session_confirmed is signed_in
    service.db.close()


def test_a_failed_cookie_export_is_visible_in_the_job_state(tmp_path: Path):
    service = _loop_service(tmp_path)
    service._browser_open = False
    assert service._export_cookie_file() is None
    message = service.download_job.snapshot()["message"]
    assert "ohne Anmeldung" in message
    service.db.close()


# --- Eigene Listen: Lesezeichen und Likes ("Verlauf" in der X-App) ---------


class _ListLocator:
    """Minimaler Locator für die Listenseiten (Lesezeichen/Likes)."""

    def __init__(self, page: "_ListPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_ListLocator":
        return self

    def count(self) -> int:
        return 1 if self.selector in self.page.elements else 0

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        if self.selector not in self.page.elements:
            raise _FakeTimeout(f"Timeout waiting for {self.selector}")

    def get_attribute(self, name: str) -> str:
        return self.page.elements.get(self.selector, {}).get(name, "")

    def inner_text(self, timeout: float = 0) -> str:
        return self.page.elements.get(self.selector, {}).get("text", "")


class _ListPage:
    def __init__(self, elements: dict, url: str = "https://x.com/home") -> None:
        self.elements = elements
        self.url = url
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> None:
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str) -> _ListLocator:
        return _ListLocator(self, selector)

    def evaluate(self, script: str) -> None:
        return None


def _list_service(tmp_path: Path) -> XService:
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    return XService(db, settings, tmp_path / "session")


def test_source_validation_accepts_only_the_three_known_lists():
    assert validate_source(None) == "search"
    assert validate_source("BOOKMARKS") == "bookmarks"
    assert validate_source(" likes ") == "likes"
    assert source_label("likes") == "Likes"
    assert source_label("bookmarks") == "Lesezeichen"
    assert source_label("search") == ""
    assert validate_source("") == "search"  # leeres Feld = ganz normale Suche
    for bad in ("feed", "../etc", 7):
        with pytest.raises(XValidationError):
            validate_source(bad)


def test_bookmarks_use_the_fixed_address(tmp_path: Path):
    service = _list_service(tmp_path)
    page = _ListPage({})
    assert service._collection_url(page, "bookmarks") == "https://x.com/i/bookmarks"
    assert page.goto_calls == []  # Lesezeichen brauchen keinen Umweg
    service.db.close()


def test_likes_address_is_built_from_the_own_username(tmp_path: Path):
    service = _list_service(tmp_path)
    page = _ListPage({'a[data-testid="AppTabBar_Profile_Link"]': {"href": "/meinname"}})
    assert service._collection_url(page, "likes") == "https://x.com/meinname/likes"
    service.db.close()


def test_likes_fall_back_to_the_account_switcher(tmp_path: Path):
    service = _list_service(tmp_path)
    page = _ListPage(
        {'[data-testid="SideNav_AccountSwitcher_Button"]': {"text": "Mein Name\n@zweitname"}}
    )
    assert service._collection_url(page, "likes") == "https://x.com/zweitname/likes"
    service.db.close()


def test_likes_without_a_readable_username_explain_themselves(tmp_path: Path):
    service = _list_service(tmp_path)
    with pytest.raises(XServiceError) as excinfo:
        service._collection_url(_ListPage({}), "likes")
    assert "Benutzername" in str(excinfo.value)
    service.db.close()


def test_an_empty_bookmark_list_is_no_error_but_an_empty_result(tmp_path: Path):
    service = _list_service(tmp_path)
    page = _ListPage({}, url="https://x.com/i/bookmarks")
    results = service._search_visible_page(
        page, "Lesezeichen", 10, "latest", False, threading.Event(), "bookmarks"
    )
    assert results == []
    service.db.close()


def test_an_empty_search_page_stays_an_error(tmp_path: Path):
    service = _list_service(tmp_path)
    page = _ListPage({}, url="https://x.com/search?q=test")
    with pytest.raises(XServiceError):
        service._search_visible_page(
            page, "test", 10, "latest", False, threading.Event(), "search"
        )
    service.db.close()


def test_bookmark_job_needs_no_query_and_labels_its_own_folder(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    seen: dict = {}

    def search(**kwargs):
        seen.update(kwargs)
        return [
            {
                "post_id": "1234567890",
                "username": "alice",
                "author_name": "Alice",
                "post_url": "https://x.com/alice/status/1234567890",
                "search_query": kwargs["query"],
                "caption": "Video",
                "preview_url": "",
                "published_at": "",
            }
        ]

    service = XService(db, settings, tmp_path / "session", browser_search=search)
    assert service.start_search("", 10, source="bookmarks")
    state = _wait(service.search_job)
    assert seen["source"] == "bookmarks"
    assert seen["query"] == "Lesezeichen"
    assert state["found"] == 1
    # Zielordner: <Download-Ordner>/X/Lesezeichen/Videos
    assert safe_path_component(state["items"][0]["search_query"]) == "Lesezeichen"
    db.close()


def test_likes_job_rejects_nothing_for_a_missing_query(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session", browser_search=lambda **_k: [])
    assert service.start_search(None, 5, source="likes")
    assert _wait(service.search_job)["finished"] is True
    db.close()


def test_the_search_still_insists_on_a_query(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session", browser_search=lambda **_k: [])
    with pytest.raises(XValidationError):
        service.start_search("", 5, source="search")
    db.close()


class _NoWait:
    """cancel_event-Ersatz, der Wartezeiten im Test überspringt."""

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float = 0) -> bool:
        return False


class _ArticleWithoutVideo:
    def locator(self, selector: str):
        return _ListLocator(_ListPage({}), selector)

    def inner_text(self, timeout: float = 0) -> str:
        return "Nur Text"


class _Articles:
    def __init__(self) -> None:
        self.article = _ArticleWithoutVideo()

    @property
    def first(self):
        return self

    def wait_for(self, state: str = "visible", timeout: float = 0) -> None:
        return None

    def count(self) -> int:
        return 1

    def nth(self, index: int):
        return self.article


class _ScrollPage:
    """Liste ohne ein einziges Video, die sich N-mal weiterschieben lässt."""

    def __init__(self, scrollable_rounds: int) -> None:
        self.url = "https://x.com/i/bookmarks"
        self.y = 0.0
        self.scrolls = 0
        self.scrollable_rounds = scrollable_rounds

    def goto(self, url: str, wait_until: str = "load", timeout: float = 0) -> None:
        self.url = url

    def locator(self, selector: str):
        if selector == "article":
            return _Articles()
        return _ListLocator(_ListPage({}), selector)

    def evaluate(self, script: str):
        if "scrollY" in script:
            return self.y
        self.scrolls += 1
        if self.scrolls <= self.scrollable_rounds:
            self.y += 700
        return None


def test_a_list_without_videos_keeps_scrolling_until_the_page_stands_still(tmp_path: Path):
    """Beiträge ohne Video dürfen die Liste nicht vorzeitig beenden."""
    service = _list_service(tmp_path)
    page = _ScrollPage(scrollable_rounds=8)
    results = service._search_visible_page(
        page, "Lesezeichen", 10, "latest", False, _NoWait(), "bookmarks"
    )
    assert results == []
    # Weitergescrollt, obwohl kein einziger Treffer dazukam …
    assert page.scrolls > 3
    # … aber am Seitenende beendet und nicht bis ans Rundenlimit gelaufen.
    assert page.scrolls < 20
    service.db.close()


# --- Ablage je Urheber ------------------------------------------------------


def _x_service_mit_beitrag(tmp_path: Path, post_id: str, *, query: str = "#test",
                           author: str = "alice"):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    db.upsert_x_post(
        {
            "post_id": post_id,
            "username": author,
            "author_name": author.title(),
            "post_url": f"https://x.com/{author}/status/{post_id}",
            "search_query": query,
            "caption": "",
            "preview_url": "",
            "published_at": "",
        }
    )
    service = XService(
        db, settings, tmp_path / "session",
        ytdlp_factory=lambda options: _FakeYdl(options, "nie"),
    )
    service._export_cookie_file = lambda: None
    return service, db, settings


def test_videos_land_in_a_folder_per_author(tmp_path: Path):
    post_id = "3333333333"
    service, db, settings = _x_service_mit_beitrag(tmp_path, post_id)
    assert service.start_download([post_id])
    assert _wait(service.download_job)["downloaded"] == 1

    ziel = settings.download_dir / "X" / "test" / "alice" / "Videos"
    dateien = list(ziel.glob(f"{post_id}_*"))
    assert len(dateien) == 1, sorted(p.name for p in ziel.glob("*"))
    # Der Suchbegriff bleibt als Ebene darüber erhalten.
    assert not (settings.download_dir / "X" / "test" / "Videos").exists()
    db.close()


def test_the_likes_list_also_separates_by_author(tmp_path: Path):
    post_id = "4444444444"
    service, db, settings = _x_service_mit_beitrag(
        tmp_path, post_id, query="Likes", author="bob"
    )
    assert service.start_download([post_id])
    assert _wait(service.download_job)["downloaded"] == 1
    assert list((settings.download_dir / "X" / "Likes" / "bob" / "Videos").glob("*.mp4"))
    db.close()


def test_already_downloaded_files_in_the_old_flat_folder_are_found(tmp_path: Path):
    """Altbestand darf nicht ein zweites Mal geladen werden."""
    post_id = "5555555555"
    service, db, settings = _x_service_mit_beitrag(tmp_path, post_id)
    alt = settings.download_dir / "X" / "test" / "Videos"
    alt.mkdir(parents=True)
    (alt / f"{post_id}_alice_1.mp4").write_bytes(b"alt")

    assert service.start_download([post_id])
    state = _wait(service.download_job)
    assert state["skipped"] == 1
    assert state["downloaded"] == 0
    # Die Datei bleibt, wo sie liegt – nichts wird verschoben oder doppelt.
    neu = settings.download_dir / "X" / "test" / "alice" / "Videos"
    assert list(alt.glob("*.mp4"))
    assert not list(neu.glob("*.mp4"))
    db.close()


def test_a_strange_author_name_cannot_escape_the_folder(tmp_path: Path):
    assert safe_username_component("../../etc") == "______etc"
    assert safe_username_component("") == "unknown"
    assert safe_username_component(None) == "unknown"
    assert safe_username_component("Normaler_Name9") == "Normaler_Name9"


# --- Jedes Video erreichbar machen -----------------------------------------


class _Markierung:
    """Eine Beitragskarte, die nur bestimmte Elemente enthält."""

    def __init__(self, vorhanden: set[str]) -> None:
        self.vorhanden = vorhanden

    def locator(self, selector: str):
        treffer = 1 if selector in self.vorhanden else 0
        return type("L", (), {"count": lambda _self: treffer})()


def test_a_card_without_a_player_still_counts_as_video(tmp_path: Path):
    """Bei sensiblen Inhalten zeigt X nur eine Warnkachel – ohne <video>.

    Genau solche Beiträge fehlten vorher komplett in Suche und
    Lesezeichen, ohne dass irgendwo ein Hinweis erschien.
    """
    dienst = _list_service(tmp_path)
    for marker in XService._VIDEO_MARKERS:
        assert dienst._looks_like_video(_Markierung({marker})) is True, marker
    assert dienst._looks_like_video(_Markierung(set())) is False
    assert dienst._looks_like_video(_Markierung({"img"})) is False
    dienst.db.close()


def test_a_pasted_link_can_be_downloaded(tmp_path: Path):
    """Vorher ging nur, was vorher in einem Suchergebnis stand."""
    dienst = _list_service(tmp_path)
    post_id = dienst.remember_link("https://x.com/alice/status/1234567890")
    assert post_id == "1234567890"
    zeile = dienst.db.get_x_post(post_id)
    assert zeile is not None
    assert zeile["username"] == "alice"
    assert zeile["search_query"] == "Links"
    # Jetzt kennt ihn auch die normale Download-Prüfung.
    assert dienst.resolve_post_id(post_url="https://x.com/alice/status/1234567890") == post_id
    dienst.db.close()


def test_a_known_post_keeps_its_original_source(tmp_path: Path):
    dienst = _list_service(tmp_path)
    dienst.db.upsert_x_post({
        "post_id": "2222222222", "username": "bob", "author_name": "Bob",
        "post_url": "https://x.com/bob/status/2222222222",
        "search_query": "Lesezeichen", "caption": "", "preview_url": "",
        "published_at": "",
    })
    dienst.remember_link("https://x.com/bob/status/2222222222")
    assert dienst.db.get_x_post("2222222222")["search_query"] == "Lesezeichen"
    dienst.db.close()


@pytest.mark.parametrize("schlecht", [
    "https://example.com/a/status/1234567890",
    "http://x.com/a/status/1234567890",
    "https://x.com/alice",
    "kein-link",
    "",
])
def test_a_foreign_or_broken_link_is_refused(tmp_path: Path, schlecht):
    dienst = _list_service(tmp_path)
    with pytest.raises(XValidationError):
        dienst.remember_link(schlecht)
    dienst.db.close()


# --- Zitierter Beitrag: automatisch den richtigen Link nehmen --------------


class _ZitatYdl:
    """Liefert nur für EINE bestimmte Adresse ein Video, sonst Fehler."""

    aufrufe: list[str] = []

    def __init__(self, options, funktioniert: str) -> None:
        self.options = options
        self.funktioniert = funktioniert

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=True):
        _ZitatYdl.aufrufe.append(url)
        if url != self.funktioniert:
            raise RuntimeError("No video could be found in this tweet")
        pfad = Path(self.options["outtmpl"].replace("%(autonumber)02d", "01")
                    .replace("%(ext)s", "mp4"))
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_bytes(b"video")
        return {"id": url.rsplit("/", 1)[-1]}


def _zitat_dienst(tmp_path: Path, funktioniert: str, alt: str):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    db.upsert_x_post({
        "post_id": "7777777777", "username": "aussen", "author_name": "Aussen",
        "post_url": "https://x.com/aussen/status/7777777777",
        "alt_urls": [alt],
        "search_query": "#test", "caption": "", "preview_url": "", "published_at": "",
    })
    service = XService(
        db, settings, tmp_path / "session",
        ytdlp_factory=lambda options: _ZitatYdl(options, funktioniert),
    )
    service._export_cookie_file = lambda: None
    return service, db, settings


def test_a_quoted_post_is_tried_automatically(tmp_path: Path):
    """Der Nutzer soll den richtigen Link nicht selbst suchen müssen."""
    innen = "https://x.com/innen/status/8888888888"
    _ZitatYdl.aufrufe = []
    service, db, settings = _zitat_dienst(tmp_path, funktioniert=innen, alt=innen)

    assert service.start_download(["7777777777"])
    state = _wait(service.download_job)
    assert state["downloaded"] == 1, state
    # Erst der äußere Beitrag, dann der zitierte.
    assert _ZitatYdl.aufrufe == ["https://x.com/aussen/status/7777777777", innen]
    assert db.is_x_post_downloaded("7777777777")
    db.close()


def test_the_outer_post_is_still_tried_first(tmp_path: Path):
    aussen = "https://x.com/aussen/status/7777777777"
    _ZitatYdl.aufrufe = []
    service, db, _settings = _zitat_dienst(
        tmp_path, funktioniert=aussen, alt="https://x.com/innen/status/8888888888"
    )
    assert service.start_download(["7777777777"])
    assert _wait(service.download_job)["downloaded"] == 1
    assert _ZitatYdl.aufrufe == [aussen]  # kein überflüssiger zweiter Versuch
    db.close()


def test_if_no_link_works_the_reason_is_reported(tmp_path: Path):
    _ZitatYdl.aufrufe = []
    service, db, _settings = _zitat_dienst(
        tmp_path, funktioniert="https://x.com/niemand/status/9999999999",
        alt="https://x.com/innen/status/8888888888",
    )
    assert service.start_download(["7777777777"])
    state = _wait(service.download_job)
    assert state["failed"] == 1
    assert len(_ZitatYdl.aufrufe) == 2      # beide probiert
    assert "kein Video heraus" in state["items"][0]["error"]
    db.close()


def test_alt_urls_are_validated_before_use():
    """Ein manipulierter Eintrag darf keine fremde Adresse einschleusen."""
    assert XService._alt_urls({"alt_urls": "https://boese.example/a/status/1"}) == []
    assert XService._alt_urls({"alt_urls": ""}) == []
    assert XService._alt_urls({}) == []
    viele = " ".join(f"https://x.com/a/status/{n}" for n in range(1111111, 1111120))
    assert len(XService._alt_urls({"alt_urls": viele})) == 3


# --- Browser-Rückfall: holen, was die Schnittstelle verweigert -------------


@pytest.mark.parametrize("gut", [
    "https://video.twimg.com/ext_tw_video/123/pu/pl/abc.m3u8",
    "https://video.twimg.com/ext_tw_video/123/pu/vid/720x1280/abc.mp4",
    "https://video.twimg.com/amplify_video/1/vid/avc1/x.mp4?tag=14",
])
def test_real_x_media_addresses_are_accepted(gut):
    from app.x_service import is_media_url
    assert is_media_url(gut) is True


@pytest.mark.parametrize("schlecht", [
    "http://video.twimg.com/a.m3u8",              # kein HTTPS
    "https://boese.example/a.m3u8",               # fremder Host
    "https://video.twimg.com.boese.example/a.mp4",  # Host nur vorgetaeuscht
    "https://pbs.twimg.com/media/bild.jpg",       # kein Video
    "https://video.twimg.com/skript.js",          # falsche Endung
    "", None, 42,
])
def test_foreign_or_odd_addresses_are_refused(schlecht):
    from app.x_service import is_media_url
    assert is_media_url(schlecht) is False


def test_the_browser_fallback_only_passes_on_checked_addresses(tmp_path: Path):
    """Auch wenn der Browser Unsinn meldet, darf nichts davon durchrutschen."""
    dienst = _list_service(tmp_path)
    dienst._browser_call = lambda *a, **k: [
        "https://video.twimg.com/ext_tw_video/1/pl/gut.m3u8",
        "https://boese.example/schad.mp4",
        "nicht-mal-eine-adresse",
    ]
    assert dienst._media_urls_via_browser("https://x.com/a/status/1") == [
        "https://video.twimg.com/ext_tw_video/1/pl/gut.m3u8"
    ]
    dienst.db.close()


def test_a_broken_browser_does_not_break_the_download(tmp_path: Path):
    dienst = _list_service(tmp_path)

    def kaputt(*a, **k):
        raise RuntimeError("Fenster ist zu")

    dienst._browser_call = kaputt
    assert dienst._media_urls_via_browser("https://x.com/a/status/1") == []
    dienst.db.close()


class _NurBrowserYdl:
    """Scheitert an der Beitragsadresse, kann aber die Medienadresse laden."""

    aufrufe: list[str] = []

    def __init__(self, options) -> None:
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=True):
        _NurBrowserYdl.aufrufe.append(url)
        if "video.twimg.com" not in url:
            raise RuntimeError("No video could be found in this tweet")
        pfad = Path(self.options["outtmpl"].replace("%(autonumber)02d", "01")
                    .replace("%(ext)s", "mp4"))
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_bytes(b"video")
        return {"id": "1"}


def test_what_the_api_refuses_is_fetched_through_the_browser(tmp_path: Path):
    """Der eigentliche Zweck: sensible Beiträge doch noch bekommen."""
    _NurBrowserYdl.aufrufe = []
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    db.upsert_x_post({
        "post_id": "6666666666", "username": "alice", "author_name": "Alice",
        "post_url": "https://x.com/alice/status/6666666666",
        "search_query": "#test", "caption": "", "preview_url": "", "published_at": "",
    })
    service = XService(db, settings, tmp_path / "session",
                       ytdlp_factory=lambda options: _NurBrowserYdl(options))
    service._export_cookie_file = lambda: None
    service._media_urls_via_browser = lambda url: [
        "https://video.twimg.com/ext_tw_video/6/pl/beste.m3u8"
    ]

    assert service.start_download(["6666666666"])
    state = _wait(service.download_job)
    assert state["downloaded"] == 1, state
    # Erst der Beitrag selbst, dann die mitgelesene Medienadresse.
    assert _NurBrowserYdl.aufrufe == [
        "https://x.com/alice/status/6666666666",
        "https://video.twimg.com/ext_tw_video/6/pl/beste.m3u8",
    ]
    assert db.is_x_post_downloaded("6666666666")
    db.close()


def test_without_any_media_address_the_original_reason_is_reported(tmp_path: Path):
    _NurBrowserYdl.aufrufe = []
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    db.upsert_x_post({
        "post_id": "6666666667", "username": "alice", "author_name": "Alice",
        "post_url": "https://x.com/alice/status/6666666667",
        "search_query": "#test", "caption": "", "preview_url": "", "published_at": "",
    })
    service = XService(db, settings, tmp_path / "session",
                       ytdlp_factory=lambda options: _NurBrowserYdl(options))
    service._export_cookie_file = lambda: None
    service._media_urls_via_browser = lambda url: []

    assert service.start_download(["6666666667"])
    state = _wait(service.download_job)
    assert state["failed"] == 1
    assert "kein Video heraus" in state["items"][0]["error"]
    db.close()


class _MithoerSeite:
    """Seite, die beim Öffnen ein paar Antworten „sendet“."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.zuhoerer: list = []
        self.url = "about:blank"
        self.geoeffnet = ""

    def on(self, ereignis, rueckruf):
        assert ereignis == "response"
        self.zuhoerer.append(rueckruf)

    def remove_listener(self, ereignis, rueckruf):
        self.zuhoerer.remove(rueckruf)

    def goto(self, url, wait_until="load", timeout=0):
        self.geoeffnet = url
        for u in self.urls:                      # X laedt die Medien
            for z in list(self.zuhoerer):
                z(type("A", (), {"url": u})())

    def locator(self, selector):
        return type("L", (), {
            "first": property(lambda s: s),
            "count": lambda s: 0,
            "click": lambda s, timeout=0: None,
            "wait_for": lambda s, state="visible", timeout=0: None,
        })()

    def wait_for_timeout(self, ms):
        return None


def test_only_checked_addresses_are_collected(tmp_path: Path):
    dienst = _list_service(tmp_path)
    seite = _MithoerSeite([
        "https://pbs.twimg.com/media/bild.jpg",                       # kein Video
        "https://boese.example/schad.mp4",                            # fremd
        "https://video.twimg.com/ext_tw_video/1/vid/720x1280/a.mp4",  # gut
        "https://video.twimg.com/ext_tw_video/1/pl/a.m3u8",           # gut, bevorzugt
        "https://video.twimg.com/ext_tw_video/1/pl/a.m3u8",           # doppelt
    ])
    treffer = dienst._collect_media_urls(seite, "https://x.com/a/status/1", seconds=0)
    assert treffer == [
        "https://video.twimg.com/ext_tw_video/1/pl/a.m3u8",           # Liste zuerst
        "https://video.twimg.com/ext_tw_video/1/vid/720x1280/a.mp4",
    ]
    assert seite.geoeffnet == "https://x.com/a/status/1"
    assert seite.zuhoerer == []          # Mithören sauber beendet
    dienst.db.close()


def test_the_listener_is_removed_even_after_a_failure(tmp_path: Path):
    dienst = _list_service(tmp_path)
    seite = _MithoerSeite([])

    def kaputt(url, wait_until="load", timeout=0):
        raise RuntimeError("Seite nicht erreichbar")

    seite.goto = kaputt
    with pytest.raises(RuntimeError):
        dienst._collect_media_urls(seite, "https://x.com/a/status/1", seconds=0)
    assert seite.zuhoerer == []
    dienst.db.close()
