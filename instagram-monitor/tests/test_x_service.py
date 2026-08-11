"""Unit tests for the X integration. No test opens a browser or contacts X."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.database import Database
from app.settings import Settings
from app.x_service import (
    XService,
    XValidationError,
    build_search_url,
    extract_post_id,
    normalize_post_url,
    normalize_search_query,
    public_error,
    safe_media_filename,
    safe_path_component,
    validate_result_limit,
    validate_sort,
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
