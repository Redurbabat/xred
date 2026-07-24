#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
# Ein-Tipp-Start für Android/Termux.
#
# Startet den Instagram-Monitor-Server auf dem Handy und öffnet die
# Oberfläche im Browser. Zusammen mit der App „Termux:Widget" bekommst
# du damit ein Start-Symbol auf dem Home-Bildschirm:
#
#   mkdir -p ~/.shortcuts
#   ln -sf "$PWD/termux-start.sh" ~/.shortcuts/InstagramMonitor
#   chmod +x termux-start.sh
#
# Danach das Termux:Widget auf den Home-Bildschirm legen und
# „InstagramMonitor" antippen. Einrichtung der App selbst: siehe README
# („Nur mit dem Handy – ganz ohne PC").
# =====================================================================
set -e
cd "$(dirname "$(realpath "$0")")"

# Läuft der Server schon? Dann nur den Browser öffnen.
if ! pgrep -f "python.*web\.py" >/dev/null 2>&1; then
    nohup python web.py >/dev/null 2>&1 &
    sleep 4
fi

URL="http://127.0.0.1:8756/"
termux-open-url "$URL" 2>/dev/null \
    || am start -a android.intent.action.VIEW -d "$URL" >/dev/null 2>&1 \
    || echo "Bitte im Browser öffnen: $URL"
