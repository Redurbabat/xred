"""
Einstieg für die Android-App (Chaquopy).
========================================

Startet denselben Flask-Server wie auf dem PC – nur eben direkt auf dem
Handy. Die Activity lädt anschließend http://127.0.0.1:8756/ in einer
WebView. Datenbank, Sitzungen und Downloads liegen im App-Speicher
(Android/data/de.redur.igmonitor/files/).
"""

from __future__ import annotations

import logging
import threading

_started = False
_lock = threading.Lock()


def start_server(base_dir: str) -> int:
    """Startet den Server einmalig im Hintergrund; gibt den Port zurück."""
    global _started
    with _lock:
        if _started:
            return 8756
        _started = True

    logging.basicConfig(level=logging.INFO)

    from pathlib import Path

    base = Path(base_dir)
    (base / "data").mkdir(parents=True, exist_ok=True)

    from app.database import Database
    from app.downloader import InstagramDownloader
    from app.settings import Settings
    from app.web import create_app

    db = Database(base / "data" / "monitor.db")
    settings = Settings(db, default_download_dir=str(base / "downloads"))

    # Wenn der „Alle Dateien“-Zugriff erteilt wurde: sichtbar in den
    # Foto-Ordner speichern (Pictures/InstagramMonitor/<konto>) – dort
    # sehen ihn Galerie und TeraBox-Backup. Eine vom Nutzer selbst
    # gesetzte Ordner-Wahl wird dabei nie überschrieben.
    try:
        pictures = Path("/storage/emulated/0/Pictures/InstagramMonitor")
        current = str(settings.download_dir)
        app_private_default = str(base / "downloads")
        if current in (app_private_default, str(pictures)):
            pictures.mkdir(parents=True, exist_ok=True)
            probe = pictures / ".schreibtest"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            if current != str(pictures):
                settings.download_dir = pictures
    except OSError:
        pass  # keine Freigabe – privater App-Speicher bleibt in Ordnung
    downloader = InstagramDownloader(settings, session_dir=base / "data" / "sessions")
    flask_app = create_app(db, settings, downloader)

    def _run() -> None:
        flask_app.run(host="127.0.0.1", port=8756, threaded=True,
                      debug=False, use_reloader=False)

    threading.Thread(target=_run, daemon=True, name="flask-server").start()
    return 8756
