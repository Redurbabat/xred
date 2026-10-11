@echo off
REM =====================================================================
REM  Ein-Klick-Update fuer den Instagram Monitor (Windows).
REM  Holt die neueste Version und ersetzt nur die Programmdateien.
REM  Deine Anmeldung und Einstellungen (Ordner "data") bleiben erhalten,
REM  ebenso "downloads" und "logs" - es wird NICHTS von dir kopiert oder
REM  geloescht. Einfach diese Datei per Doppelklick starten.
REM
REM  Bevorzugt wird "git pull": Das Projekt liegt im PRIVATEN Repo
REM  Redurbabat/xred, und ein anonymer ZIP-Download funktioniert dort
REM  nicht. Nur wenn der Ordner keine Git-Arbeitskopie ist, wird der
REM  ZIP-Weg versucht.
REM =====================================================================
setlocal
cd /d "%~dp0"

set "ZIP=%TEMP%\igmonitor_update.zip"
set "NEW=%TEMP%\igmonitor_update"
set "ZIPURL=https://github.com/Redurbabat/xred/archive/refs/heads/main.zip"

echo.
echo   Instagram Monitor - Update
echo   ==========================

REM --- Weg 1: Git-Arbeitskopie (funktioniert auch beim privaten Repo) ---
if exist "..\.git" (
  where git >nul 2>nul
  if not errorlevel 1 (
    echo   Git-Arbeitskopie gefunden - hole neueste Aenderungen ...
    git -C ".." pull --ff-only
    if errorlevel 1 (
      echo.
      echo   FEHLER: 'git pull' ist fehlgeschlagen.
      echo   Meist liegt es an lokalen Aenderungen oder einer fehlenden Anmeldung.
      pause
      exit /b 1
    )
    echo.
    echo   Fertig - die App ist jetzt aktuell.
    echo   Starte sie wieder wie gewohnt: start-web-handy.bat ^(oder start-web.bat^).
    echo.
    pause
    exit /b 0
  )
)

REM --- Weg 2: ZIP-Download ---
echo   Keine Git-Arbeitskopie - versuche ZIP-Download ...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%ZIPURL%' -OutFile '%ZIP%' } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo   FEHLER: Download nicht moeglich.
  echo   Das Projekt liegt in einem PRIVATEN Repo - ohne Anmeldung gibt es kein ZIP.
  echo   Bitte stattdessen einmalig eine Git-Arbeitskopie anlegen:
  echo       git clone https://github.com/Redurbabat/xred.git
  echo   Danach aktualisiert dieses Skript per 'git pull' von selbst.
  pause
  exit /b 1
)

echo   Entpacke ...
if exist "%NEW%" rmdir /s /q "%NEW%"
powershell -NoProfile -Command "try { Expand-Archive -Path '%ZIP%' -DestinationPath '%NEW%' -Force } catch { exit 1 }"
if errorlevel 1 (
  echo   FEHLER: Entpacken fehlgeschlagen.
  pause
  exit /b 1
)

echo   Aktualisiere Programmdateien ^(data, downloads und logs bleiben^) ...
robocopy "%NEW%\xred-main\instagram-monitor" "%~dp0." /E /XD data downloads logs /NFL /NDL /NJH /NJS /NP >nul

del "%ZIP%" >nul 2>&1
rmdir /s /q "%NEW%" >nul 2>&1

echo.
echo   Fertig - die App ist jetzt aktuell.
echo   Starte sie wieder wie gewohnt: start-web-handy.bat ^(oder start-web.bat^).
echo.
pause
