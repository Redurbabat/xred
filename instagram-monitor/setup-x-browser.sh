#!/usr/bin/env bash
# Einmalige Chromium-Einrichtung für die sichtbare lokale X-Sitzung.
set -e
cd "$(dirname "$0")"

BASE_PY="$(command -v python3.12 || command -v python3 || command -v python || true)"
if [ -z "$BASE_PY" ]; then
    echo "Python 3.12 oder neuer wurde nicht gefunden."
    exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
    echo "Erstelle virtuelle Umgebung …"
    "$BASE_PY" -m venv .venv
fi
PY=".venv/bin/python"

echo "Installiere Python-Abhängigkeiten …"
"$PY" -m pip install -r requirements.txt
echo "Installiere Playwright Chromium einmalig …"
"$PY" -m playwright install chromium
echo "X-Browser ist eingerichtet. Starte jetzt ./start-web.sh."
