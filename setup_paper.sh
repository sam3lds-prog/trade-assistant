#!/usr/bin/env bash
# Keep Trade Assistant running on this Mac (2.15), so paper portfolios are valued through the trading day and the
# morning session's rescans and the news digest keep running, even with no browser open.
# Usage:  ./setup_paper.sh            start the app now and whenever you log in; restart it if it stops
#         ./setup_paper.sh --remove   stop it and remove the job
# The app then answers at http://127.0.0.1:8000 as usual. Do not also start it by hand (the port would be taken).
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname)" != "Darwin" ]; then
  echo "This script uses launchd, which is macOS only. On Linux, a systemd user service does the same:"
  echo "  ExecStart=$(pwd)/.venv/bin/python $(pwd)/run.py   WorkingDirectory=$(pwd)   Restart=always"
  exit 1
fi

case "$(pwd)" in
  *[\&\<\>\"\']*) echo "This folder's path ($(pwd)) contains a character that breaks launchd's XML (& < > \" ')."
     echo "Move or clone the project to a plainer path and run this again."; exit 1;;
esac

AGENTS="$HOME/Library/LaunchAgents"
LABEL=com.tradeassist.app
UID_NUM="$(id -u)"
mkdir -p "$AGENTS" data/logs

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$AGENTS/$LABEL.plist"
  echo "Stopped the app and removed the job. Start it by hand again with: .venv/bin/python run.py"
  exit 0
fi

[ -x .venv/bin/python ] || { echo "No .venv here. Create it first (python3 -m venv .venv; .venv/bin/pip install -r requirements.txt)."; exit 1; }

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo "Something (the app started by hand?) is already using port 8000. Stop it first (Ctrl+C where it runs), then run this again."
  exit 1
fi

cat > "$AGENTS/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$(pwd)/.venv/bin/python</string>
    <string>$(pwd)/run.py</string>
  </array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$(pwd)/data/logs/app.log</string>
  <key>StandardErrorPath</key><string>$(pwd)/data/logs/app.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$LABEL.plist"
echo "Trade Assistant now runs in the background at http://127.0.0.1:8000, starts when you log in, and restarts if it stops."
echo
echo "Notes:"
echo "- A sleeping Mac runs nothing. To keep it awake on market days, in System Settings > Displays > Advanced"
echo "  (or Battery > Options on a laptop), turn on \"Prevent automatic sleeping when the display is off\" while"
echo "  on power, or wake it before the open:  sudo pmset repeat wakeorpoweron MTWRF 07:00:00 (your local time)."
echo "  When it does sleep, paper portfolios catch up from price history when it wakes."
echo "- After ./update.sh, restart it:  launchctl kickstart -k gui/$UID_NUM/$LABEL"
echo "- The app's log goes to data/logs/app.log. Stop it and remove the job with: ./setup_paper.sh --remove"
