"""Unit tests for the X integration. No test opens a browser or contacts X."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.x_service import (
    XLoginError,
    XService,
    XUnavailableError,
    XValidationError,
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
    validate_password,
    validate_result_limit,
    validate_sort,
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
    assert "python -m playwright install chromium" in message


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
    service._ensure_browser = lambda: None  # type: ignore[method-assign]
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
    monkeypatch.setattr("app.x_service.shutil.which", lambda name: None)
    state = service.status()
    assert state["available"] is True
    assert state["ffmpeg"] is False
    assert "ffmpeg" not in state["warning"].lower()
    db.close()
