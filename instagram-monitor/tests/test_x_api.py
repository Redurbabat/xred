"""Flask API tests for X endpoints, using only mocked service operations."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.web import create_app
from app.x_service import XService


class _Downloader:
    is_logged_in = False
    logged_in_user = None

    def backoff_status(self):
        return {"active": False, "remaining_seconds": 0}


def _result():
    return {
        "post_id": "1234567890",
        "username": "alice",
        "author_name": "Alice",
        "post_url": "https://x.com/alice/status/1234567890",
        "search_query": "#test",
        "caption": "Testvideo",
        "preview_url": "",
        "published_at": "",
    }


@pytest.fixture()
def api_env(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(
        db,
        settings,
        tmp_path / "session",
        browser_search=lambda **_kwargs: [_result()],
    )
    app = create_app(db, settings, _Downloader(), x_service=service)
    yield app.test_client(), db, service
    db.close()


def test_x_status_and_search_success(api_env):
    client, _db, _service = api_env
    status = client.get("/api/x/status")
    assert status.status_code == 200
    assert status.get_json()["available"] is True

    response = client.post(
        "/api/x/search",
        json={"query": "#test", "limit": 5, "sort": "latest"},
    )
    assert response.status_code == 202
    end = time.monotonic() + 2
    state = {}
    while time.monotonic() < end:
        state = client.get("/api/x/search/status").get_json()
        if not state["running"]:
            break
        time.sleep(0.01)
    assert state["finished"] is True
    assert state["found"] == 1
    assert state["items"][0]["post_url"] == "https://x.com/alice/status/1234567890"


def test_bookmarks_and_likes_start_without_a_query(api_env):
    """Die eigenen Listen brauchen keinen Suchbegriff."""
    client, _db, service = api_env
    for source in ("bookmarks", "likes"):
        response = client.post("/api/x/search", json={"source": source, "limit": 5})
        assert response.status_code == 202, source
        end = time.monotonic() + 2
        while time.monotonic() < end and service.search_job.snapshot()["running"]:
            time.sleep(0.01)
        assert service.search_job.snapshot()["finished"] is True


def test_an_unknown_source_is_rejected(api_env):
    client, _db, _service = api_env
    response = client.post("/api/x/search", json={"source": "feed", "limit": 5})
    assert response.status_code == 400
    assert response.get_json()["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "", "limit": 5, "sort": "latest"},
        {"query": "test", "limit": 51, "sort": "latest"},
        {"query": "test", "limit": 5, "sort": "beliebt"},
    ],
)
def test_x_search_api_rejects_invalid_input(api_env, payload):
    client, _db, _service = api_env
    response = client.post("/api/x/search", json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_x_download_rejects_foreign_or_unknown_links(api_env):
    client, _db, _service = api_env
    foreign = client.post(
        "/api/x/download", json={"post_url": "https://example.com/a/status/1234567890"}
    )
    assert foreign.status_code == 400
    unknown = client.post(
        "/api/x/download", json={"post_url": "https://x.com/a/status/9999999999"}
    )
    assert unknown.status_code == 400


def test_android_unavailable_mode_is_clear(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    client = create_app(db, settings, _Downloader()).test_client()
    status = client.get("/api/x/status").get_json()
    assert status["available"] is False
    assert "Android-App" in status["warning"]
    start = client.post("/api/x/search", json={"query": "test"})
    assert start.status_code == 501
    db.close()


def test_access_code_guard_also_protects_x_endpoints(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    client = create_app(db, settings, _Downloader(), access_key="secret").test_client()
    denied = client.get("/api/x/status", environ_base={"REMOTE_ADDR": "192.168.1.9"})
    assert denied.status_code == 403
    allowed = client.get(
        "/api/x/status?key=secret", environ_base={"REMOTE_ADDR": "192.168.1.9"}
    )
    assert allowed.status_code in {302, 308}
    db.close()


# ----------------------------------------------------------------------
# Direkte X-Anmeldung: Der Passwort-Endpunkt gehört dem PC allein.
# ----------------------------------------------------------------------

TEST_PASSWORD = "Nur-Ein-Testwert-4711"
LAN = {"REMOTE_ADDR": "192.168.1.42"}


def _token(client) -> str:
    response = client.get("/api/x/csrf")
    assert response.status_code == 200
    return response.get_json()["token"]


def _login(client, *, token=None, environ=None, headers=None, payload=None):
    body = {"identifier": "name@example.com", "password": TEST_PASSWORD}
    if payload is not None:
        body = payload
    sent = {"X-CSRF-Token": token if token is not None else _token(client)}
    sent.update(headers or {})
    return client.post(
        "/api/x/login", json=body, headers=sent, environ_base=environ or {}
    )


def test_login_endpoint_signs_in_and_never_echoes_the_password(api_env, monkeypatch):
    client, _db, service = api_env
    seen = {}

    def fake_login(identifier, password, username=""):
        seen.update(identifier=identifier, password=password, username=username)
        return {
            "ok": True,
            "state": "signed_in",
            "browser_open": True,
            "session_confirmed": True,
            "message": "Bei X angemeldet.",
        }

    monkeypatch.setattr(service, "login", fake_login)
    response = _login(client)
    assert response.status_code == 200
    assert response.get_json()["state"] == "signed_in"
    assert TEST_PASSWORD not in response.get_data(as_text=True)
    assert seen["password"] == TEST_PASSWORD


def test_lan_device_gets_403_on_the_password_endpoint(api_env, monkeypatch):
    client, _db, service = api_env
    token = _token(client)

    def fail(*_args, **_kwargs):
        raise AssertionError("Der Dienst darf über das Netzwerk nie erreicht werden.")

    monkeypatch.setattr(service, "login", fail)
    response = _login(client, token=token, environ=LAN)
    assert response.status_code == 403
    assert response.get_json()["local_only"] is True


def test_lan_device_cannot_read_the_login_token(api_env):
    client, _db, _service = api_env
    assert client.get("/api/x/csrf", environ_base=LAN).status_code == 403


def test_password_endpoint_requires_the_token(api_env, monkeypatch):
    client, _db, service = api_env
    monkeypatch.setattr(
        service, "login", lambda *a, **k: pytest.fail("ohne Token nie aufrufen")
    )
    assert _login(client, token="").status_code == 403
    assert _login(client, token="falsches-token").status_code == 403
    # Kopfzeilen kommen als Latin-1 an: Ein Umlaut darf 403 ergeben – und
    # nicht compare_digest mit TypeError in einen HTTP 500 laufen lassen.
    assert _login(client, token="töken-mit-umlaut").status_code == 403


def test_password_endpoint_rejects_foreign_origin_and_host(api_env, monkeypatch):
    client, _db, service = api_env
    monkeypatch.setattr(
        service, "login", lambda *a, **k: pytest.fail("fremde Seite nie durchlassen")
    )
    foreign_origin = _login(client, headers={"Origin": "https://boese.example"})
    assert foreign_origin.status_code == 403
    # DNS-Rebinding: fremder Name, der auf 127.0.0.1 zeigt. Seit dem
    # globalen Herkunftsschutz (_origin_guard) wird das bereits vor der
    # Route abgewiesen – also ohne das routeneigene Feld „local_only“.
    rebinding = _login(client, headers={"Host": "angriff.example.com"})
    assert rebinding.status_code == 403
    assert rebinding.get_json()["error"]


def test_password_endpoint_rejects_html_form_posts(api_env, monkeypatch):
    client, _db, service = api_env
    monkeypatch.setattr(
        service, "login", lambda *a, **k: pytest.fail("Formular-POST nie durchlassen")
    )
    response = client.post(
        "/api/x/login",
        data={"identifier": "name@example.com", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": _token(client)},
    )
    assert response.status_code == 403


def test_login_endpoint_validates_input_before_touching_the_browser(api_env, monkeypatch):
    client, _db, service = api_env
    # Echte Prüfung von XService.login – nur der Browser bleibt tabu.
    monkeypatch.setattr(
        service,
        "_browser_call",
        lambda *a, **k: pytest.fail("ungültige Eingabe darf keinen Browser starten"),
    )
    for payload in (
        {"identifier": "", "password": TEST_PASSWORD},
        {"identifier": "name@example.com", "password": ""},
        {"identifier": "name@example.com", "password": TEST_PASSWORD, "username": "@ungültig!"},
    ):
        assert _login(client, payload=payload).status_code == 400


def test_login_failure_is_reported_without_the_password(api_env, monkeypatch, caplog):
    from app.x_service import XLoginError

    client, _db, service = api_env

    def refuse(*_args, **_kwargs):
        raise XLoginError("X hat die Zugangsdaten abgelehnt.")

    monkeypatch.setattr(service, "login", refuse)
    with caplog.at_level(0):
        response = _login(client)
    assert response.status_code == 401
    assert TEST_PASSWORD not in response.get_data(as_text=True)
    assert TEST_PASSWORD not in caplog.text


def test_missing_browser_is_reported_as_service_unavailable(api_env, monkeypatch):
    from app.x_service import XUnavailableError

    client, _db, service = api_env

    def missing(*_args, **_kwargs):
        raise XUnavailableError(
            "Playwright-Chromium fehlt oder startet nicht. Bitte einmalig "
            "`python -m playwright install chromium` ausführen."
        )

    monkeypatch.setattr(service, "login", missing)
    response = _login(client)
    assert response.status_code == 503
    assert "playwright install chromium" in response.get_json()["error"]
    assert TEST_PASSWORD not in response.get_data(as_text=True)


def test_status_marks_network_clients_as_non_local(api_env):
    client, _db, _service = api_env
    assert client.get("/api/x/status").get_json()["local"] is True
    local_flag = client.get(
        "/api/x/status", environ_base=LAN, headers={"X-Test": "1"}
    ).get_json()["local"]
    assert local_flag is False


def test_api_never_returns_cookie_or_token_values(api_env, monkeypatch):
    client, _db, service = api_env

    def fail():
        raise RuntimeError("Cookie: abc123 token=supersecret sessionid=qwerty")

    monkeypatch.setattr(service, "open_login", fail)
    response = client.post("/api/x/login/open")
    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "abc123" not in body
    assert "supersecret" not in body
    assert "qwerty" not in body


# ----------------------------------------------------------------------
# Herkunftsprüfung: Suche, Download und „Browser öffnen“ dürfen nicht von
# einer fremden Webseite auslösbar sein – müssen aber im Handy-Modus über
# das WLAN weiter funktionieren. Nur das Passwort ist PC-exklusiv.
# ----------------------------------------------------------------------

X_ACTION_ROUTES = [
    ("/api/x/login/open", None),
    ("/api/x/search", {"query": "#test", "limit": 5}),
    ("/api/x/search/cancel", None),
    ("/api/x/download", {"post_id": "1234567890"}),
    ("/api/x/download-selected", {"post_ids": []}),
    ("/api/x/download/cancel", None),
]


@pytest.mark.parametrize(("route", "body"), X_ACTION_ROUTES)
@pytest.mark.parametrize("origin", ["https://boese.example", "null"])
def test_x_actions_reject_a_foreign_origin(api_env, route, body, origin):
    client, _db, _service = api_env
    kwargs = {"headers": {"Origin": origin}}
    if body is not None:
        kwargs["json"] = body
    response = client.post(route, **kwargs)
    assert response.status_code == 403, route
    assert "fremden Seite" in response.get_json()["error"]


@pytest.mark.parametrize(("route", "body"), X_ACTION_ROUTES)
def test_x_actions_reject_a_cross_site_fetch_marker(api_env, route, body):
    client, _db, _service = api_env
    kwargs = {"headers": {"Sec-Fetch-Site": "cross-site"}}
    if body is not None:
        kwargs["json"] = body
    assert client.post(route, **kwargs).status_code == 403, route


def test_search_and_download_still_work_from_a_phone_in_the_lan(tmp_path: Path):
    """Der wichtigste Test des Umbaus: Der Handy-Modus bleibt heil.

    Eine reine localhost-Prüfung wäre hier bequem gewesen und hätte genau
    diesen Fall zerstört – vom Tablet aus soll gesucht und geladen werden
    können, nur das X-Passwort bleibt dem PC vorbehalten.
    """
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session", browser_search=lambda **_k: [])
    client = create_app(db, settings, _Downloader(), access_key="geheim", x_service=service).test_client()
    client.set_cookie("igmon_key", "geheim", domain="192.168.1.50")
    lan = {"REMOTE_ADDR": "192.168.1.42"}

    started = client.post(
        "http://192.168.1.50:8756/api/x/search",
        json={"query": "#test", "limit": 5},
        headers={"Origin": "http://192.168.1.50:8756", "Sec-Fetch-Site": "same-origin"},
        environ_base=lan,
    )
    assert started.status_code == 202

    # Dasselbe Gerät bekommt für das Passwort weiterhin 403.
    denied = client.post(
        "http://192.168.1.50:8756/api/x/login",
        json={"identifier": "name@example.com", "password": TEST_PASSWORD},
        headers={"Origin": "http://192.168.1.50:8756"},
        environ_base=lan,
    )
    assert denied.status_code == 403
    assert denied.get_json()["local_only"] is True
    db.close()


def test_a_foreign_page_cannot_use_a_valid_access_cookie(tmp_path: Path):
    db = Database(tmp_path / "d.db")
    settings = Settings(db, default_download_dir=str(tmp_path / "downloads"))
    service = XService(db, settings, tmp_path / "session", browser_search=lambda **_k: [])
    client = create_app(db, settings, _Downloader(), access_key="geheim", x_service=service).test_client()
    client.set_cookie("igmon_key", "geheim", domain="192.168.1.50")
    response = client.post(
        "http://192.168.1.50:8756/api/x/search",
        json={"query": "#test"},
        headers={"Origin": "https://boese.example"},
        environ_base={"REMOTE_ADDR": "192.168.1.42"},
    )
    assert response.status_code == 403
    assert "fremden Seite" in response.get_json()["error"]
    db.close()


@pytest.mark.parametrize(
    "route", ["/api/x/search", "/api/x/download", "/api/x/download-selected"]
)
def test_a_json_array_is_a_bad_request_not_a_server_error(api_env, route):
    # Vorher: data.get(...) auf einer Liste → AttributeError → HTTP 503 mit
    # internem Python-Text in der Oberfläche.
    client, _db, _service = api_env
    response = client.post(route, json=[1, 2])
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "object has no attribute" not in body


# ----------------------------------------------------------------------
# Anmeldung per Sitzungs-Token: genauso geschützt wie das Passwort.
# ----------------------------------------------------------------------

TEST_AUTH_TOKEN = "a" * 40
TEST_CT0 = "b" * 64


def _tokens(client, *, token=None, environ=None, headers=None, payload=None):
    body = {"auth_token": TEST_AUTH_TOKEN, "ct0": TEST_CT0}
    if payload is not None:
        body = payload
    sent = {"X-CSRF-Token": token if token is not None else _token(client)}
    sent.update(headers or {})
    return client.post(
        "/api/x/tokens", json=body, headers=sent, environ_base=environ or {}
    )


def test_tokens_are_accepted_from_the_pc_and_never_echoed(api_env):
    client, _db, service = api_env
    response = _tokens(client)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert TEST_AUTH_TOKEN not in body and TEST_CT0 not in body
    assert service.has_manual_session() is True
    assert client.get("/api/x/status").get_json()["manual_session"] is True


def test_a_network_device_cannot_hand_over_tokens(api_env):
    client, _db, service = api_env
    response = _tokens(client, token="egal", environ=LAN)
    assert response.status_code == 403
    assert service.has_manual_session() is False


def test_tokens_need_the_csrf_token(api_env):
    client, _db, service = api_env
    assert _tokens(client, token="").status_code == 403
    assert _tokens(client, token="falsch").status_code == 403
    assert service.has_manual_session() is False


def test_a_foreign_page_cannot_hand_over_tokens(api_env):
    client, _db, service = api_env
    assert _tokens(client, headers={"Origin": "https://boese.example"}).status_code == 403
    assert service.has_manual_session() is False


def test_junk_tokens_are_rejected_with_a_clear_message(api_env):
    client, _db, service = api_env
    response = _tokens(client, payload={"auth_token": "kein-hex", "ct0": TEST_CT0})
    assert response.status_code == 400
    assert "auth_token" in response.get_json()["error"]
    assert service.has_manual_session() is False


def test_the_stored_session_can_be_removed_again(api_env):
    client, _db, service = api_env
    assert _tokens(client).status_code == 200
    # Genau wie die Oberfläche: JSON-Kopfzeile, aber kein Inhalt nötig.
    removed = client.post(
        "/api/x/tokens/forget",
        headers={"X-CSRF-Token": _token(client), "Content-Type": "application/json"},
    )
    assert removed.status_code == 200
    assert removed.get_json()["removed"] is True
    assert service.has_manual_session() is False


def test_any_x_link_can_be_downloaded_without_a_search(api_env):
    """Der eingefügte Link muss nicht vorher in einem Ergebnis stehen."""
    client, db, _service = api_env
    antwort = client.post(
        "/api/x/download-link",
        json={"url": "https://x.com/alice/status/1234567890"},
    )
    assert antwort.status_code == 202
    assert antwort.get_json()["post_id"] == "1234567890"
    assert db.get_x_post("1234567890") is not None


@pytest.mark.parametrize("url", [
    "https://example.com/a/status/1234567890",
    "https://x.com/alice",
    "",
])
def test_a_foreign_link_is_refused_by_the_endpoint(api_env, url):
    client, _db, _service = api_env
    antwort = client.post("/api/x/download-link", json={"url": url})
    assert antwort.status_code == 400
    assert antwort.get_json()["error"]


def test_the_only_new_setting_reaches_the_search(api_env):
    """Der Knopf in der Oberflaeche muss auch wirklich ankommen."""
    client, _db, service = api_env
    gesehen = {}
    service._browser_search_override = lambda **kwargs: gesehen.update(kwargs) or []

    assert client.post("/api/x/search",
                       json={"query": "#t", "skip_downloaded": True}).status_code == 202
    end = time.monotonic() + 2
    while time.monotonic() < end and service.search_job.snapshot()["running"]:
        time.sleep(0.01)
    assert gesehen["skip_downloaded"] is True

    gesehen.clear()
    assert client.post("/api/x/search", json={"query": "#t"}).status_code == 202
    end = time.monotonic() + 2
    while time.monotonic() < end and service.search_job.snapshot()["running"]:
        time.sleep(0.01)
    assert gesehen["skip_downloaded"] is False     # Standard: alles zeigen
