@echo off
REM =====================================================================
REM  Ein-Klick-Update fuer den Instagram Monitor (Windows).
REM  Laedt die neueste Version von GitHub und ersetzt nur die
REM  Programmdateien. Deine Anmeldung und Einstellungen (Ordner "data")
REM  bleiben erhalten, ebenso "downloads" und "logs" - es wird NICHTS von
REM  dir kopiert oder geloescht. Einfach diese Datei per Doppelklick starten.
REM =====================================================================
setlocal
cd /d "%~dp0"

set "ZIP=%TEMP%\igmonitor_update.zip"
set "NEW=%TEMP%\igmonitor_update"

echo.
echo   Instagram Monitor - Update
echo   ==========================
echo   Lade neueste Version von GitHub ...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/RedBbt/insta-x/archive/refs/heads/main.zip' -OutFile '%ZIP%' } catch { exit 1 }"
if errorlevel 1 (
  echo   FEHLER: Download fehlgeschlagen - besteht eine Internetverbindung?
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
robocopy "%NEW%\insta-x-main\instagram-monitor" "%~dp0." /E /XD data downloads logs /NFL /NDL /NJH /NJS /NP >nul

del "%ZIP%" >nul 2>&1
rmdir /s /q "%NEW%" >nul 2>&1

echo.
echo   Fertig - die App ist jetzt aktuell.
echo   Starte sie wieder wie gewohnt: start-web-handy.bat (oder start-web.bat).
echo.
pause
