"""
Hintergrund-Zwischenspeicher für Gefolgt-/Follower-Listen und Profilbilder.
===========================================================================

Ziel: Beim Öffnen der Browser-Oberfläche sind Gefolgt- und Follower-Liste
SOFORT da – ohne einen einzigen Instagram-Abruf. Dafür gilt:

  * Die Listen liegen in der SQLite-Datenbank (Tabelle ``social_accounts``)
    und werden von dort ausgeliefert (Millisekunden statt Sekunden).
  * Ein einzelner Hintergrund-Thread frischt die Listen in ruhigem Tempo
    auf (Standard: einmal pro Stunde, einstellbar) – Instagram sieht also
    höchstens eine Handvoll Anfragen pro Stunde statt einer Flut bei jedem
    Seitenaufruf.
  * Profilbilder werden EINMAL heruntergeladen und lokal gespeichert
    (``data/cache/avatars/``). Instagram-Bild-URLs laufen nach einigen
    Stunden ab – lokale Kopien sind dauerhaft gültig und sofort da.

Der Thread nutzt dieselbe :class:`InstagramDownloader`-Instanz wie der
Rest der Anwendung; deren interner Lock serialisiert alle Zugriffe.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests as _requests

from .downloader import DownloaderError, LoginError

if TYPE_CHECKING:
    from .database import Database
    from .downloader import InstagramDownloader
    from .settings import Settings

logger = logging.getLogger(__name__)

# Wie oft der Hintergrund-Thread nachsieht, ob eine Auffrischung fällig ist.
_TICK_SECONDS = 30.0

# Pause zwischen zwei Profilbild-Downloads (CDN-Abrufe, kein API-Limit –
# trotzdem rücksichtsvoll gedrosselt).
_AVATAR_DELAY = 0.15

# Profilbilder, die älter sind als diese Spanne, werden bei der nächsten
# Auffrischung neu geladen (Profilbilder ändern sich selten).
_AVATAR_MAX_AGE_DAYS = 7

# Nur sichere Dateinamen für Profilbilder (Instagram-Namen sind ohnehin
# auf Buchstaben/Ziffern/Punkt/Unterstrich beschränkt – doppelt hält besser).
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class SocialCache:
    """Hält Gefolgt-/Follower-Listen und Profilbilder lokal aktuell."""

    def __init__(
        self,
        db: "Database",
        settings: "Settings",
        downloader: "InstagramDownloader",
        avatar_dir: Path,
    ) -> None:
        self._db = db
        self._settings = settings
        self._downloader = downloader
        self._avatar_dir = Path(avatar_dir)
        self._avatar_dir.mkdir(parents=True, exist_ok=True)

        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()      # Sofort-Auffrischung anstoßen
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False               # läuft gerade eine Auffrischung?
        self._last_error = ""

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Startet den Hintergrund-Thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="social-cache", daemon=True
        )
        self._thread.start()

    def refresh_now(self) -> bool:
        """Stößt eine sofortige Auffrischung an.

        Rückgabe: False, wenn bereits eine läuft (kein Doppelstart).
        """
        with self._state_lock:
            if self._running:
                return False
        self._wake.set()
        return True

    def status(self) -> dict:
        """Zustand für die Oberfläche (läuft gerade? letzter Fehler?)."""
        with self._state_lock:
            err = self._last_error.lower()
            return {
                "running": self._running,
                "last_error": self._last_error,
                "auto": self._settings.social_auto_refresh,
                "interval_minutes": self._settings.social_refresh_minutes,
                # Instagram verlangt eine Sicherheitsbestätigung (Checkpoint)?
                "checkpoint": "checkpoint" in err or "sicherheitsbest" in err,
                # Anmeldung vermutlich abgelaufen? (Die Auffrischung ist der
                # regelmäßige "Gesundheitscheck" der Session.)
                "session_invalid": "anmeld" in err,
            }

    # ------------------------------------------------------------------
    # Profilbilder
    # ------------------------------------------------------------------
    def avatar_file(self, username: str) -> Optional[Path]:
        """Pfad des lokal gespeicherten Profilbilds (None, wenn keins da)."""
        username = (username or "").strip().lstrip("@").lower()
        if not _SAFE_NAME_RE.match(username):
            return None
        path = self._avatar_dir / f"{username}.jpg"
        return path if path.exists() else None

    def store_avatar(self, username: str, url: str) -> Optional[Path]:
        """Lädt ein Profilbild herunter und speichert es lokal (best effort).

        Vorhandene, noch frische Dateien werden nicht erneut geladen.
        Rückgabe: Pfad der lokalen Datei oder None bei Fehlern.
        """
        username = (username or "").strip().lstrip("@").lower()
        if not url or not _SAFE_NAME_RE.match(username):
            return None
        path = self._avatar_dir / f"{username}.jpg"
        try:
            if path.exists():
                age_days = (time.time() - path.stat().st_mtime) / 86400
                if age_days < _AVATAR_MAX_AGE_DAYS:
                    return path
            upstream = _requests.get(
                url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            upstream.raise_for_status()
            content = upstream.content
            if not content:
                return path if path.exists() else None
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
            return path
        except Exception:
            # Abgelaufene CDN-URL, Netzwerkfehler … – alte Datei behalten.
            logger.debug("Profilbild @%s nicht ladbar.", username, exc_info=True)
            return path if path.exists() else None

    # ------------------------------------------------------------------
    # Hintergrund-Schleife
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            woken = self._wake.wait(timeout=_TICK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                if woken or self._refresh_due():
                    self._refresh()
            except Exception:
                # Der Thread darf niemals sterben.
                logger.exception("Unerwarteter Fehler bei der Listen-Auffrischung.")

    def _refresh_due(self) -> bool:
        """Ist die automatische Auffrischung fällig?"""
        if not self._settings.social_auto_refresh:
            return False
        if not self._downloader.is_logged_in:
            return False
        # Ruhezeit (z. B. nachts): keine automatischen Abrufe. Eine manuell
        # angestoßene Auffrischung (refresh_now) läuft trotzdem.
        if self._settings.in_quiet_hours(datetime.now().strftime("%H:%M")):
            return False
        interval = self._settings.social_refresh_minutes * 60
        newest = ""
        for kind in ("following", "followers"):
            fetched = self._db.social_fetched_at(kind)
            if not fetched:
                return True  # noch nie geholt → sofort fällig
            newest = max(newest, fetched)
        try:
            # ``social_fetched_at`` wird mit Zeitzone gespeichert (…+00:00).
            # Ein naives ``datetime.now()`` warf hier einen TypeError, der
            # nicht abgefangen wurde: Die Fälligkeitsprüfung scheiterte ab
            # dem ersten gespeicherten Stand bei JEDEM Durchlauf, die Listen
            # wurden nie wieder von selbst aufgefrischt.
            stamp = datetime.fromisoformat(newest)
            if stamp.tzinfo is None:  # Altbestand ohne Zeitzone: als UTC lesen
                stamp = stamp.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - stamp).total_seconds()
        except (ValueError, TypeError):
            return True
        return elapsed >= interval

    def _refresh(self) -> None:
        """Holt beide Listen von Instagram und speichert sie + Profilbilder."""
        if not self._downloader.is_logged_in:
            with self._state_lock:
                self._last_error = "Nicht angemeldet."
            return
        with self._state_lock:
            if self._running:
                return
            self._running = True
            self._last_error = ""
        logger.info("Auffrischung der Gefolgt-/Follower-Listen gestartet …")
        try:
            pics: dict[str, str] = {}
            for kind, fetch in (
                ("following", self._downloader.following_details),
                ("followers", self._downloader.followers_details),
            ):
                if self._stop.is_set():
                    return
                try:
                    items = fetch()
                except LoginError as exc:
                    with self._state_lock:
                        self._last_error = str(exc)
                    logger.warning("Listen-Auffrischung (%s): %s", kind, exc)
                    return
                except DownloaderError as exc:
                    with self._state_lock:
                        self._last_error = str(exc)
                    logger.warning("Listen-Auffrischung (%s): %s", kind, exc)
                    continue
                self._db.replace_social_list(kind, items)
                for item in items:
                    if item.get("pic") and item.get("username"):
                        pics.setdefault(item["username"], item["pic"])
            # Story-Punkte: EIN Abruf der eigenen Story-Leiste zeigt, welche
            # Konten gerade eine aktive Story haben (best effort).
            try:
                self._db.set_story_usernames(
                    self._downloader.active_story_usernames()
                )
            except DownloaderError as exc:
                logger.debug("Story-Leiste nicht abrufbar: %s", exc)
            self._download_avatars(pics)
        finally:
            with self._state_lock:
                self._running = False
            logger.info("Auffrischung der Gefolgt-/Follower-Listen beendet.")

    def _download_avatars(self, pics: dict[str, str]) -> None:
        """Lädt fehlende/veraltete Profilbilder nach (CDN, gedrosselt)."""
        missing = []
        for username, url in pics.items():
            path = self._avatar_dir / f"{username.lower()}.jpg"
            try:
                if path.exists():
                    age_days = (time.time() - path.stat().st_mtime) / 86400
                    if age_days < _AVATAR_MAX_AGE_DAYS:
                        continue
            except OSError:
                pass
            missing.append((username, url))
        if not missing:
            return
        logger.info("Lade %d Profilbild(er) in den lokalen Cache …", len(missing))
        for username, url in missing:
            if self._stop.is_set():
                return
            self.store_avatar(username, url)
            time.sleep(_AVATAR_DELAY)
        # Neue Bilder sollen sofort in der Oberfläche erscheinen.
        self._db.bump_social_version()

    def shutdown(self) -> None:
        """Beendet den Hintergrund-Thread (beim App-Ende)."""
        self._stop.set()
        self._wake.set()
