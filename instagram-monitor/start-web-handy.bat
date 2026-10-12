@echo off
REM ============================================================
REM  Instagram Monitor - BROWSER-Oberflaeche im HANDY-MODUS
REM  Macht die Seite im lokalen WLAN erreichbar (z. B. Handy)
REM  und zeigt die genaue Adresse inkl. Zugangscode an.
REM  Handy und PC muessen im GLEICHEN WLAN sein.
REM ============================================================

cd /d "%~dp0"

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
    echo.
    echo Python wurde nicht gefunden.
    echo Bitte Python 3.12 oder neuer installieren:
    echo    https://www.python.org/downloads/
    echo Wichtig: im Installer den Haken "Add python.exe to PATH" setzen,
    echo danach dieses Skript erneut starten.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Erste Einrichtung: virtuelle Umgebung wird angelegt ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo Anlegen der virtuellen Umgebung ist fehlgeschlagen.
        pause
        exit /b 1
    )
)

echo Abhaengigkeiten werden geprueft ...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo Installation der Abhaengigkeiten ist fehlgeschlagen.
    echo Bitte Internetverbindung pruefen und erneut versuchen.
    pause
    exit /b 1
)

REM Chromium fuer die X-Videosuche. Playwright installiert nur, was fehlt -
REM ist es schon da, ist der Aufruf in einer Sekunde durch. Vorher lief der
REM Start ohne Browser durch und die X-Suche scheiterte erst beim Klicken,
REM mit einem Hinweis auf "python -m playwright install", der ins falsche
REM Python gezeigt haette.
echo Chromium fuer die X-Suche wird geprueft ...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo HINWEIS: Chromium konnte nicht geholt werden. Instagram funktioniert
    echo trotzdem; die X-VIDEOSUCHE bleibt bis dahin ohne Browser. Spaeter
    echo einfach setup-x-browser.bat ausfuehren.
    echo.
)

echo.
echo HINWEIS: Beim ersten Start fragt Windows evtl. nach der Firewall-
echo Freigabe fuer Python. Bitte "Zugriff zulassen" (privates Netzwerk)
echo waehlen, damit das Handy die Seite erreichen kann.
echo.

".venv\Scripts\python.exe" web.py --handy
if errorlevel 1 pause
