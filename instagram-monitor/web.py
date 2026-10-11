"""
Einstiegspunkt der Browser-Oberfläche.
======================================

Zwei Betriebsarten:

  python web.py            – nur auf DIESEM PC (http://127.0.0.1:8756/)
  python web.py --handy    – auch im lokalen Netzwerk (WLAN) erreichbar,
                             z. B. vom Handy. Es wird ein Zugangscode
                             erzeugt und die genaue Adresse angezeigt.

Der Handy-Modus bindet den Server an alle Netzwerk-Adressen (0.0.0.0),
schützt den Zugriff aber mit einem Code (nur wer die vollständige Adresse
inkl. ?key=… kennt, kommt rein). So können Geräte im selben WLAN – etwa
dein Handy – die Oberfläche öffnen. Für Zugriff von unterwegs (Mobilfunk)
zusätzlich einen Tunnel-Dienst nutzen; siehe README.

Datenbank, Einstellungen, Download-Ordner und die gespeicherte Anmeldung
werden mit der Desktop-App (main.py) geteilt. (Bitte nicht beide
gleichzeitig laufen lassen.)
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from app.database import Database
from app.downloader import InstagramDownloader
from app.logging_setup import setup_logging
from app.settings import Settings
from app.web import create_app

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DOWNLOAD_DIR = BASE_DIR / "downloads"


def _port_from_env(default: int = 8756) -> int:
    """Port aus der Umgebungsvariable IGMON_PORT (falls gesetzt und gültig)."""
    raw = os.environ.get("IGMON_PORT", "").strip()
    if raw:
        try:
            value = int(raw)
            if 1 <= value <= 65535:
                return value
            logging.getLogger(__name__).warning(
                "IGMON_PORT=%r liegt außerhalb 1–65535 – Standard %d.",
                raw, default)
        except ValueError:
            logging.getLogger(__name__).warning(
                "IGMON_PORT=%r ist keine Zahl – Standard %d.", raw, default)
    return default


# Port der Browser-Oberfläche. Standard 8756; überschreibbar per
# Umgebungsvariable IGMON_PORT (z. B. wenn 8756 schon belegt ist).
PORT = _port_from_env()

logger = logging.getLogger(__name__)


def _lan_ip() -> str:
    """Ermittelt die lokale Netzwerk-Adresse dieses PCs (best effort).

    Nutzt den üblichen Trick, einen UDP-Socket zu einer öffentlichen
    Adresse zu "verbinden" (es werden keine Daten gesendet) und die
    dabei gewählte lokale Adresse auszulesen.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _stable_access_key(data_dir: Path) -> str:
    """Fester Zugangscode für den Handy-/Tunnel-Modus.

    Reihenfolge: Umgebungsvariable ``IGMON_KEY`` (falls gesetzt) → gespeicherte
    Datei ``data/access_key.txt`` → sonst EINMALIG neu erzeugen und speichern.
    So bleibt die Handy-Adresse (``…?key=…``) über Neustarts hinweg GLEICH –
    ein einmal gesetztes Lesezeichen funktioniert dauerhaft.
    """
    env = os.environ.get("IGMON_KEY", "").strip()
    if env:
        return env
    key_file = data_dir / "access_key.txt"
    try:
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(9)
    try:
        key_file.write_text(key, encoding="utf-8")
    except OSError:
        logger.warning("Zugangscode konnte nicht gespeichert werden – er "
                       "ändert sich dann bei jedem Start.")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Instagram Monitor – Browser")
    parser.add_argument(
        "--handy", "--lan", action="store_true", dest="lan",
        help="auch im lokalen Netzwerk (WLAN) erreichbar machen (z. B. Handy)",
    )
    parser.add_argument(
        "--downloads", metavar="ORDNER", default=None,
        help="Download-Ordner festlegen (wird gespeichert; z. B. in Termux: "
             "~/storage/shared/Pictures/InstagramMonitor)",
    )
    args = parser.parse_args(argv)

    for directory in (DATA_DIR, LOG_DIR, DOWNLOAD_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    setup_logging(LOG_DIR / "web.log")
    logger.info("Browser-Oberfläche startet …")

    db = Database(DATA_DIR / "monitor.db")
    settings = Settings(db, default_download_dir=str(DOWNLOAD_DIR))
    if args.downloads:
        # Gewünschten Ordner prüfen und übernehmen (bleibt gespeichert).
        # Praktisch auf dem Handy (Termux), damit Fotos im freigegebenen
        # Speicher landen. resolve(): relativer Pfad soll nicht bei jedem
        # Start von einem anderen Arbeitsverzeichnis abhängen.
        target = Path(args.downloads).expanduser().resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".schreibtest"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            print(f"\nFEHLER: Download-Ordner nicht nutzbar: {target}")
            print(f"        ({exc})")
            print("Tipp (Termux): zuerst 'termux-setup-storage' ausführen "
                  "und den Speicherzugriff erlauben.\n")
            db.close()
            return 2
        settings.download_dir = target
        print(f"\n  Download-Ordner: {target}")
    downloader = InstagramDownloader(settings, session_dir=DATA_DIR / "sessions")
    # Nur der Desktop-Einstiegspunkt aktiviert X. Das Modul importiert
    # Playwright/yt-dlp erst nach einem ausdrücklichen Klick des Nutzers.
    from app.x_service import XService

    x_service = XService(db, settings, session_dir=DATA_DIR / "x_session")

    if args.lan:
        host = "0.0.0.0"
        # Fester Zugangscode: bleibt über Neustarts gleich (data/access_key.txt
        # bzw. Umgebungsvariable IGMON_KEY) → das Handy-Lesezeichen funktioniert
        # dauerhaft.
        access_key = _stable_access_key(DATA_DIR)
        lan_ip = _lan_ip()
        phone_url = f"http://{lan_ip}:{PORT}/?key={access_key}"
        local_url = f"http://127.0.0.1:{PORT}/"
        print("\n" + "=" * 60)
        print("  HANDY-MODUS – im WLAN bzw. über Tailscale erreichbar")
        print("=" * 60)
        print("  Auf dem Handy diese Adresse öffnen (bleibt dauerhaft gleich):\n")
        print(f"     {phone_url}\n")
        print("  Über Tailscale von überall: dieselbe Adresse, aber die")
        print(f"  100.x.y.z-Adresse dieses PCs statt {lan_ip} verwenden.")
        print("  (Tipp: als Lesezeichen / zum Home-Bildschirm hinzufuegen.)")
        print(f"  Auf diesem PC:  {local_url}")
        print("  Beenden mit Strg+C in diesem Fenster.")
        print("=" * 60 + "\n")
        open_url = local_url
    else:
        host = "127.0.0.1"
        access_key = None
        open_url = f"http://127.0.0.1:{PORT}/"
        print(f"\n  Instagram Monitor läuft im Browser: {open_url}")
        print("  (Nur auf diesem PC. Fürs Handy: 'web.py --handy' bzw. "
              "start-web-handy starten.)")
        print("  Beenden mit Strg+C in diesem Fenster.\n")

    app = create_app(
        db, settings, downloader, access_key=access_key, x_service=x_service
    )

    # Browser auf dem PC kurz nach dem Serverstart öffnen (best effort).
    threading.Timer(1.0, lambda: webbrowser.open(open_url)).start()

    try:
        # threaded=True: Vorschaubilder/Streams laden parallel; die
        # Instaloader-Zugriffe selbst serialisiert der Downloader-Lock.
        app.run(host=host, port=PORT, threaded=True, debug=False)
    finally:
        db.close()
        logger.info("Browser-Oberfläche beendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
