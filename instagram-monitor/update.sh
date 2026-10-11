#!/usr/bin/env bash
# =====================================================================
#  Ein-Klick-Update fuer den Instagram Monitor (Linux/macOS/Termux).
#  Holt die neueste Version und ersetzt nur die Programmdateien.
#  "data" (Anmeldung/Einstellungen), "downloads" und "logs" bleiben
#  unangetastet.
#
#  Bevorzugt wird "git pull": Das Projekt liegt im PRIVATEN Repo
#  Redurbabat/xred, und ein anonymer ZIP-Download funktioniert dort
#  nicht. Nur wenn der Ordner keine Git-Arbeitskopie ist, wird der
#  ZIP-Weg versucht.
# =====================================================================
set -e
cd "$(dirname "$0")"

ZIP_URL="https://github.com/Redurbabat/xred/archive/refs/heads/main.zip"
ZIP_DIR="xred-main"

echo "Instagram Monitor - Update"

root="$(cd .. && pwd)"
if [ -d "$root/.git" ] && command -v git >/dev/null 2>&1; then
    echo "Git-Arbeitskopie gefunden - hole neueste Aenderungen ..."
    git -C "$root" pull --ff-only
    echo "Fertig - die App ist jetzt aktuell. Starte sie wie gewohnt (z. B. ./start-web-handy.sh)."
    exit 0
fi

echo "Keine Git-Arbeitskopie - versuche ZIP-Download ..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL "$ZIP_URL" -o "$tmp/igm.zip"; then
    echo
    echo "FEHLER: Download nicht moeglich."
    echo "Das Projekt liegt in einem PRIVATEN Repo - ohne Anmeldung gibt es kein ZIP."
    echo "Bitte stattdessen einmalig eine Git-Arbeitskopie anlegen:"
    echo "    git clone https://github.com/Redurbabat/xred.git"
    echo "Danach aktualisiert dieses Skript per 'git pull' von selbst."
    exit 1
fi

echo "Entpacke ..."
unzip -q -o "$tmp/igm.zip" -d "$tmp"

src="$tmp/$ZIP_DIR/instagram-monitor"
echo "Aktualisiere Programmdateien (data, downloads, logs bleiben) ..."
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude data --exclude downloads --exclude logs "$src/" ./
else
    # Fallback ohne rsync: App-Paket + Start-/Projektdateien uebernehmen.
    cp -r "$src/app" ./
    cp -f "$src/"*.py "$src/"*.sh "$src/"*.toml "$src/"*.txt ./ 2>/dev/null || true
fi

echo "Fertig - die App ist jetzt aktuell. Starte sie wie gewohnt (z. B. ./start-web-handy.sh)."
