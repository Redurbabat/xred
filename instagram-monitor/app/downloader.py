"""
Zugriff auf öffentliche Instagram-Inhalte.
==========================================

Dieses Modul kapselt sämtliche Instagram-Zugriffe hinter der Klasse
:class:`InstagramDownloader`. Als Unterbau dient **Instaloader**
(https://instaloader.github.io/), ein etabliertes, dokumentiertes
Open-Source-Werkzeug.

Wichtige Grundsätze dieser Implementierung
------------------------------------------
* Standardmäßig werden nur **öffentlich zugängliche** Inhalte abgerufen –
  genau das, was auch ein nicht angemeldeter Besucher im Browser sähe.
* **Optionaler Login mit dem EIGENEN Konto:** Über die dokumentierte
  Instaloader-Anmeldung kann sich der Benutzer mit seinen eigenen
  Zugangsdaten einloggen. Das reduziert die Drosselung und erlaubt den
  Zugriff auf Konten, denen der Benutzer folgt (inkl. privater Konten, zu
  denen eine legitime Berechtigung besteht). Es werden dabei **keine**
  Schutzmechanismen umgangen – der Login ist der offizielle, von
  Instagram vorgesehene Weg. Die Zugangsdaten werden nicht im Klartext
  gespeichert; Instaloader legt lediglich eine Session-Datei (Cookies) an.
* **Private Profile ohne Berechtigung werden übersprungen.** Folgt der
  angemeldete Benutzer dem privaten Konto nicht, wird nicht darauf
  zugegriffen.
* **Rate-Limits werden respektiert:** Instaloader bringt eine eigene
  Ratenbegrenzung mit; zusätzlich legt der Monitor Pausen zwischen den
  Konten ein und prüft nur in dem vom Benutzer konfigurierten Intervall.
* Antwortet Instagram mit einer Sperre/Drosselung (HTTP 429 u. ä.), wird
  der Fehler protokolliert und der nächste reguläre Prüflauf abgewartet –
  es gibt keine aggressiven Wiederholungsversuche.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

# Wie lange geladene Beiträge/Profil-Infos zwischengespeichert werden
# (Sekunden). Beschleunigt Navigation deutlich und reduziert Anfragen
# an Instagram (weniger Drosselung).
_CACHE_TTL = 300

# Mindestabstand zwischen zwei Instagram-Zugriffen (Sekunden). Gilt GLOBAL
# über alle Threads (Monitor, Web-Oberfläche, Listen-Auffrischung) – dazu
# kommt ein zufälliger Aufschlag (Jitter), damit die Abrufe nicht im exakt
# gleichen Takt erfolgen.
_MIN_REQUEST_GAP = 1.2

# Obergrenze für das exponentielle Zurückweichen nach einer Drosselung.
_BACKOFF_MAX_SECONDS = 1800.0  # 30 Minuten
_BACKOFF_START_SECONDS = 120.0  # erste Wartezeit: 2 Minuten

# Medien werden nach Typ in Unterordner einsortiert: <konto>/Fotos bzw.
# <konto>/Videos. So ist die Ablage auf allen Geräten identisch und lässt
# sich sauber in eine Cloud (z. B. TeraBox) synchronisieren.
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
_PHOTO_SUBDIR = "Fotos"
_VIDEO_SUBDIR = "Videos"
# Stories und Highlights bekommen je Konto EIGENE Ordner (nicht nach Typ in
# Fotos/Videos), damit sie klar getrennt sind:
#   <konto>/Story/       – alle Stories (Bilder + Videos)
#   <konto>/Highlights/  – alle Highlights (öne çıkanlar; Bilder + Videos)
_STORY_SUBDIR = "Story"
_HIGHLIGHT_SUBDIR = "Highlights"

import instaloader
import requests as _requests
from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    InstaloaderException,
    LoginRequiredException,
    ProfileNotExistsException,
    TwoFactorAuthRequiredException,
)

# AbortDownloadException ist in Instaloader KEINE Unterklasse von
# InstaloaderException (sie erbt direkt von Exception). Instagram wirft sie
# u. a. bei „checkpoint_required“ – deshalb muss sie überall dort, wo
# Instaloader-Fehler behandelt werden, EIGENS mit abgefangen werden, sonst
# schlägt sie als „unerwarteter Fehler“ durch. Defensiv importiert, falls
# ältere Instaloader-Versionen sie nicht kennen.
try:
    from instaloader.exceptions import AbortDownloadException
except ImportError:  # pragma: no cover - versionsabhängig
    class AbortDownloadException(Exception):  # type: ignore
        """Platzhalter, falls die installierte Instaloader-Version sie nicht kennt."""

# PrivateProfileNotFollowedException gibt es erst in neueren Instaloader-
# Versionen; fällt zurück auf die Basisklasse, wenn nicht vorhanden.
try:
    from instaloader.exceptions import PrivateProfileNotFollowedException
except ImportError:  # pragma: no cover - versionsabhängig
    PrivateProfileNotFollowedException = InstaloaderException  # type: ignore

if TYPE_CHECKING:  # nur für Typprüfung, vermeidet Importzyklen zur Laufzeit
    from .settings import Settings

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Eigene, sprechende Ausnahmen – die restliche Anwendung muss dadurch
# keine Instaloader-Interna kennen.
# ----------------------------------------------------------------------
class DownloaderError(Exception):
    """Basisklasse für alle Fehler dieses Moduls."""


class ProfileNotFound(DownloaderError):
    """Das angegebene Profil existiert nicht."""


class ProfileIsPrivate(DownloaderError):
    """Das Profil ist privat – ohne Berechtigung kein Zugriff (gewollt)."""


class TemporaryError(DownloaderError):
    """Vorübergehender Fehler (Netzwerk, Drosselung) – später erneut versuchen."""


class LoginError(DownloaderError):
    """Die Anmeldung ist fehlgeschlagen (falsche Daten, abgebrochen …)."""


class TwoFactorRequired(DownloaderError):
    """Signalisiert intern, dass ein Zwei-Faktor-Code benötigt wird."""


# Hinweistext, der bei einem Instagram-Sicherheits-Checkpoint angezeigt wird.
_CHECKPOINT_HINT = (
    "Instagram verlangt eine zusätzliche Sicherheitsbestätigung "
    "(Checkpoint), weil die Anmeldung von einem neuen Gerät kommt. Das ist "
    "gewollt und wird nicht umgangen.\n\n"
    "Einfachste Lösung: Melde dich in deinem normalen Browser bei "
    "instagram.com an (und bestätige dort ggf. die Sicherheitsabfrage) und "
    "klicke hier auf »Aus Browser übernehmen«. Dann wird deine bereits "
    "bestätigte Anmeldung verwendet – ohne erneuten Checkpoint."
)


def _is_checkpoint(exc: Exception) -> bool:
    """Erkennt eine Checkpoint-/Challenge-Anforderung an der Fehlermeldung."""
    text = str(exc).lower()
    return "checkpoint" in text or "challenge" in text


@dataclass(frozen=True)
class PostInfo:
    """Leichtgewichtige, GUI-freundliche Beschreibung eines Beitrags."""

    shortcode: str          # eindeutige Kurz-ID (Teil der Beitrags-URL)
    username: str           # Konto, zu dem der Beitrag gehört
    post_type: str          # "Reel", "Video", "Album" oder "Bild"
    posted_at: str          # Veröffentlichungszeitpunkt (ISO, UTC)
    url: str                # Link zum Beitrag

    # Für die Web-Oberfläche (best effort, kann leer sein):
    thumbnail_url: str = ""  # Vorschaubild-URL (Instagram-CDN)
    caption: str = ""        # Anfang der Bildunterschrift
    likes: int = 0           # Anzahl Likes (0 wenn unbekannt)
    comments: int = 0        # Anzahl Kommentare (0 wenn unbekannt)

    # Das originale Instaloader-Post-Objekt (nur intern; spart beim
    # Download eine erneute Netzanfrage). Von Vergleich/Repr ausgenommen.
    raw: Optional[object] = field(default=None, repr=False, compare=False)

    @property
    def is_reel(self) -> bool:
        return self.post_type == "Reel"


def _classify_post(post: "instaloader.Post") -> str:
    """Bestimmt den Beitragstyp für Anzeige und Datenbank.

    Reel-Erkennung ist "best effort": Instaloader stellt keine eigene
    Property dafür bereit; das rohe API-Feld ``product_type == "clips"``
    kennzeichnet Reels, ist aber nicht in jedem Feed-Knoten enthalten.
    Videos, die nicht sicher zuzuordnen sind, werden ehrlich als
    "Video/Reel" bezeichnet – für Benachrichtigung und Download ist die
    Unterscheidung ohnehin unerheblich.
    """
    try:
        node = getattr(post, "_node", None) or {}
        if node.get("product_type") == "clips":  # rohes API-Feld, best effort
            return "Reel"
        if post.typename == "GraphSidecar":
            return "Album"
        if post.is_video:
            return "Video/Reel"
    except Exception:  # defensive: Metadaten-Zugriffe können nachladen & scheitern
        logger.debug("Beitragstyp konnte nicht bestimmt werden.", exc_info=True)
    return "Bild"


def _media_subdir(filename: str) -> str:
    """Unterordner nach Medientyp: Videos für Videodateien, sonst Fotos.

    Die Einordnung erfolgt anhand der Dateiendung (die Medien-URLs liefern
    ``.mp4``/``.jpg`` je Element), sodass auch gemischte Alben pro Datei
    korrekt aufgeteilt werden.
    """
    return _VIDEO_SUBDIR if Path(filename).suffix.lower() in _VIDEO_EXTS else _PHOTO_SUBDIR


class InstagramDownloader:
    """Prüft öffentliche Profile auf neue Beiträge und lädt sie herunter."""

    def __init__(
        self, settings: "Settings", session_dir: Optional[Path] = None
    ) -> None:
        self._settings = settings

        # Instaloader-Instanz: bewusst "leise" und sparsam konfiguriert –
        # nur die Medien selbst, keine Metadaten-/Kommentar-Dateien.
        self._loader = instaloader.Instaloader(
            quiet=True,                    # keine Ausgaben auf stdout (wir loggen selbst)
            download_video_thumbnails=False,
            save_metadata=False,           # keine .json.xz-Metadaten
            compress_json=False,
            post_metadata_txt_pattern="",  # keine .txt-Begleitdateien
        )

        # Alle Loader-Zugriffe werden serialisiert: Der Monitor-Thread und
        # eine (aus der GUI angestoßene) Anmeldung teilen sich dieselbe
        # Instaloader-Instanz; deren Requests-Session ist nicht für echt
        # parallele Nutzung ausgelegt.
        self._lock = threading.RLock()

        # Kurzlebiger Zwischenspeicher (username → (zeit, daten)) für
        # Beiträge und Profil-Infos; macht Navigation spürbar schneller.
        self._recent_cache: dict = {}
        self._profile_cache: dict = {}

        # Ordner für die Session-Datei (Cookies) des angemeldeten Kontos.
        self._session_dir = Path(session_dir) if session_dir else None
        if self._session_dir:
            self._session_dir.mkdir(parents=True, exist_ok=True)

        # Globale Anfragen-Bremse: Zeitpunkt des letzten Zugriffs und
        # Zustand des Zurückweichens nach einer Drosselung. Eigener Lock,
        # weil auch Aufrufer OHNE self._lock (z. B. Statusabfragen) den
        # Backoff-Zustand lesen können sollen.
        self._pace_lock = threading.Lock()
        self._last_request = 0.0        # time.monotonic() des letzten Zugriffs
        self._backoff_until = 0.0       # bis wann Abrufe pausieren (monotonic)
        self._backoff_seconds = 0.0     # aktuelle Backoff-Stufe (0 = keine)

        # Beim Start eine zuvor gespeicherte Anmeldung wiederherstellen,
        # damit man sich nicht bei jedem Programmstart neu einloggen muss.
        self._restore_session()

    # ------------------------------------------------------------------
    # Globale Anfragen-Bremse + Zurückweichen bei Drosselung
    # ------------------------------------------------------------------
    def _pace(self) -> None:
        """VOR jedem Instagram-Zugriff aufrufen.

        1. Läuft gerade ein Backoff (Instagram hat gedrosselt), wird der
           Zugriff sofort mit :class:`TemporaryError` abgelehnt – statt
           weiter zu hämmern und die Sperre zu verlängern.
        2. Sonst wird gewartet, bis seit dem letzten Zugriff mindestens
           ``_MIN_REQUEST_GAP`` (+ Jitter) vergangen ist.
        """
        with self._pace_lock:
            now = time.monotonic()
            if now < self._backoff_until:
                minutes = int((self._backoff_until - now) / 60) + 1
                raise TemporaryError(
                    "Instagram drosselt gerade – die Anwendung pausiert und "
                    f"versucht es in ca. {minutes} Minute(n) wieder."
                )
            gap = _MIN_REQUEST_GAP * (1.0 + random.random() * 0.5)
            wait = self._last_request + gap - now
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    @contextmanager
    def _paced_lock(self, pace: bool = True):
        """Wartet die globale Anfragen-Pause AUSSERHALB des Loader-Locks ab
        und hält den Lock danach nur für den eigentlichen (nicht mehr
        schlafenden) Zugriff.

        Bisher lief ``self._pace()`` – inklusive ``time.sleep`` – INNERHALB
        von ``self._lock``. Dadurch blockierte die 1,2-Sekunden-Mindestpause
        eines einzigen Zugriffs alle anderen Threads, die auf den Loader-Lock
        warteten (Feed-Aufbau, Vorschaubilder, Medien-Anzeige …). Jetzt
        schläft die Pause lockfrei; ``self._lock`` wird erst danach – und nur
        so kurz wie nötig – gehalten. ``self._pace`` ist über einen eigenen
        Lock weiterhin serialisiert, der Mindestabstand bleibt also erhalten.

        ``pace=False`` überspringt die Pause für rein lokale Zugriffe, die
        gar keine Instagram-Anfrage auslösen (z. B. bereits geladene Medien).
        """
        if pace:
            self._pace()
        with self._lock:
            yield

    def _register_throttle(self, exc: Exception) -> None:
        """Nach einem Verbindungsfehler aufrufen: erkennt Drosselung.

        Bei Hinweisen auf ein Rate-Limit (HTTP 429, "please wait" …) wird
        die Wartezeit jeweils verdoppelt (2 → 4 → 8 … Minuten, gedeckelt
        bei 30). Normale Netzwerkfehler lösen KEIN Backoff aus.
        """
        text = str(exc).lower()
        if not any(marker in text for marker in
                   ("429", "too many requests", "please wait a few minutes",
                    "rate limit", "temporarily blocked")):
            return
        with self._pace_lock:
            self._backoff_seconds = min(
                _BACKOFF_MAX_SECONDS,
                (self._backoff_seconds * 2) or _BACKOFF_START_SECONDS,
            )
            self._backoff_until = time.monotonic() + self._backoff_seconds
            minutes = self._backoff_seconds / 60
        logger.warning(
            "Instagram-Drosselung erkannt – alle Abrufe pausieren jetzt "
            "%.0f Minute(n).", minutes
        )

    def _register_success(self) -> None:
        """Nach einem erfolgreichen Zugriff: Backoff-Stufe zurücksetzen."""
        if self._backoff_seconds:
            with self._pace_lock:
                self._backoff_seconds = 0.0
                self._backoff_until = 0.0

    def backoff_status(self) -> dict:
        """Für die Oberfläche: Ist gerade eine Drossel-Pause aktiv?"""
        with self._pace_lock:
            now = time.monotonic()
            remaining = max(0.0, self._backoff_until - now)
            return {
                "active": remaining > 0,
                "remaining_seconds": int(remaining),
            }

    def _fetch_error(self, exc: Exception, what: str) -> DownloaderError:
        """Übersetzt einen Instagram-Fehler in eine sprechende App-Ausnahme.

        Erkennt insbesondere einen **Sicherheits-Checkpoint** von Instagram
        (``checkpoint_required``): Dann muss der Nutzer sich im Browser neu
        bestätigen – dafür gibt es eine klare Anleitung statt eines
        technischen Absturzes. Sonst: vorübergehender Fehler.
        """
        if _is_checkpoint(exc):
            return LoginError(_CHECKPOINT_HINT)
        self._register_throttle(exc)
        return TemporaryError(f"{what} derzeit nicht abrufbar: {exc}")

    # ------------------------------------------------------------------
    # Anmeldung (optional, mit dem EIGENEN Konto)
    # ------------------------------------------------------------------
    @property
    def is_logged_in(self) -> bool:
        """True, wenn eine gültige Anmeldung aktiv ist."""
        return bool(getattr(self._loader.context, "is_logged_in", False))

    @property
    def logged_in_user(self) -> Optional[str]:
        """Benutzername des angemeldeten Kontos (oder None)."""
        return getattr(self._loader.context, "username", None)

    def _session_file(self, username: str) -> Optional[Path]:
        if not self._session_dir:
            return None
        return self._session_dir / f"session-{username.lower()}"

    def _restore_session(self) -> None:
        """Lädt – falls vorhanden – die gespeicherte Session beim Start."""
        username = self._settings.login_username
        session_file = self._session_file(username) if username else None
        if not username or not session_file or not session_file.exists():
            return
        try:
            self._loader.load_session_from_file(username, str(session_file))
            logger.info("Gespeicherte Anmeldung für @%s wiederhergestellt.", username)
        except Exception:
            logger.warning(
                "Gespeicherte Anmeldung für @%s konnte nicht geladen werden "
                "– bitte ggf. neu anmelden.",
                username,
                exc_info=True,
            )

    def login(
        self,
        username: str,
        password: str,
        two_factor_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        """Meldet sich mit dem eigenen Instagram-Konto an (offizieller Weg).

        Parameter:
          username, password  – Zugangsdaten des EIGENEN Kontos
          two_factor_provider – optionaler Callback, der bei aktivierter
                                 Zwei-Faktor-Authentifizierung den Code
                                 liefert (oder None bei Abbruch).

        Bei Erfolg wird die Session als Cookie-Datei gespeichert, sodass
        beim nächsten Start keine erneute Anmeldung nötig ist. Das
        Passwort wird NICHT gespeichert.
        """
        username = username.strip().lstrip("@").lower()
        if not username or not password:
            raise LoginError("Benutzername und Passwort dürfen nicht leer sein.")

        with self._lock:
            try:
                self._loader.login(username, password)
            except TwoFactorAuthRequiredException as exc:
                code = two_factor_provider() if two_factor_provider else None
                if not code:
                    # Eigener Ausnahmetyp, damit Aufrufer (z. B. die
                    # Web-Oberfläche) gezielt ein Code-Feld anbieten können.
                    raise TwoFactorRequired(
                        "Zwei-Faktor-Code erforderlich – bitte den Code aus "
                        "deiner Authenticator-App/SMS eingeben."
                    ) from exc
                try:
                    self._loader.two_factor_login(code.strip())
                except InstaloaderException as exc2:
                    raise LoginError(
                        f"Zwei-Faktor-Anmeldung fehlgeschlagen: {exc2}"
                    ) from exc2
            except BadCredentialsException as exc:
                raise LoginError("Benutzername oder Passwort ist falsch.") from exc
            except ConnectionException as exc:
                if _is_checkpoint(exc):
                    raise LoginError(_CHECKPOINT_HINT) from exc
                raise LoginError(
                    f"Anmeldung derzeit nicht möglich (Verbindung/Drosselung): {exc}"
                ) from exc
            except InstaloaderException as exc:
                if _is_checkpoint(exc):
                    raise LoginError(_CHECKPOINT_HINT) from exc
                raise LoginError(f"Anmeldung fehlgeschlagen: {exc}") from exc

            # Erfolgreich – Session speichern und Benutzernamen merken.
            self._settings.login_username = username
            session_file = self._session_file(username)
            if session_file:
                try:
                    self._loader.save_session_to_file(str(session_file))
                except OSError:
                    logger.warning(
                        "Session-Datei konnte nicht gespeichert werden.",
                        exc_info=True,
                    )
        logger.info("Erfolgreich angemeldet als @%s.", username)

    # Browser, aus denen Cookies übernommen werden können (Reihenfolge =
    # Versuchsreihenfolge bei "automatisch").
    _SUPPORTED_BROWSERS = (
        "firefox",
        "chrome",
        "edge",
        "brave",
        "opera",
        "vivaldi",
        "chromium",
        "librewolf",
    )

    def _fresh_loader(self) -> "instaloader.Instaloader":
        """Erzeugt eine neue, gleich konfigurierte Instaloader-Instanz."""
        return instaloader.Instaloader(
            quiet=True,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
        )

    def _activate_session(
        self, loader: "instaloader.Instaloader", username: str, source: str
    ) -> str:
        """Übernimmt eine erfolgreich getestete Session als aktive Anmeldung."""
        loader.context.username = username
        self._loader = loader
        self._settings.login_username = username
        self._recent_cache.clear(); self._profile_cache.clear()
        session_file = self._session_file(username)
        if session_file:
            try:
                loader.save_session_to_file(str(session_file))
            except OSError:
                logger.warning(
                    "Session-Datei konnte nicht gespeichert werden.",
                    exc_info=True,
                )
        logger.info("Angemeldet als @%s (%s).", username, source)
        return username

    def import_browser_session(self, browser: Optional[str] = None) -> str:
        """Übernimmt die bestehende Instagram-Anmeldung aus dem Browser.

        Dies ist der von Instaloader empfohlene Weg, wenn die direkte
        Anmeldung mit Benutzername/Passwort einen Sicherheits-Checkpoint
        auslöst: Es wird KEINE neue Anmeldung durchgeführt, sondern die
        bereits im Browser vorhandene (von dir über die offizielle
        Instagram-Seite/-App bestätigte) Session weiterverwendet. Dazu
        werden ausschließlich die ``instagram.com``-Cookies gelesen.

        Parameter:
          browser – "firefox", "chrome", "edge" … oder None (=erst alle
                    gemeinsam, dann einzeln probieren).

        Rückgabe: der erkannte Benutzername.
        Wirft :class:`LoginError` mit einer Diagnose je Browser, wenn keine
        gültige Anmeldung gefunden wird.
        """
        try:
            import browser_cookie3
        except ImportError as exc:
            raise LoginError(
                "Das Modul 'browser_cookie3' fehlt. Bitte einmal "
                "'pip install browser_cookie3' ausführen (oder das "
                "Startskript erneut starten)."
            ) from exc

        with self._lock:
            # 1. Bequemster Weg: alle installierten Browser gemeinsam.
            if browser is None:
                try:
                    jar = browser_cookie3.load(domain_name="instagram.com")
                    loader = self._fresh_loader()
                    loader.context._session.cookies.update(jar)
                    username = loader.test_login()
                    if username:
                        return self._activate_session(
                            loader, username, "Browser-Cookies"
                        )
                except Exception:
                    logger.debug("Sammel-Import fehlgeschlagen.", exc_info=True)

            # 2. Einzeln – für eine aussagekräftige Diagnose je Browser.
            candidates = (
                [browser.lower()] if browser else list(self._SUPPORTED_BROWSERS)
            )
            report: list[str] = []
            for name in candidates:
                loader_fn = getattr(browser_cookie3, name, None)
                if loader_fn is None:
                    continue
                try:
                    jar = loader_fn(domain_name="instagram.com")
                except Exception as exc:
                    # Browser nicht installiert / Profil nicht gefunden /
                    # Cookies nicht entschlüsselbar → nur bei "installiert,
                    # aber unlesbar" für den Nutzer interessant.
                    text = str(exc).lower()
                    if "profile" in text or "not find" in text or "no " in text:
                        continue  # schlicht nicht installiert
                    report.append(f"{name}: nicht lesbar ({exc})")
                    continue

                loader = self._fresh_loader()
                loader.context._session.cookies.update(jar)
                try:
                    username = loader.test_login()
                except Exception as exc:
                    report.append(f"{name}: Prüfung fehlgeschlagen ({exc})")
                    continue
                if username:
                    return self._activate_session(loader, username, name)
                report.append(f"{name}: keine Instagram-Anmeldung")

        detail = "; ".join(report) if report else (
            "in keinem installierten Browser waren instagram.com-Cookies lesbar"
        )
        raise LoginError(
            "Es konnte keine aktive Instagram-Anmeldung im Browser gefunden "
            "werden.\n\nBitte melde dich zuerst in deinem Browser (am "
            "zuverlässigsten: Firefox) auf instagram.com an, schließe den "
            "Browser einmal komplett und versuche es erneut. Falls das nicht "
            "klappt, nutze die Anmeldung per sessionid (siehe „Erweitert“).\n\n"
            f"Geprüft: {detail}"
        )

    def login_with_sessionid(
        self, sessionid: str, username: Optional[str] = None
    ) -> str:
        """Meldet mit einem ``sessionid``-Cookie an (zuverlässiger Fallback).

        Den Wert findet man im Browser, wenn man dort bei instagram.com
        angemeldet ist (Entwicklertools → Anwendung/Storage → Cookies →
        instagram.com → ``sessionid``). Es wird KEINE neue Anmeldung
        ausgelöst und damit auch kein Checkpoint – die bestehende Session
        wird nur weiterverwendet.
        """
        sessionid = (sessionid or "").strip().strip('"')
        if not sessionid:
            raise LoginError("Bitte einen sessionid-Wert eingeben.")

        with self._lock:
            loader = self._fresh_loader()
            loader.context._session.cookies.set(
                "sessionid", sessionid, domain=".instagram.com"
            )
            try:
                detected = loader.test_login()
            except ConnectionException as exc:
                raise LoginError(
                    f"Anmeldung derzeit nicht möglich (Verbindung/Drosselung): {exc}"
                ) from exc
            except Exception as exc:
                raise LoginError(f"Anmeldung fehlgeschlagen: {exc}") from exc
            if not detected:
                raise LoginError(
                    "Die sessionid ist ungültig oder abgelaufen. Bitte im "
                    "Browser neu bei instagram.com anmelden und den Wert "
                    "erneut kopieren."
                )
            return self._activate_session(loader, detected, "sessionid")

    def logout(self) -> None:
        """Meldet ab und entfernt die gespeicherte Session."""
        username = self._settings.login_username
        with self._lock:
            session_file = self._session_file(username) if username else None
            if session_file and session_file.exists():
                try:
                    session_file.unlink()
                except OSError:
                    logger.debug("Session-Datei konnte nicht gelöscht werden.")
            # Neue, anonyme Loader-Instanz erzeugen.
            self._loader = instaloader.Instaloader(
                quiet=True,
                download_video_thumbnails=False,
                save_metadata=False,
                compress_json=False,
                post_metadata_txt_pattern="",
            )
        self._settings.login_username = ""
        self._recent_cache.clear(); self._profile_cache.clear()
        logger.info("Abgemeldet.")

    # ------------------------------------------------------------------
    # Profil-Zugriff
    # ------------------------------------------------------------------
    def _get_profile(self, username: str) -> "instaloader.Profile":
        """Löst einen Benutzernamen in ein Profil auf – mit Fallback.

        ``Profile.from_username`` basiert auf einer Instagram-**Suche**.
        Anonymen Clients liefert diese Suche häufig leere Ergebnisse, und
        Instaloader meldet dann fälschlich "Profile does not exist", obwohl
        das Profil existiert. Deshalb:

          1. regulärer Weg über ``Profile.from_username``
          2. bei "nicht gefunden": zweiter, unabhängiger Suchweg über die
             Top-Suche (``TopSearchResults``)
          3. Nur wenn eine Suche nachweislich Ergebnisse liefert und das
             Profil nicht dabei ist → :class:`ProfileNotFound`.
             Liefern beide Wege nichts, ist eine anonyme Drosselung am
             wahrscheinlichsten → :class:`TemporaryError` (wird beim
             nächsten Prüflauf automatisch erneut versucht).
        """
        with self._paced_lock():
            try:
                profile = instaloader.Profile.from_username(
                    self._loader.context, username
                )
                self._register_success()
                return profile
            except ProfileNotExistsException:
                logger.debug(
                    "@%s: reguläre Namensauflösung ohne Treffer – "
                    "Top-Suche wird als zweiter Weg versucht.",
                    username,
                )
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise

            search_had_results = False
            try:
                results = instaloader.TopSearchResults(
                    self._loader.context, username
                )
                for profile in results.get_profiles():
                    search_had_results = True
                    if profile.username.lower() == username.lower():
                        return profile
            except InstaloaderException:
                logger.debug("Top-Suche nicht verfügbar.", exc_info=True)

        if search_had_results:
            # Die Suche funktioniert und liefert andere Profile – der
            # gesuchte Name ist also wirklich nicht dabei.
            raise ProfileNotFound(
                f"Profil @{username} wurde in der Instagram-Suche nicht "
                "gefunden – bitte die Schreibweise prüfen."
            )
        raise TemporaryError(
            f"@{username}: Instagram beantwortet anonyme Profil-Abfragen "
            "derzeit nicht (übliche Drosselung) – oder das Profil existiert "
            "nicht. Nächster Versuch beim kommenden Prüflauf."
        )

    def fetch_new_posts(self, username: str, is_known) -> list[PostInfo]:
        """Liefert die noch unbekannten unter den neuesten Beiträgen.

        Parameter:
          username – zu prüfendes Konto
          is_known – Callback ``(shortcode: str) -> bool``; True, wenn der
                     Beitrag bereits registriert ist. So bleibt dieses
                     Modul frei von Datenbank-Wissen.

        Es werden nur die ``posts_per_check`` neuesten Beiträge betrachtet
        (Instaloader liefert sie neueste zuerst). Das reicht für eine
        Überwachung im Minuten-/Stundentakt völlig aus und hält die Zahl
        der Anfragen klein.
        """
        try:
            profile = self._get_profile(username)
            # WICHTIG: is_private ist eine "faule" Property – der Zugriff
            # kann Metadaten nachladen und dabei Instaloader-Ausnahmen
            # werfen. Deshalb wird sie HIER, innerhalb des try-Blocks,
            # gelesen.
            profile_is_private = profile.is_private
        except ProfileNotExistsException as exc:
            # Kann noch aus dem faulen Metadaten-Nachladen kommen; die
            # Namensauflösung selbst behandelt _get_profile differenziert.
            raise TemporaryError(
                f"Profil @{username} konnte nicht geladen werden: {exc}"
            ) from exc
        except ConnectionException as exc:
            # Netzwerkfehler ODER Drosselung durch Instagram (z. B. 429).
            raise TemporaryError(
                f"Verbindungsproblem bei @{username}: {exc}"
            ) from exc
        except InstaloaderException as exc:
            raise TemporaryError(f"Fehler bei @{username}: {exc}") from exc

        # Private Profile: nur zugreifen, wenn der ANGEMELDETE Benutzer dem
        # Konto folgt (dann liefert Instaloader die Beiträge legitim).
        # Ohne Anmeldung bzw. ohne Berechtigung wird nicht zugegriffen.
        if profile_is_private and not self.is_logged_in:
            raise ProfileIsPrivate(
                f"@{username} ist privat – ohne Anmeldung kein Zugriff. "
                "Melde dich mit deinem eigenen Konto an; Zugriff besteht nur "
                "auf private Konten, denen du folgst."
            )

        posts = self._collect_posts(
            profile, limit=self._settings.posts_per_check, is_known=is_known
        )
        # Älteste zuerst zurückgeben, damit Benachrichtigungen und Downloads
        # in chronologischer Reihenfolge erfolgen.
        posts.reverse()
        return posts

    def list_recent_posts(self, username: str, limit: int) -> list[PostInfo]:
        """Liefert die neuesten ``limit`` Beiträge – für den MANUELLEN Download.

        Anders als :meth:`fetch_new_posts` filtert diese Methode nicht nach
        "schon bekannt": Der Benutzer will damit gezielt vorhandene Inhalte
        herunterladen. Doppelte Dateien werden trotzdem vermieden, weil
        Instaloader vorhandene Dateien überspringt und die Datenbank den
        Download-Status führt.

        Ergebnisse werden kurz zwischengespeichert (``_CACHE_TTL``), damit
        wiederholtes Öffnen/Feed-Aufbau schnell ist und Instagram weniger
        oft angefragt wird.
        """
        key = username.lower()
        cached = self._recent_cache.get(key)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL \
                and len(cached[1]) >= limit:
            return cached[1][:limit]

        try:
            profile = self._get_profile(username)
            profile_is_private = profile.is_private
        except ProfileNotExistsException as exc:
            raise TemporaryError(
                f"Profil @{username} konnte nicht geladen werden: {exc}"
            ) from exc
        except ConnectionException as exc:
            raise TemporaryError(
                f"Verbindungsproblem bei @{username}: {exc}"
            ) from exc
        except InstaloaderException as exc:
            raise TemporaryError(f"Fehler bei @{username}: {exc}") from exc

        if profile_is_private and not self.is_logged_in:
            raise ProfileIsPrivate(
                f"@{username} ist privat – ohne Anmeldung kein Zugriff. "
                "Melde dich mit deinem eigenen Konto an (nur Konten, denen "
                "du folgst)."
            )

        posts = self._collect_posts(profile, limit=max(1, int(limit)), is_known=None)
        self._recent_cache[key] = (time.monotonic(), posts)
        return posts

    def _collect_posts(
        self,
        profile: "instaloader.Profile",
        limit: int,
        is_known: Optional[Callable[[str], bool]],
    ) -> list[PostInfo]:
        """Iteriert die neuesten ``limit`` Beiträge eines Profils.

        ``is_known`` (optional) filtert bereits bekannte Beiträge heraus.
        Die Iteration wird unter dem Loader-Lock ausgeführt.
        """
        username = profile.username
        collected: list[PostInfo] = []
        try:
            with self._paced_lock():
                # islice: nur die N neuesten Beiträge anfassen.
                for post in islice(profile.get_posts(), limit):
                    if is_known is not None and is_known(post.shortcode):
                        continue
                    # Vorschaubild/Beschriftung sind Komfort für die
                    # Web-Oberfläche – Fehler dabei sind unkritisch.
                    try:
                        thumbnail = post.url or ""
                    except Exception:
                        thumbnail = ""
                    try:
                        caption = (post.caption or "")[:280]
                    except Exception:
                        caption = ""
                    try:
                        likes = int(post.likes)
                    except Exception:
                        likes = 0
                    try:
                        comments = int(post.comments)
                    except Exception:
                        comments = 0
                    collected.append(
                        PostInfo(
                            shortcode=post.shortcode,
                            username=username,
                            post_type=_classify_post(post),
                            posted_at=post.date_utc.isoformat(timespec="seconds"),
                            url=f"https://www.instagram.com/p/{post.shortcode}/",
                            thumbnail_url=thumbnail,
                            caption=caption,
                            likes=likes,
                            comments=comments,
                            raw=post,
                        )
                    )
        except PrivateProfileNotFollowedException as exc:
            raise ProfileIsPrivate(
                f"@{username} ist privat und du folgst dem Konto nicht – "
                "kein Zugriff."
            ) from exc
        except LoginRequiredException as exc:
            raise TemporaryError(
                f"Für @{username} ist eine Anmeldung erforderlich: {exc}"
            ) from exc
        except ConnectionException as exc:
            self._register_throttle(exc)
            raise TemporaryError(
                f"Verbindungsproblem beim Lesen der Beiträge von @{username}: {exc}"
            ) from exc
        except InstaloaderException as exc:
            raise TemporaryError(
                f"Fehler beim Lesen der Beiträge von @{username}: {exc}"
            ) from exc
        self._register_success()
        return collected

    # ------------------------------------------------------------------
    # Abonnements (wem der angemeldete Benutzer folgt)
    # ------------------------------------------------------------------
    def following_usernames(self, limit: Optional[int] = None) -> list[str]:
        """Liefert die Benutzernamen der Konten, denen man folgt.

        Erfordert eine Anmeldung (nur das eigene Abo-Verzeichnis ist so
        abrufbar). ``limit`` begrenzt optional die Anzahl.
        Wirft :class:`LoginError`, wenn nicht angemeldet, und
        :class:`TemporaryError` bei Netzwerk-/Drosselungsproblemen.
        """
        return [f["username"] for f in self.following_details(limit)]

    def following_details(self, limit: Optional[int] = None) -> list[dict]:
        """Wie :meth:`following_usernames`, aber mit Name und Profilbild.

        Rückgabe je Konto: {"username", "full_name", "pic"} – die Angaben
        stammen aus der Abo-Liste selbst (keine Extra-Anfragen pro Konto).
        """
        if not self.is_logged_in:
            raise LoginError(
                "Für die Abo-Liste ist eine Anmeldung nötig. Bitte zuerst "
                "anmelden (z. B. „Aus Browser übernehmen“)."
            )
        with self._paced_lock():
            try:
                me = instaloader.Profile.own_profile(self._loader.context)
                out: list[dict] = []
                for profile in me.get_followees():
                    try:
                        full_name = profile.full_name or ""
                    except Exception:
                        full_name = ""
                    try:
                        pic = profile.profile_pic_url or ""
                    except Exception:
                        pic = ""
                    out.append({
                        "username": profile.username,
                        "full_name": full_name,
                        "pic": pic,
                    })
                    if limit is not None and len(out) >= limit:
                        break
                self._register_success()
                return out
            except LoginRequiredException as exc:
                raise LoginError(
                    "Die Anmeldung ist nicht (mehr) gültig – bitte neu anmelden."
                ) from exc
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise TemporaryError(
                    f"Abo-Liste derzeit nicht abrufbar (Verbindung/Drosselung): "
                    f"{exc}"
                ) from exc
            except (AbortDownloadException, InstaloaderException) as exc:
                # Fängt auch Instagram-Sicherheitscheckpoints ab.
                raise self._fetch_error(exc, "Abo-Liste") from exc

    # ------------------------------------------------------------------
    # Stories & Highlights (erfordern Anmeldung)
    # ------------------------------------------------------------------
    def _storyitem_info(self, item, username: str, label: str) -> PostInfo:
        """Wandelt ein Instaloader-StoryItem in ein PostInfo um."""
        is_video = bool(getattr(item, "is_video", False))
        try:
            posted = item.date_utc.isoformat(timespec="seconds")
        except Exception:
            posted = ""
        return PostInfo(
            shortcode=str(item.mediaid),   # eindeutig & stabil → für Dedup
            username=username,
            post_type=label + (" · Video" if is_video else ""),
            posted_at=posted,
            url=f"https://www.instagram.com/stories/{username}/{item.mediaid}/",
            thumbnail_url=getattr(item, "url", "") or "",
            raw=item,
        )

    def list_story_items(self, username: str, kind: str = "stories") -> list[PostInfo]:
        """Liefert die aktuellen Story- bzw. Highlight-Elemente eines Kontos.

        ``kind`` = "stories" (aktuelle 24-h-Stories) oder "highlights"
        (dauerhafte Highlights). Erfordert Anmeldung; bei privaten Konten
        musst du dem Konto folgen.
        """
        if not self.is_logged_in:
            raise LoginError(
                "Für Stories/Highlights ist eine Anmeldung nötig. Bitte "
                "zuerst anmelden (z. B. „Aus Browser übernehmen“)."
            )
        # Profil zuerst auflösen (pausiert + lockt selbst) – NICHT innerhalb
        # des nachfolgenden Locks, sonst liefe dessen Anfragen-Pause wieder
        # unter gehaltenem Lock.
        profile = self._get_profile(username)
        with self._paced_lock():
            try:
                items: list[PostInfo] = []
                if kind == "highlights":
                    for highlight in self._loader.get_highlights(profile):
                        label = (
                            f"Highlight: {highlight.title}"
                            if highlight.title else "Highlight"
                        )
                        for item in highlight.get_items():
                            items.append(self._storyitem_info(item, username, label))
                else:
                    for story in self._loader.get_stories(userids=[profile.userid]):
                        for item in story.get_items():
                            items.append(
                                self._storyitem_info(item, username, "Story")
                            )
                return items
            except LoginRequiredException as exc:
                raise LoginError(
                    "Die Anmeldung ist nicht (mehr) gültig – bitte neu anmelden."
                ) from exc
            except ConnectionException as exc:
                raise TemporaryError(
                    f"Stories von @{username} derzeit nicht abrufbar "
                    f"(Verbindung/Drosselung): {exc}"
                ) from exc
            except InstaloaderException as exc:
                raise TemporaryError(
                    f"Stories von @{username} konnten nicht geladen werden: {exc}"
                ) from exc

    def download_story_item(self, post_info: PostInfo) -> bool:
        """Lädt ein Story-/Highlight-Element in seinen eigenen Konto-Ordner.

        Stories → ``<username>/Story``, Highlights → ``<username>/Highlights``
        (jeweils Bilder UND Videos im selben Ordner). Ob es ein Highlight ist,
        wird am Beitragstyp erkannt (``post_type`` beginnt mit "Highlight").
        """
        download_dir: Path = self._settings.download_dir
        item = post_info.raw
        if item is None:
            raise TemporaryError(
                "Story-Element ist nicht mehr verfügbar – bitte neu laden."
            )
        # URL + Typ aus dem bereits geladenen Story-Item lesen (unter Lock,
        # ohne Pause – kein neuer API-Abruf nötig). Der Byte-Download läuft
        # danach lockfrei vom CDN.
        with self._lock:
            is_video = bool(getattr(item, "is_video", False))
            url = (getattr(item, "video_url", "") if is_video else "") \
                or getattr(item, "url", "") or ""
        if not url:
            raise TemporaryError(
                f"Story {post_info.shortcode} hat keine abrufbare "
                "Medien-Adresse."
            )
        # Story vs. Highlight am Beitragstyp erkennen (list_story_items setzt
        # "Story…" bzw. "Highlight…"). Eigener Ordner je Art.
        sub = (_HIGHLIGHT_SUBDIR
               if (post_info.post_type or "").startswith("Highlight")
               else _STORY_SUBDIR)
        ext = ".mp4" if is_video else ".jpg"
        dest = download_dir / post_info.username / sub / f"{post_info.shortcode}{ext}"
        if dest.is_file() and dest.stat().st_size > 0:
            return True
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TemporaryError(
                f"Story {post_info.shortcode} konnte nicht gespeichert "
                f"werden: {exc}"
            ) from exc
        self._download_file(url, dest)
        return True

    # ------------------------------------------------------------------
    # Profil-Infos, Follower, Suche
    # ------------------------------------------------------------------
    @staticmethod
    def _profile_summary(profile: "instaloader.Profile") -> dict:
        """Kompakte Profil-Infos (best effort, einzelne Felder optional)."""
        def _safe(fn, default):
            try:
                return fn()
            except Exception:
                return default
        return {
            "username": profile.username,
            "full_name": _safe(lambda: profile.full_name or "", ""),
            "pic": _safe(lambda: profile.profile_pic_url or "", ""),
            "biography": _safe(lambda: profile.biography or "", ""),
            "posts": _safe(lambda: int(profile.mediacount), 0),
            "followers": _safe(lambda: int(profile.followers), 0),
            "following": _safe(lambda: int(profile.followees), 0),
            "is_verified": _safe(lambda: bool(profile.is_verified), False),
            "is_private": _safe(lambda: bool(profile.is_private), False),
        }

    def me(self) -> dict:
        """Infos zum eigenen (angemeldeten) Konto."""
        if not self.is_logged_in:
            raise LoginError("Nicht angemeldet.")
        with self._paced_lock():
            try:
                profile = instaloader.Profile.own_profile(self._loader.context)
                return self._profile_summary(profile)
            except LoginRequiredException as exc:
                raise LoginError("Anmeldung nicht mehr gültig – bitte neu "
                                 "anmelden.") from exc
            except ConnectionException as exc:
                raise TemporaryError(f"Konto-Infos nicht abrufbar: {exc}") from exc
            except InstaloaderException as exc:
                raise TemporaryError(f"Konto-Infos nicht abrufbar: {exc}") from exc

    def profile_info(self, username: str) -> dict:
        """Kompakte Infos zu einem beliebigen Profil (für die Profil-Ansicht).

        Kurz zwischengespeichert (``_CACHE_TTL``) für schnelle Navigation.
        """
        key = username.lower()
        cached = self._profile_cache.get(key)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
            return cached[1]
        profile = self._get_profile(username)
        with self._lock:
            info = self._profile_summary(profile)
        self._profile_cache[key] = (time.monotonic(), info)
        return info

    def followers_details(self, limit: Optional[int] = None) -> list[dict]:
        """Liste der eigenen Follower (``limit`` None = alle). Erfordert Anmeldung."""
        if not self.is_logged_in:
            raise LoginError("Für die Follower-Liste ist eine Anmeldung nötig.")
        with self._paced_lock():
            try:
                me = instaloader.Profile.own_profile(self._loader.context)
                out: list[dict] = []
                for profile in me.get_followers():
                    out.append({
                        "username": profile.username,
                        "full_name": getattr(profile, "full_name", "") or "",
                        "pic": getattr(profile, "profile_pic_url", "") or "",
                    })
                    if limit is not None and len(out) >= limit:
                        break
                self._register_success()
                return out
            except LoginRequiredException as exc:
                raise LoginError("Anmeldung nicht mehr gültig.") from exc
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise TemporaryError(f"Follower nicht abrufbar: {exc}") from exc
            except (AbortDownloadException, InstaloaderException) as exc:
                raise self._fetch_error(exc, "Follower-Liste") from exc

    def active_story_usernames(self) -> list[str]:
        """Konten mit GERADE aktiver Story (die eigene Story-Leiste).

        Instagram liefert die komplette Story-Leiste in EINEM Abruf –
        deshalb ist das die sparsamste Art, Story-Punkte für alle Abos
        anzuzeigen. Erfordert Anmeldung.
        """
        if not self.is_logged_in:
            raise LoginError("Für Stories ist eine Anmeldung nötig.")
        with self._paced_lock():
            try:
                out: list[str] = []
                for story in self._loader.get_stories():
                    try:
                        name = story.owner_username or ""
                    except Exception:
                        name = ""
                    if name:
                        out.append(name)
                self._register_success()
                return out
            except LoginRequiredException as exc:
                raise LoginError(
                    "Die Anmeldung ist nicht (mehr) gültig – bitte neu anmelden."
                ) from exc
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise TemporaryError(
                    f"Story-Leiste derzeit nicht abrufbar: {exc}"
                ) from exc
            except (AbortDownloadException, InstaloaderException) as exc:
                raise self._fetch_error(exc, "Story-Leiste") from exc

    def get_comments(self, post_info: PostInfo, limit: int = 15) -> list[dict]:
        """Liefert die neuesten Kommentare eines Beitrags (erfordert Anmeldung)."""
        if not self.is_logged_in:
            raise LoginError("Für Kommentare ist eine Anmeldung nötig.")
        post = post_info.raw
        with self._paced_lock():
            try:
                if post is None:
                    post = instaloader.Post.from_shortcode(
                        self._loader.context, post_info.shortcode
                    )
                out: list[dict] = []
                for comment in post.get_comments():
                    try:
                        owner = comment.owner.username
                    except Exception:
                        owner = ""
                    try:
                        posted = comment.created_at_utc.isoformat(timespec="seconds")
                    except Exception:
                        posted = ""
                    out.append({
                        "username": owner,
                        "text": (comment.text or "")[:600],
                        "likes": int(getattr(comment, "likes_count", 0) or 0),
                        "posted_at": posted,
                    })
                    if len(out) >= limit:
                        break
                return out
            except LoginRequiredException as exc:
                raise LoginError("Anmeldung nicht mehr gültig.") from exc
            except ConnectionException as exc:
                raise TemporaryError(f"Kommentare nicht abrufbar: {exc}") from exc
            except InstaloaderException as exc:
                raise TemporaryError(f"Kommentare nicht abrufbar: {exc}") from exc

    def hashtag_posts(self, tag: str, limit: int = 24) -> list[PostInfo]:
        """Neueste öffentliche Beiträge zu einem Hashtag (für „Entdecken“)."""
        tag = (tag or "").strip().lstrip("#").lower()
        if not tag:
            return []
        with self._paced_lock():
            try:
                hashtag = instaloader.Hashtag.from_name(self._loader.context, tag)
                collected: list[PostInfo] = []
                for post in islice(hashtag.get_posts(), limit):
                    try:
                        thumbnail = post.url or ""
                    except Exception:
                        thumbnail = ""
                    collected.append(PostInfo(
                        shortcode=post.shortcode,
                        username=post.owner_username,
                        post_type=_classify_post(post),
                        posted_at=post.date_utc.isoformat(timespec="seconds"),
                        url=f"https://www.instagram.com/p/{post.shortcode}/",
                        thumbnail_url=thumbnail,
                        raw=post,
                    ))
                return collected
            except ProfileNotExistsException as exc:
                raise ProfileNotFound(f"Hashtag #{tag} nicht gefunden.") from exc
            except ConnectionException as exc:
                raise TemporaryError(f"Hashtag-Beiträge nicht abrufbar: {exc}") from exc
            except InstaloaderException as exc:
                raise TemporaryError(f"Hashtag #{tag}: {exc}") from exc

    def search_profiles(self, query: str, limit: int = 12) -> list[dict]:
        """Sucht Konten nach Namen (Instagram-Top-Suche)."""
        query = (query or "").strip().lstrip("@")
        if not query:
            return []
        with self._paced_lock():
            try:
                results = instaloader.TopSearchResults(self._loader.context, query)
                out: list[dict] = []
                for profile in results.get_profiles():
                    out.append({
                        "username": profile.username,
                        "full_name": getattr(profile, "full_name", "") or "",
                        "pic": getattr(profile, "profile_pic_url", "") or "",
                        "is_verified": bool(getattr(profile, "is_verified", False)),
                    })
                    if len(out) >= limit:
                        break
                return out
            except ConnectionException as exc:
                raise TemporaryError(f"Suche derzeit nicht möglich: {exc}") from exc
            except InstaloaderException as exc:
                raise TemporaryError(f"Suche fehlgeschlagen: {exc}") from exc

    # ------------------------------------------------------------------
    # Direkte Medien-URLs (zum Streamen auf das anfragende Gerät)
    # ------------------------------------------------------------------
    def media_urls(self, post_info: PostInfo) -> list[tuple[str, str]]:
        """Liefert die direkten Medien-URLs eines Beitrags.

        Rückgabe: Liste von (url, dateiname). Für ein einzelnes Foto/Video
        genau ein Eintrag, für ein Album (Sidecar) mehrere. Die URLs zeigen
        auf das Instagram-CDN; das Streamen an das Gerät übernimmt die
        aufrufende Schicht (Web-Server).
        """
        post = post_info.raw
        # Ist das Post-Objekt bereits geladen (Feed/Anzeige), stecken die
        # Medien-URLs schon darin – kein Instagram-Abruf, also auch keine
        # Anfragen-Pause nötig: Anzeige/Download starten dann ohne Wartezeit.
        with self._paced_lock(pace=post is None):
            try:
                if post is None:
                    post = instaloader.Post.from_shortcode(
                        self._loader.context, post_info.shortcode
                    )
                items: list[tuple[str, str]] = []
                base = post_info.shortcode
                if post.typename == "GraphSidecar":
                    for index, node in enumerate(post.get_sidecar_nodes(), start=1):
                        if node.is_video and node.video_url:
                            items.append((node.video_url, f"{base}_{index}.mp4"))
                        elif node.display_url:
                            items.append((node.display_url, f"{base}_{index}.jpg"))
                elif post.is_video and post.video_url:
                    items.append((post.video_url, f"{base}.mp4"))
                elif post.url:
                    items.append((post.url, f"{base}.jpg"))
                return items
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise TemporaryError(
                    f"Medien-Adresse von {post_info.shortcode} nicht abrufbar "
                    f"(Verbindung/Drosselung): {exc}"
                ) from exc
            except InstaloaderException as exc:
                raise TemporaryError(
                    f"Medien-Adresse von {post_info.shortcode} nicht abrufbar: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # Download (auf den PC, in den konfigurierten Ordner)
    # ------------------------------------------------------------------
    def _download_file(self, url: str, dest: Path) -> None:
        """Lädt eine einzelne Medien-Datei vom CDN und speichert sie atomar.

        Kein Loader-Lock nötig: Der Abruf geht an das Instagram-CDN (kein
        API-Ratenlimit), nicht an die GraphQL-API. Bei einem Fehler wird die
        halbfertige Datei entfernt und ein :class:`TemporaryError` geworfen.
        """
        tmp = dest.with_name(dest.name + ".part")
        try:
            with _requests.get(
                url, timeout=60, stream=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
            tmp.replace(dest)
        except Exception as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise TemporaryError(
                f"Datei {dest.name} konnte nicht geladen werden: {exc}"
            ) from exc

    def download_post(self, post_info: PostInfo) -> bool:
        """Lädt einen Beitrag typgetrennt in den Konto-Ordner.

        Zielstruktur: ``<download_dir>/<username>/Fotos`` (Bilder) bzw.
        ``<download_dir>/<username>/Videos`` (Videos/Reels). Alben werden pro
        Datei auf beide Ordner aufgeteilt. Die Dateinamen sind fest
        (Shortcode-basiert), damit Laptop und Tablet dieselbe Ablage erzeugen
        und eine Cloud (TeraBox) sie ohne Doppelte zusammenführt.

        Rückgabe: immer True – der Beitrag liegt danach lokal vor (bereits
        vorhandene Dateien werden übersprungen). Fehler werden ausschließlich
        als :class:`TemporaryError` gemeldet, damit ausstehende Downloads beim
        nächsten Lauf erneut versucht werden.
        """
        download_dir: Path = self._settings.download_dir
        username = post_info.username
        # Direkte Medien-URLs auflösen (media_urls hält dafür nur kurz den
        # Loader-Lock und pausiert bloß bei einem echten Instagram-Abruf). Der
        # eigentliche Byte-Download läuft danach lockfrei vom CDN.
        items = self.media_urls(post_info)
        if not items:
            raise TemporaryError(
                f"Für Beitrag {post_info.shortcode} wurde kein Medium gefunden."
            )
        try:
            newly = 0
            for url, filename in items:
                dest_dir = download_dir / username / _media_subdir(filename)
                dest = dest_dir / filename
                if dest.is_file() and dest.stat().st_size > 0:
                    continue  # bereits vorhanden → überspringen
                dest_dir.mkdir(parents=True, exist_ok=True)
                self._download_file(url, dest)
                newly += 1
            # Optional: Bildunterschrift als .txt in den Fotos-Ordner.
            if self._settings.save_captions and post_info.caption:
                try:
                    cap_dir = download_dir / username / _PHOTO_SUBDIR
                    cap_dir.mkdir(parents=True, exist_ok=True)
                    (cap_dir / f"{post_info.shortcode}.txt").write_text(
                        post_info.caption, encoding="utf-8"
                    )
                except OSError:
                    logger.debug("Bildunterschrift nicht speicherbar.",
                                 exc_info=True)
        except OSError as exc:
            raise TemporaryError(
                f"Beitrag {post_info.shortcode} konnte nicht gespeichert "
                f"werden: {exc}"
            ) from exc
        if newly:
            logger.info(
                "Beitrag %s (@%s, %s): %d Datei(en) gespeichert unter %s",
                post_info.shortcode, username, post_info.post_type, newly,
                download_dir / username,
            )
        else:
            logger.info("Beitrag %s (@%s) war bereits vollständig vorhanden.",
                        post_info.shortcode, username)
        self._register_success()
        return True

    def download_by_shortcode(self, shortcode: str) -> dict:
        """Lädt einen Beitrag/Reel allein anhand seines Kürzels.

        Für die Funktion „Instagram-Link speichern" (Lesezeichen am Laptop,
        Teilen-Ziel am Handy): Der Beitrag wird zuerst aufgelöst (Konto, Typ,
        Zeitpunkt) und dann typgetrennt heruntergeladen. Rückgabe:
        ``{"shortcode", "username", "type", "posted_at"}``. Wirft die üblichen
        :class:`DownloaderError`-Typen (nicht gefunden, privat, Drosselung …).
        """
        shortcode = (shortcode or "").strip()
        if not shortcode:
            raise TemporaryError("Kein Beitrags-Kürzel angegeben.")
        self._pace()
        with self._lock:
            try:
                post = instaloader.Post.from_shortcode(
                    self._loader.context, shortcode
                )
                username = post.owner_username
                ptype = _classify_post(post)
                try:
                    posted = post.date_utc.isoformat(timespec="seconds")
                except Exception:
                    posted = ""
                self._register_success()
            except ProfileNotExistsException as exc:
                raise ProfileNotFound(
                    f"Beitrag {shortcode} wurde nicht gefunden."
                ) from exc
            except ConnectionException as exc:
                self._register_throttle(exc)
                raise TemporaryError(
                    f"Beitrag {shortcode} derzeit nicht abrufbar "
                    f"(Verbindung/Drosselung): {exc}"
                ) from exc
            except (AbortDownloadException, InstaloaderException) as exc:
                raise self._fetch_error(exc, f"Beitrag {shortcode}") from exc
        info = PostInfo(
            shortcode=shortcode,
            username=username,
            post_type=ptype,
            posted_at=posted,
            url=f"https://www.instagram.com/p/{shortcode}/",
            raw=post,
        )
        self.download_post(info)
        return {"shortcode": shortcode, "username": username,
                "type": ptype, "posted_at": posted}
