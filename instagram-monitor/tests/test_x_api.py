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
