#!/usr/bin/env bash
# Keep the trading daemon running on this Mac (3.0): the Trading Desk's jobs, notifications and phone approvals.
# Usage:  ./setup_daemon.sh           install it and start it now (it starts again at login and if it stops)
#         ./setup_daemon.sh --remove  stop it and remove it
#         ./setup_daemon.sh --status  is it running? (its last heartbeat is on the Trading Desk too)
# Test it in a terminal first with:  .venv/bin/python run.py --daemon   (Ctrl-C stops it)
set -euo pipefail
cd "$(dirname "$0")"

LABEL=com.tradeassist.daemon
if [ "$(uname)" != "Darwin" ]; then
  echo "This script uses launchd, which is macOS only. On Linux, run it as a systemd user service:"
  echo "  ExecStart=$(pwd)/.venv/bin/python $(pwd)/run.py --daemon   Restart=always"
  exit 1
fi
case "$(pwd)" in
  *[\&\<\>\"\']*) echo "This folder's path ($(pwd)) contains a character that breaks launchd's XML (& < > \" ')."
     echo "Move or clone the project to a plainer path and run this again."; exit 1;;
esac

AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
PLIST="$AGENTS/$LABEL.plist"
mkdir -p "$AGENTS" data/logs

case "${1:-}" in
  --remove)
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Stopped and removed the trading daemon. The web app runs the desk's jobs itself while it is open."
    exit 0;;
  --status)
    launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "Not installed."
    exit 0;;
esac

[ -x .venv/bin/python ] || { echo "No .venv here. Create it first (python3 -m venv .venv; .venv/bin/pip install -r requirements.txt)."; exit 1; }

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$(pwd)/.venv/bin/python</string>
    <string>$(pwd)/run.py</string>
    <string>--daemon</string>
  </array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(pwd)/data/logs/daemon.log</string>
  <key>StandardErrorPath</key><string>$(pwd)/data/logs/daemon.log</string>
</dict></plist>
PLIST

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "The trading daemon is installed and running. It starts at login and restarts if it stops."
echo "Its log: data/logs/daemon.log. The Trading Desk shows its heartbeat. Remove it with ./setup_daemon.sh --remove"
echo "A Mac that sleeps does not trade: in System Settings > Battery, let it stay awake on power during market hours if you want that."
