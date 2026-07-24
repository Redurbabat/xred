#!/usr/bin/env bash
# =====================================================================
#  Ein-Klick-Update fuer den Instagram Monitor (Linux/macOS/Termux).
#  Laedt die neueste Version von GitHub und ersetzt nur die
#  Programmdateien. "data" (Anmeldung/Einstellungen), "downloads" und
#  "logs" bleiben unangetastet.
# =====================================================================
set -e
cd "$(dirname "$0")"

echo "Instagram Monitor - Update"
echo "Lade neueste Version ..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "https://github.com/Redurbabat/Red/archive/refs/heads/main.zip" -o "$tmp/igm.zip"
echo "Entpacke ..."
unzip -q -o "$tmp/igm.zip" -d "$tmp"

src="$tmp/Red-main/instagram-monitor"
echo "Aktualisiere Programmdateien (data, downloads, logs bleiben) ..."
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude data --exclude downloads --exclude logs "$src/" ./
else
    # Fallback ohne rsync: App-Paket + Start-/Projektdateien uebernehmen.
    cp -r "$src/app" ./
    cp -f "$src/"*.py "$src/"*.sh "$src/"*.toml "$src/"*.txt ./ 2>/dev/null || true
fi

echo "Fertig - die App ist jetzt aktuell. Starte sie wie gewohnt (z. B. ./start-web-handy.sh)."
