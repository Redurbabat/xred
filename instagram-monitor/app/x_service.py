"""Desktop-only X video search and download support.

The module itself deliberately imports only the Python standard library.  The
desktop entry point may instantiate :class:`XService`; Playwright and yt-dlp
are imported lazily when a user explicitly opens the X session or starts a
download.  This keeps the shared Flask application importable in Chaquopy.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

if TYPE_CHECKING:
    from .database import Database
    from .settings import Settings

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 120
MAX_RESULTS = 50
MAX_SCROLL_ROUNDS = 20
MAX_SEARCH_SECONDS = 180
# Taktgeber der Browserschleife im Leerlauf. Kurz genug, dass ein zugeklicktes
# Fenster praktisch sofort auffällt, und praktisch kostenlos: Der Ping erzeugt
# keinen Verkehr zum Browser, nur ein setTimeout im Playwright-Treiber.
_IDLE_TICK_SECONDS = 0.75
MAX_IDENTIFIER_LENGTH = 254
MAX_USERNAME_LENGTH = 15
MAX_PASSWORD_LENGTH = 256
LOGIN_TIMEOUT_SECONDS = 150
_STATUS_RE = re.compile(r"^/([A-Za-z0-9_]{1,50})/status/([0-9]{5,30})(?:/.*)?$")
_POST_ID_RE = re.compile(r"^[0-9]{5,30}$")
_SECRET_RE = re.compile(
    r"(?i)(cookie|authorization|session(?:id)?|auth[_-]?token|ct0|token)"
    r"(\s*[:=]\s*|%3D)[^\s,;&]+"
)
_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
_PREVIEW_HOSTS = {"pbs.twimg.com", "video.twimg.com", "abs.twimg.com"}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# Sitzungs-Cookies von X: auth_token ist 40 Hex-Zeichen, ct0 je nach
# Zeitpunkt 32 bis 160. Grosszuegig gefasst, aber ohne Sonderzeichen.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TOKEN_MIN, _TOKEN_MAX = 16, 200
_TOKEN_RE = re.compile(rf"^[0-9a-fA-F]{{{_TOKEN_MIN},{_TOKEN_MAX}}}$")
# Von Hand hinterlegte Sitzung (Token-Anmeldung) im Profilordner.
_MANUAL_COOKIE_FILE = "manual_session.json"
_LOGIN_URL = "https://x.com/i/flow/login"
_HOME_URL = "https://x.com/home"
_BOOKMARKS_URL = "https://x.com/i/bookmarks"

# Quellen der Beitragsliste. „search“ ist die Stichwortsuche, die beiden
# anderen sind die eigenen Listen aus dem X-Bereich „Verlauf“ (Reiter
# „Lesezeichen“ und das Herz). Alle drei liefern dieselben Beitragskarten,
# deshalb unterscheidet sich nur die aufgerufene Adresse.
SOURCE_SEARCH = "search"
SOURCE_LIKES = "likes"
SOURCE_BOOKMARKS = "bookmarks"
# Zugleich der Ordnername unterhalb von <Download-Ordner>/X/.
_SOURCE_LABELS = {SOURCE_LIKES: "Likes", SOURCE_BOOKMARKS: "Lesezeichen"}

# Chromium ohne Passwortmanager, ohne Speichern-Dialog und ohne Autofill-
# Server: Das X-Passwort soll ausschließlich im Formular landen und nirgends
# im Browserprofil gespeichert werden.
_NO_PASSWORD_MANAGER_ARGS = (
    "--disable-features="
    "PasswordManagerEnableAccountStorage,PasswordManagerOnboarding,"
    "PasswordLeakDetection,PasswordGeneration,AutofillServerCommunication,"
    "AutofillEnableAccountWalletStorage",
    "--disable-save-password-bubble",
    "--password-store=basic",
    "--use-mock-keychain",
)

# Dieselbe Abschaltung als Profil-Einstellung – Chromium liest sie beim Start
# aus <profil>/Default/Preferences und zeigt dann kein „Passwort speichern?“.
_NO_PASSWORD_MANAGER_PREFS: dict[str, Any] = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {
        "password_manager_enabled": False,
        "password_manager_leak_detection": False,
    },
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}

# Bewusst KEIN nacktes „closed“: Sonst gilt auch net::ERR_CONNECTION_CLOSED –
# ein vorübergehender Netzwerkfehler – als geschlossenes Fenster, und die
# komplette Chromium-Sitzung würde dafür abgeräumt. „has been closed“ deckt
# alle Playwright-Formulierungen ab („Target page, context or browser has
# been closed“, „Browser has been closed“, „Target page has been closed“).
_CLOSED_MARKERS = (
    "has been closed",
    "target closed",
    "browser has disconnected",
    "event loop is closed",
)

_LOGIN_MESSAGES = {
    "signed_in": "Bei X angemeldet. Die Sitzung bleibt in diesem Browserprofil gespeichert.",
    "verification": (
        "X verlangt eine zusätzliche Bestätigung (2FA-Code, CAPTCHA oder Sicherheitsabfrage). "
        "Bitte im geöffneten X-Fenster abschließen."
    ),
    "pending": (
        "Zugangsdaten wurden im X-Fenster eingetragen. Bitte dort prüfen und die Anmeldung "
        "abschließen."
    ),
}

# Beschriftungen der X-Anmeldemaske (Deutsch und Englisch).
_NEXT_LABELS = ("Weiter", "Next")
_SUBMIT_LABELS = ("Anmelden", "Log in", "Login", "Sign in")
_VERIFICATION_HINTS = (
    "bestätigungscode",
    "verification code",
    "authentication code",
    "zwei-faktor",
    "two-factor",
    "captcha",
    "arkose",
    "ungewöhnliche aktivität",
    "unusual login activity",
    "sicherheitsabfrage",
    "deine identität",
    "your identity",
)
# Pfade, die NICHT „angemeldet“ bedeuten: der Anmeldeablauf selbst sowie die
# Entsperr- und Rückfrageseiten, die X bei eingeschränkten Konten zeigt. Ohne
# sie würde eine solche Seite als fertige Anmeldung durchgehen.
# Hosts, die wirklich zu X gehören. Eine Stelle für beide Prüfungen:
# Beitragslinks (normalize_post_url) und „bin ich angemeldet?“.
_X_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_NOT_SIGNED_IN_PATHS = (
    "/login",
    "/i/flow/",
    "/account/access",
    "/account/suspended",
)
_LOGIN_FAILURE_HINTS = (
    "falsches passwort",
    "wrong password",
    "incorrect password",
    "passwort war falsch",
    "password you entered was incorrect",
    "konnten wir nicht finden",
    "could not find your account",
    "wir konnten dich nicht finden",
)


class XServiceError(RuntimeError):
    """Public, already-sanitized error from the X integration."""


class XUnavailableError(XServiceError):
    """A desktop dependency or the X browser is unavailable."""


class XValidationError(XServiceError, ValueError):
    """Invalid user input for an X action.

    Erbt von beiden: ``XServiceError``, damit ``_map_browser_error`` ein
    Datenproblem als Klartext meldet statt als „X-Browserfehler“, und
    ``ValueError``, damit die ``except (TypeError, ValueError)``-Zweige in
    web.py weiterhin sauber mit HTTP 400 antworten.
    """


class XLoginError(XServiceError):
    """The direct X login failed; the message never contains the password."""


class _DownloadCancelled(Exception):
    pass


def public_error(exc: BaseException | str) -> str:
    """Return a short error without cookies, tokens, profile paths or dumps."""
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    # yt-dlp faerbt seine Meldungen fuer das Terminal ein. In der Oberflaeche
    # standen die Steuerzeichen als "[0;31mERROR:[0m" mitten im Text.
    text = _ANSI_RE.sub("", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = re.sub(r"(?i)--cookies(?:-from-browser)?\s+\S+", "--cookies <redacted>", text)
    text = re.sub(r"\s+", " ", text)
    return (text[:500] or "Unbekannter X-Fehler.")


def redact_secret(text: str, secret: str) -> str:
    """Remove a password from any text before it is shown, returned or logged."""
    if not secret or len(secret) < 3:
        return text
    return text.replace(secret, "<redacted>")


def normalize_login_identifier(value: Any) -> str:
    """Normalize the e-mail address or user name used for the X login."""
    if not isinstance(value, str):
        raise XValidationError("Bitte E-Mail-Adresse oder X-Benutzernamen eingeben.")
    identifier = value.strip()
    if identifier.startswith("@"):
        identifier = identifier[1:].strip()
    if not identifier:
        raise XValidationError("Bitte E-Mail-Adresse oder X-Benutzernamen eingeben.")
    if any(ord(char) < 32 or ord(char) == 127 for char in identifier):
        raise XValidationError("Die Kennung enthält ungültige Steuerzeichen.")
    if len(identifier) > MAX_IDENTIFIER_LENGTH:
        raise XValidationError(
            f"Die Kennung darf höchstens {MAX_IDENTIFIER_LENGTH} Zeichen haben."
        )
    return identifier


def normalize_optional_username(value: Any) -> str:
    """Normalize the optional ``@name`` X asks for during unusual-activity checks."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise XValidationError("Der @Benutzername ist ungültig.")
    username = value.strip().lstrip("@").strip()
    if not username:
        return ""
    if not _USERNAME_RE.fullmatch(username):
        raise XValidationError(
            "Der @Benutzername darf nur Buchstaben, Ziffern und _ enthalten "
            f"(höchstens {MAX_USERNAME_LENGTH} Zeichen)."
        )
    return username


