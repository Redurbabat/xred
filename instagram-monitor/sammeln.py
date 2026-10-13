"""
Alle bereits geladenen X-Videos in den Sammelordner bringen.
===========================================================

Zum Doppelklicken gedacht (``sammeln.bat`` bzw. ``sammeln.sh``). Das
Programm legt für jedes schon heruntergeladene X-Video einen Eintrag im
Ordner ``X/Alle/`` an – dort liegt dann alles beieinander, statt verteilt
auf Unterordner je Suchbegriff, Liste und Urheber.

Es wird eine HARTE VERKNÜPFUNG angelegt: Die Datei liegt weiterhin nur
einmal auf der Platte und ist nur an zwei Stellen sichtbar. Es wird also
nichts verschoben, nichts kopiert und kein zusätzlicher Speicherplatz
belegt. Löschst du im Sammelordner, bleibt das Original erhalten.

Die Browser-Oberfläche muss dafür NICHT laufen. Mehrfaches Ausführen
schadet nicht: Was schon drin ist, wird übersprungen.
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.database import Database  # noqa: E402  (erst nach sys.path)
from app.settings import Settings  # noqa: E402
from app.x_service import XService  # noqa: E402

DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = BASE_DIR / "downloads"


def main() -> int:
    print()
    print("  Videos im Sammelordner zusammenführen")
    print("  =====================================")

    if not (DATA_DIR / "monitor.db").exists():
        print()
        print("  Es gibt noch keine Datenbank – wurde die App hier schon einmal")
        print(f"  gestartet? Erwartet: {DATA_DIR / 'monitor.db'}")
        return 1

    db = Database(DATA_DIR / "monitor.db")
    try:
        settings = Settings(db, default_download_dir=str(DOWNLOAD_DIR))
        service = XService(db, settings, session_dir=DATA_DIR / "x_session")
        ziel = settings.download_dir / "X" / "Alle"
        print(f"\n  Download-Ordner: {settings.download_dir}")
        print(f"  Sammelordner:    {ziel}")
        print("\n  Wird durchsucht … (bei vielen Dateien kann das dauern)")

        ergebnis = service.rebuild_all_folder()
    finally:
        db.close()

    neu = ergebnis["verknuepft"] + ergebnis["kopiert"]
    print()
    print(f"  Geprüfte Videos:        {ergebnis['geprueft']}")
    print(f"  Neu im Sammelordner:    {neu}")
    print(f"  Waren schon drin:       {ergebnis['vorhanden']}")
    if ergebnis["kopiert"]:
        print(f"    davon kopiert:        {ergebnis['kopiert']}  (siehe Hinweis unten)")
    if ergebnis["fehler"]:
        print(f"  Nicht möglich:          {ergebnis['fehler']}")

    if ergebnis["geprueft"] == 0:
        print()
        print("  Es wurden keine X-Videos gefunden. Liegt der Download-Ordner")
        print("  woanders? Er lässt sich in der App unter Einstellungen ändern.")
    elif ergebnis["kopiert"]:
        print()
        print("  Hinweis: Einige Dateien mussten KOPIERT werden, weil das")
        print("  Dateisystem keine harten Verknüpfungen erlaubt (z. B. FAT32")
        print("  auf einem USB-Stick). Diese Kopien belegen echten Platz.")
    else:
        print()
        print("  Fertig. Der Sammelordner belegt keinen zusätzlichen Platz –")
        print("  die Dateien liegen weiterhin nur einmal auf der Platte.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n  Abgebrochen.")
        code = 1
    except Exception as exc:                 # noqa: BLE001 – letzte Rettung
        print(f"\n  FEHLER: {exc}")
        code = 1
    sys.exit(code)
