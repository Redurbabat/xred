@echo off
REM Einmalige Chromium-Einrichtung fuer die sichtbare lokale X-Sitzung.
cd /d "%~dp0"

set "BASEPY="
where py >nul 2>nul && set "BASEPY=py"
if not defined BASEPY where python >nul 2>nul && set "BASEPY=python"
if not defined BASEPY (
    echo Python 3.12 oder neuer wurde nicht gefunden.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Erstelle virtuelle Umgebung ...
    %BASEPY% -m venv .venv
    if errorlevel 1 goto :error
)
set "PYCMD=.venv\Scripts\python.exe"

echo Installiere Python-Abhaengigkeiten ...
"%PYCMD%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Installiere Playwright Chromium einmalig ...
"%PYCMD%" -m playwright install chromium
if errorlevel 1 goto :error

echo.
echo X-Browser ist eingerichtet. Starte jetzt start-web.bat.
pause
exit /b 0

:error
echo.
echo Einrichtung fehlgeschlagen. Bitte Internetverbindung und Python pruefen.
pause
exit /b 1
