@echo off
REM =====================================================================
REM  Alle schon geladenen X-Videos im Ordner "X\Alle" zusammenfuehren.
REM  Einfach per Doppelklick starten - die App muss NICHT laufen.
REM
REM  Es wird nichts verschoben und nichts geloescht: Jede Datei bekommt
REM  nur zusaetzlich einen Eintrag im Sammelordner (harte Verknuepfung),
REM  belegt also keinen weiteren Speicherplatz.
REM =====================================================================
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYCMD=.venv\Scripts\python.exe"
) else (
    REM Noch keine Umgebung: System-Python reicht hier aus, denn dieses
    REM Programm braucht nur die mitgelieferten Dateien.
    set "PYCMD="
    where py >nul 2>nul && set "PYCMD=py"
    if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
    if not defined PYCMD (
        echo.
        echo Python wurde nicht gefunden. Bitte zuerst start-web.bat einmal
        echo ausfuehren - das richtet alles ein.
        echo.
        pause
        exit /b 1
    )
)

"%PYCMD%" sammeln.py
echo.
pause
