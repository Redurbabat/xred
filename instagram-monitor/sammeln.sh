#!/usr/bin/env bash
# =====================================================================
#  Alle schon geladenen X-Videos im Ordner "X/Alle" zusammenführen.
#  Die App muss dafür NICHT laufen.
#
#  Es wird nichts verschoben und nichts gelöscht: Jede Datei bekommt nur
#  zusätzlich einen Eintrag im Sammelordner (harte Verknüpfung), belegt
#  also keinen weiteren Speicherplatz.
# =====================================================================
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    # Noch keine Umgebung: System-Python reicht hier aus, denn dieses
    # Programm braucht nur die mitgelieferten Dateien.
    PY="$(command -v python3.12 || command -v python3 || true)"
    if [ -z "$PY" ]; then
        echo "Python wurde nicht gefunden. Bitte zuerst ./start-web.sh einmal"
        echo "ausführen – das richtet alles ein."
        exit 1
    fi
fi

"$PY" sammeln.py
