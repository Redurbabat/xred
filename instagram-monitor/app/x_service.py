"""Desktop-only X video search and download support.

The module itself deliberately imports only the Python standard library.  The
desktop entry point may instantiate :class:`XService`; Playwright and yt-dlp
are imported lazily when a user explicitly opens the X session or starts a
download.  This keeps the shared Flask application importable in Chaquopy.
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from .database import Database
    from .settings import Settings

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 120
MAX_RESULTS = 50
MAX_SCROLL_ROUNDS = 20
MAX_SEARCH_SECONDS = 180
_STATUS_RE = re.compile(r"^/([A-Za-z0-9_]{1,50})/status/([0-9]{5,30})(?:/.*)?$")
_POST_ID_RE = re.compile(r"^[0-9]{5,30}$")
_SECRET_RE = re.compile(
    r"(?i)(cookie|authorization|session(?:id)?|auth[_-]?token|ct0|token)"
    r"(\s*[:=]\s*|%3D)[^\s,;&]+"
)
_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
_PREVIEW_HOSTS = {"pbs.twimg.com", "video.twimg.com", "abs.twimg.com"}


class XServiceError(RuntimeError):
    """Public, already-sanitized error from the X integration."""


class XUnavailableError(XServiceError):
    """A desktop dependency or the X browser is unavailable."""


class XValidationError(ValueError):
    """Invalid user input for an X action."""


class _DownloadCancelled(Exception):
    pass


def public_error(exc: BaseException | str) -> str:
    """Return a short error without cookies, tokens, profile paths or dumps."""
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = re.sub(r"(?i)--cookies(?:-from-browser)?\s+\S+", "--cookies <redacted>", text)
    text = re.sub(r"\s+", " ", text)
    return (text[:500] or "Unbekannter X-Fehler.")


def normalize_search_query(value: Any) -> str:
    """Normalize a hashtag, word or phrase while keeping its meaning."""
    if not isinstance(value, str):
        raise XValidationError("Bitte einen Hashtag oder Suchbegriff eingeben.")
    value = " ".join(value.strip().split())
    if not value or value == "#":
        raise XValidationError("Bitte einen Hashtag oder Suchbegriff eingeben.")
    if any(ord(char) < 32 for char in value):
        raise XValidationError("Der Suchbegriff enthält ungültige Steuerzeichen.")
    if len(value) > MAX_QUERY_LENGTH:
        raise XValidationError(f"Der Suchbegriff darf höchstens {MAX_QUERY_LENGTH} Zeichen haben.")
    if value.startswith("#"):
        tag = value[1:].strip().lstrip("#")
        if not tag or any(char.isspace() for char in tag):
            raise XValidationError("Ein Hashtag darf keine Leerzeichen enthalten.")
        value = f"#{tag}"
    return value


def validate_result_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise XValidationError("Die Ergebnisanzahl muss eine Zahl von 1 bis 50 sein.") from exc
    if not 1 <= limit <= MAX_RESULTS:
        raise XValidationError("Die Ergebnisanzahl muss zwischen 1 und 50 liegen.")
    return limit


def validate_sort(value: Any) -> str:
    sort = str(value or "latest").strip().lower()
    if sort not in {"latest", "top"}:
        raise XValidationError("Sortierung muss „Neueste“ oder „Top“ sein.")
    return sort


def normalize_post_url(value: Any) -> str:
    """Accept only an HTTPS X/Twitter status URL and return canonical x.com."""
    if not isinstance(value, str) or not value.strip():
        raise XValidationError("Kein gültiger X-Beitragslink angegeben.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise XValidationError("Kein gültiger X-Beitragslink angegeben.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise XValidationError("Erlaubt sind nur HTTPS-Beitragslinks von x.com oder twitter.com.")
    match = _STATUS_RE.fullmatch(parsed.path)
    if not match:
        raise XValidationError("Der Link muss auf einen X-Beitrag (/name/status/id) zeigen.")
    username, post_id = match.groups()
    return f"https://x.com/{username}/status/{post_id}"


def extract_post_id(value: Any) -> str:
    path = urlsplit(normalize_post_url(value)).path.split("/")
    return path[3]


def safe_path_component(value: Any, fallback: str = "suche", max_length: int = 64) -> str:
    """Create one deterministic directory component without traversal syntax."""
    raw = normalize_search_query(str(value or fallback)).lstrip("#")
    normalized = unicodedata.normalize("NFKC", raw)
    out: list[str] = []
    previous_dash = False
    for char in normalized:
        if char.isalnum() or char in {"_", "-"}:
            out.append(char)
            previous_dash = False
        elif not previous_dash:
            out.append("-")
            previous_dash = True
    component = "".join(out).strip("-._")[:max_length].strip("-._")
    return component or fallback


def safe_media_filename(post_id: str, username: str, index: int, suffix: str = ".mp4") -> str:
    if not _POST_ID_RE.fullmatch(str(post_id)):
        raise XValidationError("Ungültige X-Beitrags-ID.")
    safe_user = re.sub(r"[^A-Za-z0-9_]", "_", str(username or "unknown"))[:50] or "unknown"
    safe_suffix = suffix.lower() if suffix.lower() in _VIDEO_SUFFIXES else ".mp4"
    return f"{post_id}_{safe_user}_{max(1, int(index))}{safe_suffix}"


def build_search_url(query: str, sort: str, exclude_reposts: bool) -> str:
    terms = f"{normalize_search_query(query)} filter:videos"
    if exclude_reposts:
        terms += " -filter:nativeretweets"
    url = f"https://x.com/search?q={quote(terms)}&src=typed_query"
    if validate_sort(sort) == "latest":
        url += "&f=live"
    return url


def _safe_preview_url(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if parsed.scheme == "https" and (parsed.hostname or "").lower() in _PREVIEW_HOSTS:
        return str(value)
    return ""


@dataclass
class _Command:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None


class _JobState:
    """Small thread-safe state container returned directly by status APIs."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.running = False
        self.finished = False
        self.cancelled = False
        self.total = 0
        self.done = 0
        self.found = 0
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.current = ""
        self.message = ""
        self.error = ""
        self.items: list[dict[str, Any]] = []

    def try_start(self, total: int = 0, message: str = "") -> bool:
        with self._lock:
            if self.running:
                return False
            self._reset_locked()
            self.cancel_event.clear()
            self.running = True
            self.total = total
            self.message = message
            return True

    def update(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def mutate(self, callback: Callable[["_JobState"], None]) -> None:
        with self._lock:
            callback(self)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "finished": self.finished,
                "cancelled": self.cancelled,
                "total": self.total,
                "done": self.done,
                "found": self.found,
                "downloaded": self.downloaded,
                "skipped": self.skipped,
                "failed": self.failed,
                "current": self.current,
                "message": self.message,
                "error": self.error,
                "items": [dict(item) for item in self.items],
            }

    def cancel(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self.cancel_event.set()
            self.message = "Abbruch angefordert …"
            return True


class XService:
    """Owns the desktop browser session and serialized X background jobs."""

    def __init__(
        self,
        db: "Database",
        settings: "Settings",
        session_dir: Path | str,
        *,
        browser_search: Optional[Callable[..., list[dict[str, Any]]]] = None,
        ytdlp_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.session_dir = Path(session_dir)
        self._browser_search_override = browser_search
        self._ytdlp_factory = ytdlp_factory
        self._commands: "queue.Queue[_Command | None]" = queue.Queue()
        self._browser_lock = threading.Lock()
        self._browser_ready = threading.Event()
        self._browser_thread: Optional[threading.Thread] = None
        self._browser_start_error = ""
        self._browser_open = False
        self._session_confirmed = False
        self._operation_lock = threading.Lock()
        self.search_job = _JobState("search")
        self.download_job = _JobState("download")

    @staticmethod
    def dependency_status() -> dict[str, bool]:
        return {
            "playwright": importlib.util.find_spec("playwright") is not None,
            "yt_dlp": importlib.util.find_spec("yt_dlp") is not None,
        }

    @staticmethod
    def public_error(exc: BaseException | str) -> str:
        return public_error(exc)

    def status(self) -> dict[str, Any]:
        deps = self.dependency_status()
        warning = ""
        if not deps["playwright"]:
            warning = "Playwright fehlt. Bitte zuerst `pip install -r requirements.txt` ausführen."
        elif self._browser_start_error:
            warning = self._browser_start_error
        recent_downloads = [
            {
                "post_id": row["post_id"],
                "username": row["username"],
                "post_url": row["post_url"],
                "search_query": row["search_query"],
                "downloaded_at": row["downloaded_at"],
            }
            for row in self.db.recent_x_downloads(20)
        ]
        return {
            "available": True,
            "browser_open": self._browser_open,
            "session_confirmed": self._session_confirmed,
            "dependencies": deps,
            "warning": warning,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "search": self.search_job.snapshot(),
            "download": self.download_job.snapshot(),
            "recent_downloads": recent_downloads,
        }

    def open_login(self) -> dict[str, Any]:
        result = self._browser_call("open_login", timeout=75)
        return {"ok": True, "browser_open": True, "url": result.get("url", "https://x.com/")}

    def start_search(
        self,
        query: Any,
        limit: Any = 10,
        sort: Any = "latest",
        exclude_reposts: Any = False,
    ) -> bool:
        normalized_query = normalize_search_query(query)
        normalized_limit = validate_result_limit(limit)
        normalized_sort = validate_sort(sort)
        if not self._operation_lock.acquire(blocking=False):
            return False
        if not self.search_job.try_start(total=normalized_limit, message="X-Suche wird vorbereitet …"):
            self._operation_lock.release()
            return False

        def run() -> None:
            try:
                results = self._perform_search(
                    normalized_query,
                    normalized_limit,
                    normalized_sort,
                    bool(exclude_reposts),
                    self.search_job.cancel_event,
                )
                unique: dict[str, dict[str, Any]] = {}
                for item in results:
                    canonical_url = normalize_post_url(item.get("post_url"))
                    post_id = extract_post_id(canonical_url)
                    item["post_id"] = post_id
                    item["post_url"] = canonical_url
                    unique[post_id] = item
                results = list(unique.values())[:normalized_limit]
                for item in results:
                    self.db.upsert_x_post(item)
                    item["downloaded"] = self.db.is_x_post_downloaded(item["post_id"])
                self.search_job.update(
                    running=False,
                    finished=True,
                    cancelled=self.search_job.cancel_event.is_set(),
                    found=len(results),
                    done=len(results),
                    items=results,
                    message=(
                        "Suche abgebrochen."
                        if self.search_job.cancel_event.is_set()
                        else f"{len(results)} Videobeitrag/Beiträge gefunden."
                    ),
                )
            except Exception as exc:
                message = self._map_browser_error(exc)
                logger.warning("X-Suche fehlgeschlagen: %s", message)
                self.search_job.update(running=False, finished=True, error=message, message="")
            finally:
                self._operation_lock.release()

        self.search_job.thread = threading.Thread(target=run, daemon=True, name="x-search")
        self.search_job.thread.start()
        return True

    def cancel_search(self) -> bool:
        return self.search_job.cancel()

    def resolve_post_id(self, *, post_id: Any = "", post_url: Any = "") -> str:
        if post_url:
            resolved = extract_post_id(post_url)
        else:
            resolved = str(post_id or "").strip()
            if not _POST_ID_RE.fullmatch(resolved):
                raise XValidationError("Ungültige X-Beitrags-ID.")
        if self.db.get_x_post(resolved) is None:
            raise XValidationError("Der X-Beitrag ist nicht in den Suchergebnissen bekannt.")
        return resolved

    def start_download(self, post_ids: list[Any] | None = None, *, all_new: bool = False) -> bool:
        if all_new:
            current = self.search_job.snapshot().get("items", [])
            ids = [
                item["post_id"] for item in current
                if item.get("post_id") and not self.db.is_x_post_downloaded(item["post_id"])
            ]
        else:
            raw_ids = post_ids or []
            if len(raw_ids) > MAX_RESULTS:
                raise XValidationError("Es können höchstens 50 Beiträge gleichzeitig geladen werden.")
            ids = []
            for raw in raw_ids:
                post_id = self.resolve_post_id(post_id=raw)
                if post_id not in ids:
                    ids.append(post_id)
        if not ids:
            raise XValidationError("Keine neuen X-Videos zum Herunterladen ausgewählt.")
        if not self._operation_lock.acquire(blocking=False):
            return False
        if not self.download_job.try_start(total=len(ids), message="Download wird vorbereitet …"):
            self._operation_lock.release()
            return False

        def run() -> None:
            cookie_file: Optional[Path] = None
            try:
                cookie_file = self._export_cookie_file()
                rows_by_id = {row["post_id"]: row for row in self.db.get_x_posts(ids)}
                item_states = [
                    {"post_id": post_id, "state": "pending", "error": "", "paths": []}
                    for post_id in ids
                ]
                self.download_job.update(items=item_states)
                for index, post_id in enumerate(ids):
                    if self.download_job.cancel_event.is_set():
                        break
                    row = rows_by_id.get(post_id)
                    if row is None:
                        self._finish_download_item(index, "error", "Beitrag nicht mehr bekannt.")
                        continue
                    self.download_job.update(current=post_id, done=index, message="Video wird geladen …")
                    self._set_download_item(index, state="active")
                    try:
                        state, paths = self._download_one(row, cookie_file)
                        if state == "skipped":
                            self.download_job.mutate(lambda job: setattr(job, "skipped", job.skipped + 1))
                        else:
                            self.download_job.mutate(lambda job: setattr(job, "downloaded", job.downloaded + 1))
                        self._finish_download_item(index, state, paths=paths)
                    except _DownloadCancelled:
                        self.download_job.cancel_event.set()
                        self._finish_download_item(index, "cancelled", "Abgebrochen.")
                        break
                    except Exception as exc:
                        message = self._map_download_error(exc)
                        self.db.mark_x_download_error(post_id, message)
                        self.download_job.mutate(lambda job: setattr(job, "failed", job.failed + 1))
                        self._finish_download_item(index, "error", message)
                        logger.warning("X-Download %s fehlgeschlagen: %s", post_id, message)
                    self.download_job.update(done=index + 1)
                snapshot = self.download_job.snapshot()
                cancelled = self.download_job.cancel_event.is_set()
                self.download_job.update(
                    running=False,
                    finished=True,
                    cancelled=cancelled,
                    current="",
                    message=(
                        "Download abgebrochen."
                        if cancelled
                        else (
                            f"Fertig: {snapshot['downloaded']} gespeichert, "
                            f"{snapshot['skipped']} übersprungen, {snapshot['failed']} fehlgeschlagen."
                        )
                    ),
                )
            except Exception as exc:
                message = self._map_download_error(exc)
                logger.warning("X-Downloadauftrag fehlgeschlagen: %s", message)
                self.download_job.update(running=False, finished=True, error=message, message="")
            finally:
                if cookie_file is not None:
                    try:
                        cookie_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._operation_lock.release()

        self.download_job.thread = threading.Thread(target=run, daemon=True, name="x-download")
        self.download_job.thread.start()
        return True

    def cancel_download(self) -> bool:
        return self.download_job.cancel()

    def _perform_search(
        self,
        query: str,
        limit: int,
        sort: str,
        exclude_reposts: bool,
        cancel_event: threading.Event,
    ) -> list[dict[str, Any]]:
        if self._browser_search_override is not None:
            return self._browser_search_override(
                query=query,
                limit=limit,
                sort=sort,
                exclude_reposts=exclude_reposts,
                cancel_event=cancel_event,
            )
        return self._browser_call(
            "search",
            {
                "query": query,
                "limit": limit,
                "sort": sort,
                "exclude_reposts": exclude_reposts,
                "cancel_event": cancel_event,
            },
            timeout=MAX_SEARCH_SECONDS + 45,
        )

    def _ensure_browser(self) -> None:
        with self._browser_lock:
            if self._browser_thread is not None and self._browser_thread.is_alive():
                return
            self._browser_ready.clear()
            self._browser_start_error = ""
            self._browser_thread = threading.Thread(
                target=self._browser_loop, daemon=True, name="x-browser"
            )
            self._browser_thread.start()
        if not self._browser_ready.wait(45):
            raise XUnavailableError("Chromium konnte nicht innerhalb von 45 Sekunden gestartet werden.")
        if self._browser_start_error:
            raise XUnavailableError(self._browser_start_error)

    def _browser_call(
        self, name: str, payload: Optional[dict[str, Any]] = None, timeout: float = 30
    ) -> Any:
        self._ensure_browser()
        command = _Command(name=name, payload=payload or {})
        self._commands.put(command)
        if not command.done.wait(timeout):
            raise XUnavailableError("Die sichtbare X-Browser-Sitzung antwortet nicht.")
        if command.error is not None:
            raise command.error
        return command.result

    def _browser_loop(self) -> None:
        context = None
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise XUnavailableError(
                    "Playwright fehlt. Bitte `pip install -r requirements.txt` ausführen."
                ) from exc
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(self.session_dir), headless=False, viewport=None
                    )
                except Exception as exc:
                    message = public_error(exc)
                    if "executable" in message.lower() or "browser" in message.lower():
                        message = (
                            "Playwright-Chromium fehlt oder startet nicht. Bitte einmalig "
                            "`python -m playwright install chromium` ausführen."
                        )
                    raise XUnavailableError(message) from exc
                context.set_default_timeout(10_000)
                page = context.pages[0] if context.pages else context.new_page()
                self._browser_open = True
                self._browser_ready.set()
                while True:
                    command = self._commands.get()
                    if command is None:
                        break
                    try:
                        if command.name == "open_login":
                            page.goto(
                                "https://x.com/i/flow/login",
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                            page.bring_to_front()
                            command.result = {"url": "https://x.com/i/flow/login"}
                        elif command.name == "search":
                            command.result = self._search_visible_page(page, **command.payload)
                        elif command.name == "cookies":
                            command.result = context.cookies(["https://x.com", "https://twitter.com"])
                        else:
                            raise XServiceError("Unbekannter Browserauftrag.")
                    except Exception as exc:
                        command.error = exc
                        if "closed" in public_error(exc).lower():
                            self._browser_open = False
                    finally:
                        command.done.set()
                    if not self._browser_open:
                        break
        except Exception as exc:
            self._browser_start_error = self._map_browser_error(exc)
            logger.warning("X-Browser nicht verfügbar: %s", self._browser_start_error)
        finally:
            self._browser_open = False
            self._browser_ready.set()
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            while True:
                try:
                    pending = self._commands.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.error = XUnavailableError(
                        self._browser_start_error or "Die X-Browser-Sitzung wurde geschlossen."
                    )
                    pending.done.set()

    def _search_visible_page(
        self,
        page: Any,
        query: str,
        limit: int,
        sort: str,
        exclude_reposts: bool,
        cancel_event: threading.Event,
    ) -> list[dict[str, Any]]:
        page.goto(
            build_search_url(query, sort, exclude_reposts),
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        if "/login" in page.url or "/i/flow/login" in page.url:
            self._session_confirmed = False
            raise XServiceError(
                "Die X-Sitzung ist nicht angemeldet oder abgelaufen. Bitte „Bei X anmelden“ öffnen."
            )
        try:
            page.locator("article").first.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=2_000).lower()
            except Exception:
                pass
            if any(word in body for word in ("log in", "sign in", "anmelden")):
                self._session_confirmed = False
                raise XServiceError(
                    "Die X-Sitzung ist abgelaufen. Bitte „Bei X anmelden“ öffnen und manuell anmelden."
                ) from exc
            raise XServiceError(
                "X zeigt keine Beitragskarten. Möglicherweise wurde das Layout geändert oder eine Sicherheitsabfrage wartet im Browser."
            ) from exc

        self._session_confirmed = True
        results: dict[str, dict[str, Any]] = {}
        started = time.monotonic()
        stagnant_rounds = 0
        for round_number in range(MAX_SCROLL_ROUNDS + 1):
            if cancel_event.is_set() or time.monotonic() - started >= MAX_SEARCH_SECONDS:
                break
            before = len(results)
            articles = page.locator("article")
            for index in range(articles.count()):
                if cancel_event.is_set() or len(results) >= limit:
                    break
                item = self._article_data(articles.nth(index), query)
                if item is not None:
                    results[item["post_id"]] = item
            self.search_job.update(
                found=len(results), done=len(results), items=list(results.values()),
                message=f"{len(results)} von {limit} Videobeiträgen gefunden …",
            )
            if len(results) >= limit:
                break
            stagnant_rounds = stagnant_rounds + 1 if len(results) == before else 0
            if stagnant_rounds >= 3 or round_number >= MAX_SCROLL_ROUNDS:
                break
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.85, 700))")
            if cancel_event.wait(1.5):
                break
        return list(results.values())[:limit]

    def _article_data(self, article: Any, query: str) -> Optional[dict[str, Any]]:
        try:
            videos = article.locator("video")
            if videos.count() == 0 or not videos.first.is_visible():
                return None
            hrefs = article.locator("a[href*='/status/']").evaluate_all(
                "els => els.map(el => el.href)"
            )
            post_url = ""
            for href in hrefs:
                try:
                    post_url = normalize_post_url(href)
                    break
                except XValidationError:
                    continue
            if not post_url:
                return None
            parts = urlsplit(post_url).path.split("/")
            username, post_id = parts[1], parts[3]
            published_at = ""
            times = article.locator("time")
            if times.count():
                published_at = times.first.get_attribute("datetime") or ""
            preview_url = _safe_preview_url(videos.first.get_attribute("poster"))
            caption = ""
            tweet_text = article.locator('[data-testid="tweetText"]')
            if tweet_text.count():
                caption = tweet_text.first.inner_text().strip()
            if not caption:
                caption = article.inner_text().strip()
            caption = " ".join(caption.split())[:600]
            author_name = ""
            user_block = article.locator('[data-testid="User-Name"]')
            if user_block.count():
                for line in user_block.first.inner_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("@"):
                        author_name = line[:120]
                        break
            return {
                "post_id": post_id,
                "username": username,
                "author_name": author_name,
                "post_url": post_url,
                "search_query": query,
                "caption": caption,
                "preview_url": preview_url,
                "published_at": published_at,
                "downloaded": self.db.is_x_post_downloaded(post_id),
            }
        except Exception:
            logger.debug("Eine sichtbare X-Beitragskarte konnte nicht gelesen werden.", exc_info=True)
            return None

    def _export_cookie_file(self) -> Optional[Path]:
        if not self._browser_open:
            return None
        try:
            cookies = self._browser_call("cookies", timeout=15)
        except Exception:
            return None
        if not cookies:
            return None
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_dir / ".yt-dlp-cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        for cookie in cookies:
            domain = str(cookie.get("domain") or "")
            if not (domain == ".x.com" or domain.endswith(".x.com") or domain.endswith(".twitter.com")):
                continue
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if cookie.get("secure") else "FALSE"
            expires = int(float(cookie.get("expires") or 0))
            name = str(cookie.get("name") or "").replace("\t", "")
            value = str(cookie.get("value") or "").replace("\t", "").replace("\n", "")
            lines.append(f"{domain}\t{include_subdomains}\t/\t{secure}\t{expires}\t{name}\t{value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def _download_one(self, row: Any, cookie_file: Optional[Path]) -> tuple[str, list[str]]:
        post_id = str(row["post_id"])
        post_url = normalize_post_url(row["post_url"])
        username = str(row["username"] or urlsplit(post_url).path.split("/")[1])
        query_dir = safe_path_component(row["search_query"] or "suche")
        videos_dir = self.settings.download_dir / "X" / query_dir / "Videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            path for path in videos_dir.glob(f"{post_id}_*")
            if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES
        )
        if self.db.is_x_post_downloaded(post_id) or existing:
            if existing and not self.db.is_x_post_downloaded(post_id):
                self.db.mark_x_post_downloaded(post_id, str(existing[0]))
            return "skipped", [str(path) for path in existing]

        partial_dir = videos_dir / f".partial-{post_id}"
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        partial_dir.mkdir(parents=True)
        ffmpeg = shutil.which("ffmpeg")

        def progress_hook(data: dict[str, Any]) -> None:
            if self.download_job.cancel_event.is_set():
                raise _DownloadCancelled()
            if data.get("status") == "downloading":
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
                percent = int(downloaded * 100 / total) if total else 0
                self.download_job.update(message=f"{post_id}: {percent}%")

        options: dict[str, Any] = {
            "format": "bestvideo*+bestaudio/best" if ffmpeg else "best[ext=mp4]/best",
            "outtmpl": str(partial_dir / "media_%(autonumber)02d.%(ext)s"),
            "autonumber_start": 1,
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "overwrites": True,
            "continuedl": True,
        }
        if ffmpeg:
            options["merge_output_format"] = "mp4"
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
        factory = self._ytdlp_factory or self._default_ytdlp_factory
        try:
            try:
                with factory(options) as ydl:
                    ydl.extract_info(post_url, download=True)
            except Exception as exc:
                if self.download_job.cancel_event.is_set():
                    raise _DownloadCancelled() from exc
                raise
            media_files = sorted(
                path for path in partial_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES
            )
            if not media_files:
                raise XServiceError("yt-dlp hat keine fertige Videodatei erzeugt.")
            final_paths: list[str] = []
            for index, source in enumerate(media_files, 1):
                target = videos_dir / safe_media_filename(post_id, username, index, source.suffix)
                if target.exists():
                    source.unlink(missing_ok=True)
                else:
                    source.replace(target)
                final_paths.append(str(target))
            self.db.mark_x_post_downloaded(post_id, final_paths[0])
            return "downloaded", final_paths
        finally:
            if partial_dir.exists():
                shutil.rmtree(partial_dir, ignore_errors=True)

    @staticmethod
    def _default_ytdlp_factory(options: dict[str, Any]) -> Any:
        try:
            import yt_dlp
        except ImportError as exc:
            raise XUnavailableError(
                "yt-dlp fehlt. Bitte `pip install -r requirements.txt` ausführen."
            ) from exc
        return yt_dlp.YoutubeDL(options)

    def _set_download_item(self, index: int, **values: Any) -> None:
        def change(job: _JobState) -> None:
            if 0 <= index < len(job.items):
                job.items[index].update(values)

        self.download_job.mutate(change)

    def _finish_download_item(
        self,
        index: int,
        state: str,
        error: str = "",
        paths: Optional[list[str]] = None,
    ) -> None:
        self._set_download_item(index, state=state, error=error, paths=paths or [])
        if state in {"downloaded", "skipped"}:
            item = self.download_job.snapshot()["items"][index]
            post_id = item["post_id"]

            def mark(job: _JobState) -> None:
                for result in job.items:
                    if result.get("post_id") == post_id:
                        result["downloaded"] = True

            self.search_job.mutate(mark)

    @staticmethod
    def _map_browser_error(exc: BaseException) -> str:
        if isinstance(exc, XServiceError):
            return public_error(exc)
        message = public_error(exc)
        lower = message.lower()
        if "playwright" in lower and "install" in lower:
            return message
        if "executable" in lower or "browser has been closed" in lower:
            return (
                "Playwright-Chromium fehlt oder wurde geschlossen. Bitte einmalig "
                "`python -m playwright install chromium` ausführen und die Sitzung neu öffnen."
            )
        return f"X-Browserfehler: {message}"

    @staticmethod
    def _map_download_error(exc: BaseException) -> str:
        if isinstance(exc, _DownloadCancelled):
            return "Download abgebrochen."
        message = public_error(exc)
        if "ffmpeg" in message.lower():
            return (
                "FFmpeg wird für diese Video-/Audio-Kombination benötigt. Bitte FFmpeg installieren "
                "und die Anwendung neu starten."
            )
        return message