def validate_password(value: Any) -> str:
    """Check the password without ever putting it into an error message."""
    if not isinstance(value, str) or not value:
        raise XValidationError("Bitte das X-Passwort eingeben.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise XValidationError("Das Passwort enthält ungültige Steuerzeichen.")
    if len(value) > MAX_PASSWORD_LENGTH:
        raise XValidationError(
            f"Das Passwort darf höchstens {MAX_PASSWORD_LENGTH} Zeichen haben."
        )
    return value


def write_no_password_manager_prefs(session_dir: Path | str) -> Path:
    """Turn Chromium's password manager off inside the persistent X profile."""
    profile = Path(session_dir) / "Default"
    profile.mkdir(parents=True, exist_ok=True)
    path = profile / "Preferences"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    for key, value in _NO_PASSWORD_MANAGER_PREFS.items():
        if isinstance(value, dict):
            section = data.get(key)
            if not isinstance(section, dict):
                section = {}
            section.update(value)
            data[key] = section
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def is_browser_closed_error(exc: BaseException | str) -> bool:
    """Recognize a Playwright error caused by a closed window or context."""
    text = str(exc).lower()
    return any(marker in text for marker in _CLOSED_MARKERS)


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


def chromium_install_hint() -> str:
    """Nennt den Befehl, der Chromium WIRKLICH an die richtige Stelle holt.

    Die frühere Meldung riet zu ``python -m playwright install chromium``.
    Wer das in der Eingabeaufforderung tippt, trifft damit das
    System-Python – die App läuft aber in ihrer eigenen Umgebung unter
    ``.venv``. Der Browser landete also neben der App und der Fehler blieb.
    Deshalb hier der Pfad des laufenden Interpreters, der immer stimmt.
    """
    skript = "setup-x-browser.bat" if sys.platform.startswith("win") else "./setup-x-browser.sh"
    return (
        f"Am einfachsten einmalig {skript} ausführen. "
        f'Oder von Hand: "{sys.executable}" -m playwright install chromium.'
    )


def ffmpeg_path() -> str:
    """Pfad zu FFmpeg – aus dem System oder aus dem Python-Paket.

    FFmpeg verbindet die getrennt ausgelieferten besten Bild- und
    Tonspuren. Ohne FFmpeg bleibt nur eine fertige MP4-Variante, also
    oft geringere Qualität und bei manchen Beiträgen gar kein Ton.
    Unter Windows ist FFmpeg selten installiert, und eine Anleitung
    ("bitte FFmpeg installieren") hilft wenig. Deshalb wird zusätzlich
    das Paket ``imageio-ffmpeg`` genutzt, das eine eigene Programmdatei
    mitbringt – ohne Administratorrechte und ohne PATH-Gebastel.
    """
    gefunden = shutil.which("ffmpeg")
    if gefunden:
        return gefunden
    try:
        import imageio_ffmpeg
    except ImportError:
        return ""
    try:
        pfad = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.debug("Mitgeliefertes FFmpeg nicht nutzbar.", exc_info=True)
        return ""
    return pfad if pfad and Path(pfad).exists() else ""


def describe_launch_failure(exc: BaseException) -> str:
    """Erklärt, warum Chromium nicht startet – ohne den Grund zu verschlucken.

    Bisher wurde jede Meldung, in der „executable“ ODER „browser“ vorkam,
    durch „Chromium fehlt“ ersetzt. Da fast jede Playwright-Meldung das Wort
    „browser“ enthält, verdeckte dieser Pauschaltext auch ganz andere
    Ursachen – etwa eine von Windows blockierte Datei. Der Originaltext
    wird deshalb immer mitgegeben.
    """
    message = public_error(exc)
    lower = message.lower()
    if "0xc0e90002" in lower or "integrity" in lower or "virus" in lower:
        kopf = (
            "Windows hat Chromium blockiert (Code-Integrität). Das ist meist "
            "Smart App Control oder ein Virenscanner – die Chromium-Ausgabe "
            "von Playwright ist nicht signiert."
        )
    elif "doesn't exist" in lower or "executable" in lower or "install" in lower:
        kopf = "Playwright-Chromium fehlt oder startet nicht. " + chromium_install_hint()
    else:
        kopf = "Chromium konnte nicht gestartet werden."
    return f"{kopf} Originalmeldung: {message}"


def validate_x_token(value: Any, field: str) -> str:
    """Prüft einen X-Sitzungs-Cookie (``auth_token`` bzw. ``ct0``).

    Beide sind reine Hex-Zeichenketten. Bewusst streng: Der Wert landet in
    einer Cookie-Datei, in der ein Zeilenumbruch oder Tabulator weitere
    Einträge unterschieben könnte.
    """
    token = str(value or "").strip().strip('"').strip()
    if not token:
        raise XValidationError(f"Bitte {field} eintragen.")
    if not _TOKEN_RE.fullmatch(token):
        raise XValidationError(
            f"{field} sieht nicht wie ein X-Cookie aus: erwartet werden "
            f"{_TOKEN_MIN}–{_TOKEN_MAX} Zeichen aus 0-9 und a-f."
        )
    return token.lower()


def validate_source(value: Any) -> str:
    """Prüft die Herkunft der Liste: Suche, Likes oder Lesezeichen."""
    source = str(value or SOURCE_SEARCH).strip().lower()
    if source not in {SOURCE_SEARCH, SOURCE_LIKES, SOURCE_BOOKMARKS}:
        raise XValidationError("Unbekannte Quelle. Erlaubt: Suche, Likes oder Lesezeichen.")
    return source


def source_label(source: str) -> str:
    """Anzeigename und Ordnername einer Quelle."""
    return _SOURCE_LABELS.get(validate_source(source), "")


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
        or host not in _X_HOSTS
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


def safe_username_component(username: Any) -> str:
    """Ein X-Name als Ordner-/Dateinamensteil – ohne Pfadsyntax.

    Bewusst NICHT ``safe_path_component``: Das geht über
    ``normalize_search_query`` und lehnt Eingaben ab, die als Suchbegriff
    unsinnig wären. Hier zählt nur, dass nichts Gefährliches übrig bleibt;
    echte X-Namen bestehen ohnehin aus [A-Za-z0-9_].
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(username or "unknown"))[:50] or "unknown"


def safe_media_filename(post_id: str, username: str, index: int, suffix: str = ".mp4") -> str:
    if not _POST_ID_RE.fullmatch(str(post_id)):
        raise XValidationError("Ungültige X-Beitrags-ID.")
    safe_user = safe_username_component(username)
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
    """Nur https-Vorschaubilder von X – und nur neu zusammengesetzt.

    Der Originalstring wird bewusst NICHT zurückgegeben: Er kann hinter dem
    geprüften Hostnamen noch Anführungszeichen oder Steuerzeichen tragen, mit
    denen der Wert im Frontend aus einem Attribut ausbrechen könnte. Der Umbau
    über urlunsplit wirft alles weg, was nicht Teil einer URL ist.
    """
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _PREVIEW_HOSTS:
        return ""
    if any(ord(char) < 32 or char in '"\'<>' for char in str(value)):
        return ""
    return urlunsplit(parsed)


@dataclass
class _Command:
    """Ein Browserauftrag samt Übergabe zwischen Aufrufer und Browser-Thread.

    ``abandon`` und ``claim`` verhindern, dass ein nach Zeitüberschreitung
    aufgegebener Auftrag später doch noch ausgeführt wird – mit dann bereits
    geleerten Nutzdaten. Ohne diesen Handschlag navigierte ein abgelaufenes
    ``open_login`` das sichtbare Fenster mitten in eine laufende Suche hinein.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _claimed: bool = False
    _abandoned: bool = False

    def abandon(self) -> None:
        """Aufrufer gibt auf. Läuft der Auftrag schon, bleiben die Daten."""
        with self._lock:
            if not self._claimed:
                self._abandoned = True
                self.payload.clear()

    def claim(self) -> bool:
        """True, wenn der Browser-Thread den Auftrag ausführen darf."""
        with self._lock:
            if self._abandoned:
                self.payload.clear()
                return False
            self._claimed = True
            return True


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
        # Eine per Token hinterlegte Sitzung überlebt den Neustart – das muss
        # die Anzeige auch wissen. Sonst stand nach jedem Start wieder
        # „nicht angemeldet“ da, obwohl die Token längst gespeichert waren,
        # und man trug sie unnötig erneut ein.
        self._session_confirmed = bool(self._manual_cookies())
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
            "manual_session": self.has_manual_session(),
            "dependencies": deps,
            "warning": warning,
            "ffmpeg": bool(ffmpeg_path()),
            "search": self.search_job.snapshot(),
            "download": self.download_job.snapshot(),
            "recent_downloads": recent_downloads,
        }

    def open_login(self) -> dict[str, Any]:
        result = self._browser_call("open_login", timeout=75)
        return {"ok": True, "browser_open": True, "url": result.get("url", "https://x.com/")}

    # ------------------------------------------------------------------
    # Anmeldung per Sitzungs-Token (auth_token + ct0)
    # ------------------------------------------------------------------
    def _manual_cookie_path(self) -> Path:
        return self.session_dir / _MANUAL_COOKIE_FILE

    def login_with_tokens(self, auth_token: Any, ct0: Any) -> dict[str, Any]:
        """Übernimmt eine bestehende X-Sitzung aus zwei Cookies.

        Vorteil gegenüber dem Passwort: Es findet keine Anmeldung statt, die
        X als ungewöhnlich bewerten könnte – für X geht die vorhandene
        Sitzung einfach weiter. Ausserdem laufen Downloads damit ganz ohne
        Browser, weil yt-dlp die Cookies direkt aus dieser Datei bekommt.
        """
        token = validate_x_token(auth_token, "auth_token")
        csrf = validate_x_token(ct0, "ct0")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self._manual_cookie_path()
        path.write_text(
            json.dumps({"auth_token": token, "ct0": csrf}),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:  # Windows kennt den Modus nicht
            logger.debug("Dateirechte der Sitzungsdatei nicht setzbar.", exc_info=True)
        self._session_confirmed = True
        # Läuft bereits ein Fenster, bekommt es die Cookies sofort; sonst
        # werden sie beim nächsten Start übernommen.
        if self._browser_open:
            try:
                self._browser_call("set_cookies", {"cookies": self._manual_cookies()}, timeout=20)
            except Exception:
                logger.debug("Cookies konnten nicht sofort gesetzt werden.", exc_info=True)
        return {"ok": True, "state": "signed_in",
                "message": "X-Sitzung übernommen. Suche und Download können starten."}

    def forget_tokens(self) -> bool:
        """Entfernt die hinterlegte Token-Sitzung (ohne das Browserprofil)."""
        path = self._manual_cookie_path()
        existed = path.exists()
        path.unlink(missing_ok=True)
        # Die Arbeitskopie für yt-dlp mit entfernen, falls ein Download sie
        # gerade liegen gelassen hat.
        (self.session_dir / ".yt-dlp-cookies.txt").unlink(missing_ok=True)
        return existed

    def _manual_cookies(self) -> list[dict[str, Any]]:
        """Die hinterlegte Sitzung als Playwright-Cookieliste (leer = keine)."""
        try:
            raw = json.loads(self._manual_cookie_path().read_text(encoding="utf-8"))
            token = validate_x_token(raw.get("auth_token"), "auth_token")
            csrf = validate_x_token(raw.get("ct0"), "ct0")
        except (OSError, ValueError, XValidationError):
            return []
        return [
            {
                "name": name, "value": value, "domain": ".x.com", "path": "/",
                "secure": True, "httpOnly": name == "auth_token", "sameSite": "None",
            }
            for name, value in (("auth_token", token), ("ct0", csrf))
        ]

    def has_manual_session(self) -> bool:
        return bool(self._manual_cookies())

    def _refresh_manual_session(self, cookies: list[dict[str, Any]]) -> None:
        """Hält die hinterlegte Sitzung auf dem Stand des Browsers.

        X tauscht ``ct0`` im laufenden Betrieb aus. Der einmal eingetragene
        Wert veraltet dadurch, und irgendwann gilt die gespeicherte Sitzung
        als ungültig – man müsste beide Werte neu heraussuchen. Liefert das
        Fenster frischere Cookies, werden sie deshalb übernommen.
        """
        if not self._manual_cookies():
            return  # ohne Token-Anmeldung nichts zu pflegen
        frisch = {
            str(c.get("name")): str(c.get("value") or "")
            for c in cookies
            if str(c.get("name")) in ("auth_token", "ct0")
        }
        try:
            neu = {
                "auth_token": validate_x_token(frisch.get("auth_token"), "auth_token"),
                "ct0": validate_x_token(frisch.get("ct0"), "ct0"),
            }
        except XValidationError:
            return  # unvollständig oder unbrauchbar: lieber den alten Stand behalten
        path = self._manual_cookie_path()
        try:
            if json.loads(path.read_text(encoding="utf-8")) == neu:
                return
        except (OSError, ValueError):
            pass
        try:
            path.write_text(json.dumps(neu), encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            logger.debug("Aufgefrischte X-Sitzung nicht speicherbar.", exc_info=True)
        else:
            logger.info("Hinterlegte X-Sitzung auf den Stand des Browsers gebracht.")

    def login(self, identifier: Any, password: Any, username: Any = "") -> dict[str, Any]:
        """Fill X's own login form in the visible browser window.

        The password is only passed through to the form: it is never stored,
        logged, echoed back or written into an error message.  Everything X
        asks for on top of it (2FA code, CAPTCHA, security question) stays in
        the visible window and is answered by the user.
        """
        normalized_identifier = normalize_login_identifier(identifier)
        normalized_username = normalize_optional_username(username)
        secret = validate_password(password)
        try:
            result = self._browser_call(
                "login",
                {
                    "identifier": normalized_identifier,
                    "username": normalized_username,
                    "password": secret,
                },
                timeout=LOGIN_TIMEOUT_SECONDS + 30,
            )
        except XValidationError:
            raise
        except XUnavailableError as exc:
            # Fehlender/geschlossener Browser ist kein Zugangsdatenproblem.
            raise XUnavailableError(self._map_login_error(exc, secret)) from None
        except Exception as exc:
            raise XLoginError(self._map_login_error(exc, secret)) from None
        finally:
            secret = ""
        state = str(result.get("state") or "pending")
        return {
            "ok": True,
            "state": state,
            "browser_open": self._browser_open,
            "session_confirmed": state == "signed_in",
            "message": _LOGIN_MESSAGES.get(state, _LOGIN_MESSAGES["pending"]),
        }

    def _finish_job(self, job: _JobState, release: Callable[[], None], **values: Any) -> None:
        """Endzustand veröffentlichen und dabei die Sperre freigeben.

        Beides muss unter EINEM gehaltenen Job-Lock passieren. Sonst sieht die
        Oberfläche „fertig“, während die Sperre noch liegt, startet sofort den
        nächsten Auftrag und bekommt dafür HTTP 409 – auf den eigenen Klick hin.
        Die umgekehrte Reihenfolge hilft nicht: ``try_start`` scheiterte dann an
        ``running == True``.
        """

        def apply(state: _JobState) -> None:
            for key, value in values.items():
                setattr(state, key, value)
            state.running = False
            state.finished = True
            release()

        job.mutate(apply)

    @staticmethod
    def _once(release: Callable[[], None]) -> Callable[[], None]:
        """Kapselt eine Freigabe, die höchstens einmal wirken darf."""
        done = threading.Event()

        def guarded() -> None:
            if not done.is_set():
                done.set()
                release()

        return guarded

    def start_search(
        self,
        query: Any,
        limit: Any = 10,
        sort: Any = "latest",
        exclude_reposts: Any = False,
        source: Any = SOURCE_SEARCH,
    ) -> bool:
        normalized_source = validate_source(source)
        if normalized_source == SOURCE_SEARCH:
            normalized_query = normalize_search_query(query)
            start_message = "X-Suche wird vorbereitet …"
        else:
            # Likes und Lesezeichen brauchen keinen Suchbegriff. Das Etikett
            # wandert als „search_query“ mit und bestimmt damit zugleich den
            # Zielordner (…/X/Likes/Videos bzw. …/X/Lesezeichen/Videos).
            normalized_query = _SOURCE_LABELS[normalized_source]
            start_message = f"{normalized_query} werden geladen …"
        normalized_limit = validate_result_limit(limit)
        normalized_sort = validate_sort(sort)
        if not self._operation_lock.acquire(blocking=False):
            return False
        if not self.search_job.try_start(total=normalized_limit, message=start_message):
            self._operation_lock.release()
            return False

        release = self._once(self._operation_lock.release)

        def run() -> None:
            try:
                results = self._perform_search(
                    normalized_query,
                    normalized_limit,
                    normalized_sort,
                    bool(exclude_reposts),
                    self.search_job.cancel_event,
                    normalized_source,
                )
                unique: dict[str, dict[str, Any]] = {}
                unreadable = 0
                for item in results:
                    # Pro Element abfangen: Eine einzige kaputte Karte hat
                    # früher die komplette Suche mit null Treffern beendet.
                    try:
                        canonical_url = normalize_post_url(item.get("post_url"))
                    except XValidationError:
                        unreadable += 1
                        continue
                    post_id = extract_post_id(canonical_url)
                    item["post_id"] = post_id
                    item["post_url"] = canonical_url
                    unique[post_id] = item
                if unreadable:
                    logger.info("%d X-Beitragskarte(n) ohne brauchbaren Link übersprungen.", unreadable)
                results = list(unique.values())[:normalized_limit]
                for item in results:
                    self.db.upsert_x_post(item)
                    item["downloaded"] = self.db.is_x_post_downloaded(item["post_id"])
                # Gelegenheit nutzen: X hat beim Suchen womöglich ein neues
                # ct0 gesetzt. Wer nur sucht und nie herunterlädt, hätte
                # sonst irgendwann eine veraltete hinterlegte Sitzung.
                if self._browser_open and self.has_manual_session():
                    try:
                        self._refresh_manual_session(
                            self._browser_call("cookies", timeout=15) or []
                        )
                    except Exception:
                        logger.debug("Sitzung nach der Suche nicht auffrischbar.",
                                     exc_info=True)
                self._finish_job(
                    self.search_job,
                    release,
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
                self._finish_job(self.search_job, release, error=message, message="")
            finally:
                release()

        self.search_job.thread = threading.Thread(target=run, daemon=True, name="x-search")
        self.search_job.thread.start()
        return True

    def cancel_search(self) -> bool:
        return self.search_job.cancel()

    def remember_link(self, post_url: Any, label: str = "Links") -> str:
        """Nimmt einen eingefügten X-Link in die Ablage auf.

        Bisher liessen sich nur Beiträge herunterladen, die vorher in einem
        Suchergebnis standen. Alles, was die Suche nicht erfasst – etwa ein
        Video, das dir jemand geschickt hat –, war damit unerreichbar.
        """
        url = normalize_post_url(post_url)
        post_id = extract_post_id(url)
        vorhanden = self.db.get_x_post(post_id)
        if vorhanden is None:
            self.db.upsert_x_post(
                {
                    "post_id": post_id,
                    "username": urlsplit(url).path.split("/")[1],
                    "author_name": "",
                    "post_url": url,
                    "search_query": safe_path_component(label, fallback="Links"),
                    "caption": "",
                    "preview_url": "",
                    "published_at": "",
                }
            )
        return post_id

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

        release = self._once(self._operation_lock.release)

        def run() -> None:
            cookie_file: Optional[Path] = None

            def _drop_cookie_file() -> None:
                if cookie_file is not None:
                    try:
                        cookie_file.unlink(missing_ok=True)
                    except OSError:
                        pass

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
                # Cookie-Datei VOR der Freigabe entfernen: Danach darf sofort
                # der nächste Auftrag starten und eine neue schreiben.
                _drop_cookie_file()
                self._finish_job(
                    self.download_job,
                    release,
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
                _drop_cookie_file()
                self._finish_job(self.download_job, release, error=message, message="")
            finally:
                _drop_cookie_file()
                release()

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
        source: str = SOURCE_SEARCH,
    ) -> list[dict[str, Any]]:
        if self._browser_search_override is not None:
            return self._browser_search_override(
                query=query,
                limit=limit,
                sort=sort,
                exclude_reposts=exclude_reposts,
                cancel_event=cancel_event,
                source=source,
            )
        return self._browser_call(
            "search",
            {
                "query": query,
                "limit": limit,
                "sort": sort,
                "exclude_reposts": exclude_reposts,
                "cancel_event": cancel_event,
                "source": source,
            },
            timeout=MAX_SEARCH_SECONDS + 45,
        )

    def _ensure_browser(self) -> "queue.Queue[_Command | None]":
        """Sorgt für eine lebende Sitzung und liefert deren Auftragsschlange.

        Es genügt nicht, den Thread auf ``is_alive()`` zu prüfen: Nach einem
        zugeklickten Fenster lebt er noch kurz, während er sein ``finally``
        abarbeitet. Deshalb zählt zusätzlich ``_browser_open``, und der alte
        Thread wird abgewartet, bevor ein neuer startet – sonst liefen zwei
        ``launch_persistent_context`` auf dasselbe Profilverzeichnis.
        """
        with self._browser_lock:
            if (
                self._browser_thread is not None
                and self._browser_thread.is_alive()
                and self._browser_open
            ):
                return self._commands
            old = self._browser_thread
            if old is not None and old.is_alive():
                old.join(10)
            self._browser_ready.clear()
            self._browser_start_error = ""
            # Frische Schlange je Sitzung: Ein Auftrag kann damit strukturell
            # nicht mehr aus der alten in die neue Sitzung durchrutschen.
            commands: "queue.Queue[_Command | None]" = queue.Queue()
            self._commands = commands
            thread = threading.Thread(
                target=self._browser_loop, args=(commands,), daemon=True, name="x-browser"
            )
            try:
                thread.start()
            except RuntimeError as exc:
                # Ohne diesen Zweig bliebe _browser_ready ungesetzt und jeder
                # weitere Aufruf liefe 45 Sekunden ins Leere.
                self._browser_thread = None
                self._browser_ready.set()
                raise XUnavailableError(public_error(exc)) from None
            self._browser_thread = thread
        if not self._browser_ready.wait(45):
            raise XUnavailableError("Chromium konnte nicht innerhalb von 45 Sekunden gestartet werden.")
        if self._browser_start_error:
            raise XUnavailableError(self._browser_start_error)
        return commands

    def _browser_call(
        self, name: str, payload: Optional[dict[str, Any]] = None, timeout: float = 30
    ) -> Any:
        commands = self._ensure_browser()
        command = _Command(name=name, payload=payload or {})
        commands.put(command)
        try:
            if not command.done.wait(timeout):
                # Aufgeben, damit der Auftrag später nicht doch noch losläuft.
                command.abandon()
                raise XUnavailableError("Die sichtbare X-Browser-Sitzung antwortet nicht.")
            if command.error is not None:
                raise command.error
            return command.result
        finally:
            # Der Auftrag kann Zugangsdaten enthalten – sie dürfen nicht länger
            # als nötig im Speicher stehen bleiben.
            command.payload.clear()

    def _browser_loop(self, commands: "queue.Queue[_Command | None]") -> None:
        context = None
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise XUnavailableError(
                    "Playwright fehlt. Bitte `pip install -r requirements.txt` ausführen."
                ) from exc
            self.session_dir.mkdir(parents=True, exist_ok=True)
            try:
                write_no_password_manager_prefs(self.session_dir)
            except OSError:
                logger.debug(
                    "Chromium-Profileinstellungen konnten nicht geschrieben werden.",
                    exc_info=True,
                )
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(self.session_dir),
                        headless=False,
                        viewport=None,
                        args=list(_NO_PASSWORD_MANAGER_ARGS),
                    )
                except Exception as exc:
                    raise XUnavailableError(describe_launch_failure(exc)) from exc
                context.set_default_timeout(10_000)
                # Per Token hinterlegte Sitzung ins Fenster übernehmen, damit
                # keine Anmeldemaske erscheint.
                manual = self._manual_cookies()
                if manual:
                    try:
                        context.add_cookies(manual)
                    except Exception:
                        logger.debug("Token-Sitzung nicht übernehmbar.", exc_info=True)
                page = context.pages[0] if context.pages else context.new_page()
                # Schließt der Nutzer das Fenster, wacht die Schleife auf und
                # beendet sich, statt weiter „Browser geöffnet“ zu melden.
                context.on("close", self._on_browser_closed)
                self._browser_open = True
                self._browser_ready.set()
                self._serve_commands(context, page, commands)
        except Exception as exc:
            self._browser_start_error = self._map_browser_error(exc)
            logger.warning("X-Browser nicht verfügbar: %s", self._browser_start_error)
        finally:
            self._browser_open = False
            self._session_confirmed = False
            self._browser_ready.set()
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            while True:
                try:
                    pending = commands.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.error = XUnavailableError(
                        self._browser_start_error or "Die X-Browser-Sitzung wurde geschlossen."
                    )
                    pending.payload.clear()
                    pending.done.set()

    def _serve_commands(
        self, context: Any, page: Any, commands: "queue.Queue[_Command | None]"
    ) -> None:
        """Arbeitet Browseraufträge ab und hält dabei die Ereignisse am Laufen.

        Der Takt ist nötig, weil Playwrights Sync-API Ereignisse ausschließlich
        zustellt, während der Thread in einem Playwright-Aufruf steckt. Würde
        hier ohne Zeitgrenze auf die Schlange gewartet, bliebe ein zugeklicktes
        Fenster im Leerlauf unbemerkt – die Oberfläche meldete weiter „Browser
        geöffnet“ und der nächste Klick liefe auf eine tote Seite.
        """
        while True:
            try:
                command = commands.get(timeout=_IDLE_TICK_SECONDS)
            except queue.Empty:
                page = self._pump_events(context, page)
                if page is None:
                    break
                continue
            if command is None:
                break
            if not command.claim():
                continue  # Aufrufer hat aufgegeben – nicht mehr ausführen.
            try:
                if command.name == "open_login":
                    page.goto(_LOGIN_URL, wait_until="commit", timeout=60_000)
                    page.bring_to_front()
                    command.result = {"url": _LOGIN_URL}
                elif command.name == "login":
                    command.result = self._login_on_page(page, **command.payload)
                elif command.name == "search":
                    command.result = self._search_visible_page(page, **command.payload)
                elif command.name == "set_cookies":
                    context.add_cookies(command.payload.get("cookies") or [])
                    command.result = {"ok": True}
                elif command.name == "cookies":
                    command.result = context.cookies(["https://x.com", "https://twitter.com"])
                else:
                    raise XServiceError("Unbekannter Browserauftrag.")
            except Exception as exc:
                command.error = exc
                if is_browser_closed_error(public_error(exc)):
                    self._browser_open = False
            finally:
                command.payload.clear()
                command.done.set()
            if not self._browser_open:
                break

    def _pump_events(self, context: Any, page: Any) -> Any:
        """Stellt anstehende Playwright-Ereignisse zu. None = Sitzung ist tot.

        ``page.wait_for_timeout`` ist bewusst gewählt: Es ist ein echter, den
        Ereignisverteiler antreibender Aufruf, löst auf der Treiberseite aber
        nur ein ``setTimeout`` aus – kein CDP-Verkehr, kein Renderer. Ein Ping
        über ``context.pages`` oder ``is_closed()`` sähe zwar nach einer Lösung
        aus, treibt den Verteiler aber gar nicht an und würde nichts bemerken.
        """
        try:
            page.wait_for_timeout(0)
        except Exception as exc:
            if not is_browser_closed_error(public_error(exc)):
                return page  # z. B. ein Netzwerkfehler – die Sitzung lebt.
            if self._context_closed(context):
                return None
            # Nur der Tab war zu: weitermachen statt die Sitzung aufzugeben.
            try:
                return context.pages[0] if context.pages else context.new_page()
            except Exception:
                return None
        if self._context_closed(context):
            return None
        return page

    @staticmethod
    def _context_closed(context: Any) -> bool:
        """True, wenn der Browserkontext keine Seite mehr hat oder zu ist."""
        try:
            if getattr(context, "is_closed", None) is not None and context.is_closed():
                return True
            return not context.pages
        except Exception:
            return True

    def _on_browser_closed(self, *_args: Any) -> None:
        """Playwright meldet ein geschlossenes Fenster – Schleife aufwecken."""
        self._browser_open = False
        self._session_confirmed = False
        self._commands.put(None)

    @staticmethod
    def _visible(locator: Any, timeout: float) -> bool:
        try:
            locator.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    def _already_signed_in(self, page: Any) -> bool:
        """True, sobald X den Anmeldeablauf mit einer echten Seite verlassen hat.

        Die Prüfung ist bewusst positiv: Früher galt alles als angemeldet, was
        nur nicht nach Anmeldeseite aussah – auch ``chrome-error://`` nach einem
        Navigationsfehler und ``https://x.com/`` selbst, also ausgerechnet die
        AUSGELOGGTE Startseite, auf die X nach abgelehnten Zugangsdaten
        zurückwirft.
        """
        try:
            url = str(page.url or "")
        except Exception:
            return False
        if not url:
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        if parsed.scheme != "https":
            return False
        if (parsed.hostname or "").lower().rstrip(".") not in _X_HOSTS:
            return False
        if parsed.path in ("", "/"):
            return False
        if any(path in url for path in _NOT_SIGNED_IN_PATHS):
            return False
        self._session_confirmed = True
        return True

    @staticmethod
    def _page_text(page: Any) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=2_000) or "").lower()
        except Exception:
            return ""

    @classmethod
    def _advance(cls, page: Any, field: Any, labels: tuple[str, ...]) -> None:
        """Press X's own step button; fall back to Enter inside the field."""
        for label in labels:
            button = page.locator(
                f'button:has-text("{label}"), div[role="button"]:has-text("{label}")'
            ).first
            if cls._visible(button, 4_000):
                try:
                    button.click(timeout=6_000)
                    return
                except Exception:
                    continue
        field.press("Enter")

    def _login_on_page(
        self,
        page: Any,
        identifier: str,
        password: str,
        username: str = "",
        *,
        settle_seconds: float = 25.0,
    ) -> dict[str, Any]:
        """Fill X's login form. 2FA/CAPTCHA stay in the visible window."""
        page.goto(_LOGIN_URL, wait_until="commit", timeout=60_000)
        page.bring_to_front()

        identifier_field = page.locator(
            'input[autocomplete="username"], input[name="text"]'
        ).first
        if not self._visible(identifier_field, 12_000):
            # X leitet bereits angemeldete Profile direkt weiter – das ist
            # kein Fehler, sondern der Idealfall.
            if self._already_signed_in(page):
                return {"state": "signed_in"}
            if not self._visible(identifier_field, 18_000):
                if self._already_signed_in(page):
                    return {"state": "signed_in"}
                raise XLoginError(
                    "Die X-Anmeldemaske ist nicht erschienen. Bitte im geöffneten Fenster prüfen "
                    "(eventuell wartet dort eine Sicherheitsabfrage)."
                )
        identifier_field.fill(identifier)
        self._advance(page, identifier_field, _NEXT_LABELS)

        password_field = page.locator(
            'input[name="password"], input[type="password"]'
        ).first
        if not self._visible(password_field, 8_000):
            # X fragt bei ungewöhnlicher Aktivität zusätzlich den @Namen ab.
            extra_field = page.locator('input[name="text"]').first
            if self._visible(extra_field, 4_000):
                if not username:
                    return {"state": "verification"}
                extra_field.fill(username)
                self._advance(page, extra_field, _NEXT_LABELS)
            if not self._visible(password_field, 20_000):
                if self._already_signed_in(page):
                    return {"state": "signed_in"}
                text = self._page_text(page)
                if any(hint in text for hint in _LOGIN_FAILURE_HINTS):
                    raise XLoginError(
                        "X hat die Kennung nicht akzeptiert. Bitte E-Mail-Adresse bzw. "
                        "Benutzernamen prüfen."
                    )
                return {"state": "verification"}

        password_field.fill(password)
        self._advance(page, password_field, _SUBMIT_LABELS)
        return self._login_outcome(page, settle_seconds)

    def _login_outcome(self, page: Any, settle_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, settle_seconds)
        while True:
            try:
                page.url  # noqa: B018  – löst bei geschlossenem Fenster aus
            except Exception as exc:
                if is_browser_closed_error(exc):
                    raise XUnavailableError(
                        "Das X-Fenster wurde geschlossen, bevor die Anmeldung fertig war."
                    ) from None
                raise
            if self._already_signed_in(page):
                return {"state": "signed_in"}
            text = self._page_text(page)
            if any(hint in text for hint in _LOGIN_FAILURE_HINTS):
                raise XLoginError(
                    "X hat die Zugangsdaten abgelehnt. Bitte Kennung und Passwort prüfen."
                )
            if any(hint in text for hint in _VERIFICATION_HINTS):
                return {"state": "verification"}
            if time.monotonic() >= deadline:
                return {"state": "pending"}
            time.sleep(1.0)

    def _search_visible_page(
        self,
        page: Any,
        query: str,
        limit: int,
        sort: str,
        exclude_reposts: bool,
        cancel_event: threading.Event,
        source: str = SOURCE_SEARCH,
    ) -> list[dict[str, Any]]:
        if source == SOURCE_SEARCH:
            target = build_search_url(query, sort, exclude_reposts)
        else:
            target = self._collection_url(page, source)
        page.goto(target, wait_until="commit", timeout=60_000)
        if "/login" in page.url or "/i/flow/login" in page.url:
            self._session_confirmed = False
            raise XServiceError(
                "Die X-Sitzung ist nicht angemeldet oder abgelaufen. Bitte oben anmelden."
            )
        try:
            page.locator("article").first.wait_for(state="visible", timeout=20_000)
        except Exception as exc:
            body = self._page_text(page)
            if "/login" in page.url or any(
                word in body for word in ("log in", "sign in", "anmelden")
            ):
                self._session_confirmed = False
                raise XServiceError(
                    "Die X-Sitzung ist abgelaufen. Bitte oben erneut bei X anmelden."
                ) from exc
            if source != SOURCE_SEARCH:
                # Eine leere Liste ist bei Likes und Lesezeichen ein normales
                # Ergebnis und kein Fehler: Dann steht dort schlicht nichts
                # (oder nichts mit Video). Die Suche dagegen sollte immer
                # Karten liefern – dort bleibt es eine Fehlermeldung.
                self._session_confirmed = True
                logger.info("%s: keine Beitragskarten sichtbar.", source_label(source))
                return []
            raise XServiceError(
                "X zeigt keine Beitragskarten. Möglicherweise wurde das Layout geändert oder eine Sicherheitsabfrage wartet im Browser."
            ) from exc

        self._session_confirmed = True
        results: dict[str, dict[str, Any]] = {}
        started = time.monotonic()
        stagnant_rounds = 0
        scrolled = -1.0
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
            # „Keine neuen Treffer“ allein heißt nicht, dass die Seite zu Ende
            # ist: In Lesezeichen und Likes stehen auch Beiträge ohne Video,
            # und davon können etliche Bildschirmhöhen hintereinander liegen.
            # Erst wenn die Seite sich auch nicht mehr weiterschieben lässt,
            # ist wirklich Schluss.
            moved = self._scroll_position(page) > scrolled
            stagnant_rounds = 0 if len(results) != before or moved else stagnant_rounds + 1
            if stagnant_rounds >= 3 or round_number >= MAX_SCROLL_ROUNDS:
                break
            scrolled = self._scroll_position(page)
            page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.85, 700))")
            if cancel_event.wait(1.5):
                break
        return list(results.values())[:limit]

    @staticmethod
    def _scroll_position(page: Any) -> float:
        """Aktuelle Scrollhöhe – 0.0, wenn die Seite sie nicht verrät."""
        try:
            return float(page.evaluate("window.scrollY") or 0)
        except Exception:
            return 0.0

    def _collection_url(self, page: Any, source: str) -> str:
        """Adresse der eigenen Liste (Lesezeichen oder Likes).

        Lesezeichen haben eine feste Adresse. Die Likes-Seite hängt dagegen
        am eigenen Benutzernamen, den X nirgends über eine feste Adresse
        anbietet – er wird deshalb aus der Navigationsleiste der
        angemeldeten Sitzung gelesen.
        """
        if source == SOURCE_BOOKMARKS:
            return _BOOKMARKS_URL
        username = self._own_username(page)
        if not username:
            raise XServiceError(
                "Der eigene X-Benutzername konnte nicht gelesen werden. Bitte prüfen, "
                "ob die Sitzung im X-Fenster noch angemeldet ist."
            )
        return f"https://x.com/{username}/likes"

    def _own_username(self, page: Any) -> str:
        """Liest den eigenen @Namen aus der angemeldeten Oberfläche."""
        if not page.url.startswith(("https://x.com/", "https://twitter.com/")):
            page.goto(_HOME_URL, wait_until="commit", timeout=60_000)
        try:
            link = page.locator('a[data-testid="AppTabBar_Profile_Link"]').first
            link.wait_for(state="attached", timeout=15_000)
            candidate = (link.get_attribute("href") or "").strip("/").split("/")[0]
            if _USERNAME_RE.fullmatch(candidate):
                return candidate
        except Exception:
            logger.debug("Profil-Link in der X-Navigation nicht lesbar.", exc_info=True)
        # Schmales Fenster: Die Navigationsleiste fehlt, der Kontoschalter
        # unten links trägt den @Namen aber weiterhin als Text.
        try:
            switcher = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first
            if switcher.count():
                for line in switcher.inner_text().splitlines():
                    handle = line.strip().lstrip("@")
                    if _USERNAME_RE.fullmatch(handle):
                        return handle
        except Exception:
            logger.debug("Kontoschalter in der X-Navigation nicht lesbar.", exc_info=True)
        return ""

    # Woran eine Beitragskarte als Video erkennbar ist, auch wenn X den
    # Player (noch) nicht geladen hat. Genau das war der Grund, warum
    # manche Videos in Suche und Lesezeichen gar nicht erst auftauchten:
    # Bei sensiblen Inhalten zeigt X zuerst eine Warnkachel und bindet das
    # <video>-Element überhaupt nicht ein.
    _VIDEO_MARKERS = (
        '[data-testid="videoPlayer"]',
        '[data-testid="videoComponent"]',
        '[data-testid="previewInterstitial"]',
        '[data-testid="playButton"]',
        'a[href*="/video/"]',
    )

    def _looks_like_video(self, article: Any) -> bool:
        for selector in self._VIDEO_MARKERS:
            try:
                if article.locator(selector).count():
                    return True
            except Exception:
                continue
        return False

    def _article_data(self, article: Any, query: str) -> Optional[dict[str, Any]]:
        try:
            videos = article.locator("video")
            hat_video = videos.count() > 0
            if not hat_video and not self._looks_like_video(article):
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
            preview_url = ""
            if hat_video:
                preview_url = _safe_preview_url(videos.first.get_attribute("poster"))
            if not preview_url:
                # Ohne geladenen Player (z. B. Warnkachel bei sensiblen
                # Inhalten) gibt es kein poster-Attribut – dann das Bild der
                # Karte nehmen, damit die Vorschau nicht leer bleibt.
                bilder = article.locator('img[src*="twimg.com"]')
                if bilder.count():
                    preview_url = _safe_preview_url(bilder.first.get_attribute("src"))
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

    def _note_missing_cookies(self, reason: str) -> None:
        """Hinweis in den Auftragszustand schreiben, statt zu schweigen."""
        self.download_job.update(
            message=f"{reason} – der Download läuft ohne Anmeldung und kann fehlschlagen."
        )

    def _export_cookie_file(self) -> Optional[Path]:
        """Cookies für yt-dlp ablegen. None heißt: ohne Anmeldung laden.

        Der Fehlschlag wird sichtbar gemacht. Vorher lief der Download in
        diesem Fall stillschweigend ohne Anmeldung weiter und scheiterte an
        jedem Beitrag mit einer irreführenden yt-dlp-Meldung.
        """
        if self._browser_open:
            try:
                cookies = self._browser_call("cookies", timeout=15)
            except Exception as exc:
                logger.warning("X-Cookies konnten nicht gelesen werden: %s", public_error(exc))
                self._note_missing_cookies("Die X-Cookies konnten nicht gelesen werden")
                return None
            self._refresh_manual_session(cookies or [])
        else:
            # Ohne offenes Fenster: die per Token hinterlegte Sitzung nutzen.
            # Damit laufen Downloads ganz ohne Browser – wichtig auf einem
            # Rechner ohne Bildschirm.
            cookies = self._manual_cookies()
            if not cookies:
                self._note_missing_cookies("Die X-Sitzung ist nicht geöffnet")
                return None
        if not cookies:
            self._note_missing_cookies("Die X-Sitzung hat keine Cookies geliefert")
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
        source_dir = self.settings.download_dir / "X" / query_dir
        # Je Urheber ein eigener Ordner, innerhalb der Quelle (Suchbegriff,
        # „Likes“ oder „Lesezeichen“). So liegen die Videos einer Person
        # beieinander, ohne dass der Zusammenhang verloren geht.
        videos_dir = source_dir / safe_username_component(username) / "Videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        # Frühere Versionen legten alles flach unter <Quelle>/Videos ab. Der
        # Altbestand wird mitgesucht, sonst gälte er als „noch nicht geladen“
        # und würde ein zweites Mal heruntergeladen.
        legacy_dir = source_dir / "Videos"
        existing = sorted(
            path
            for folder in (videos_dir, legacy_dir)
            for path in folder.glob(f"{post_id}_*")
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
        ffmpeg = ffmpeg_path()

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
            # Liegt FFmpeg nicht im PATH, sondern im Python-Paket, muss
            # yt-dlp den Pfad ausdruecklich bekommen.
            options["ffmpeg_location"] = ffmpeg
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
        # Ein zugeklicktes Fenster ist KEIN Installationsproblem. Diese
        # Unterscheidung muss vor dem Chromium-Hinweis stehen, sonst wird der
        # Nutzer zur Neuinstallation aufgefordert, weil er ein Fenster
        # geschlossen hat.
        if is_browser_closed_error(message):
            return (
                "Das X-Fenster wurde geschlossen. Beim nächsten Versuch öffnet es "
                "sich automatisch neu."
            )
        if "executable" in lower:
            return (
                "Playwright-Chromium fehlt oder startet nicht. "
                + chromium_install_hint()
                + " Danach die Sitzung neu öffnen."
            )
        return f"X-Browserfehler: {message}"

    @staticmethod
    def _map_login_error(exc: BaseException, secret: str = "") -> str:
        """Turn any login failure into a message that cannot contain the password."""
        if isinstance(exc, XServiceError):
            return redact_secret(public_error(exc), secret)
        message = redact_secret(public_error(exc), secret)
        if is_browser_closed_error(message):
            return (
                "Das X-Fenster wurde geschlossen. Bitte „X-Browser öffnen“ klicken und die "
                "Anmeldung erneut starten."
            )
        if "timeout" in message.lower():
            return (
                "Die X-Anmeldemaske hat nicht rechtzeitig reagiert. Bitte im geöffneten Fenster "
                "nachsehen – dort wartet eventuell eine Sicherheitsabfrage."
            )
        return f"Die Anmeldung bei X ist fehlgeschlagen: {message}"

    @staticmethod
    def _map_download_error(exc: BaseException) -> str:
        if isinstance(exc, _DownloadCancelled):
            return "Download abgebrochen."
        message = public_error(exc)
        lower = message.lower()
        if "ffmpeg" in lower:
            return (
                "FFmpeg wird für diese Video-/Audio-Kombination benötigt. Bitte FFmpeg installieren "
                "und die Anwendung neu starten."
            )
        if "no video could be found" in lower:
            return (
                "X gibt für diesen Beitrag kein Video heraus. Meist einer von drei "
                "Gründen: Der Beitrag enthält nur Fotos oder ein GIF; das Video "
                "steckt im ZITIERTEN Beitrag (dann dessen Link verwenden); oder er "
                "ist als sensibel eingestuft und die Anmeldung fehlt bzw. ist "
                "abgelaufen – in dem Fall oben die X-Sitzung erneuern."
            )
        if "nsfw" in lower or "age" in lower and "restrict" in lower:
            return (
                "Der Beitrag ist altersbeschränkt. Dafür braucht der Download eine "
                "gültige X-Anmeldung – bitte oben die Sitzung erneuern."
            )
        if "protected" in lower or "not authorized" in lower or "401" in lower:
            return (
                "Kein Zugriff auf diesen Beitrag. Entweder ist das Konto geschützt "
                "oder die X-Anmeldung ist abgelaufen."
            )
        if "does not exist" in lower or "404" in lower:
            return "Den Beitrag gibt es nicht mehr – gelöscht oder das Konto ist weg."
        if "rate limit" in lower or "429" in lower:
            return (
                "X hat vorübergehend gebremst (zu viele Anfragen). Bitte etwas "
                "warten und erneut versuchen."
            )
        return message
