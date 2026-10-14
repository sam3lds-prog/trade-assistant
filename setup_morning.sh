#!/usr/bin/env bash
# Schedule the morning email on this Mac: every weekday at 7:00 in this computer's own time zone.
# Usage:  ./setup_morning.sh            install the 7:00 job
#         ./setup_morning.sh --also-45  install a second run at 7:45 too (live option prices after the open)
#         ./setup_morning.sh --remove   remove the jobs again
# The job runs `run.py --morning-email` with this folder's own Python. Test first with:
#         .venv/bin/python run.py --morning-print
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname)" != "Darwin" ]; then
  echo "This script schedules with launchd, which is macOS only. On Linux, use cron:"
  echo "  0 7 * * 1-5  cd $(pwd) && .venv/bin/python run.py --morning-email >> data/logs/morning.log 2>&1"
  exit 1
fi

AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
mkdir -p "$AGENTS" data/logs

remove_job() {
  local label="$1"
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  rm -f "$AGENTS/$label.plist"
}

if [ "${1:-}" = "--remove" ]; then
  remove_job com.tradeassist.morning
  remove_job com.tradeassist.morning45
  echo "Removed the morning email jobs."
  exit 0
fi

[ -x .venv/bin/python ] || { echo "No .venv here. Create it first (python3 -m venv .venv; .venv/bin/pip install -r requirements.txt)."; exit 1; }

write_job() {
  local label="$1" hour="$2" minute="$3"
  cat > "$AGENTS/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$(pwd)/.venv/bin/python</string>
    <string>$(pwd)/run.py</string>
    <string>--morning-email</string>
  </array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$hour</integer>
    <key>Minute</key><integer>$minute</integer>
  </dict>
  <key>StandardOutPath</key><string>$(pwd)/data/logs/morning.log</string>
  <key>StandardErrorPath</key><string>$(pwd)/data/logs/morning.log</string>
</dict></plist>
PLIST
  remove_job_quiet=$(launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true)
  launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist"
  echo "Scheduled $label at $hour:$(printf '%02d' "$minute") local time (weekends skip themselves)."
}

write_job com.tradeassist.morning 7 0
if [ "${1:-}" = "--also-45" ]; then
  write_job com.tradeassist.morning45 7 45
fi

echo
echo "Notes:"
echo "- launchd runs missed jobs when the Mac wakes, but only if it is awake or waking at 7:00. To wake it"
echo "  in time, run once (asks for your password):  sudo pmset repeat wakeorpoweron MTWRF 06:55:00"
echo "- The email needs SMTP_USER and SMTP_PASSWORD in .env (with Gmail, an app password from"
echo "  myaccount.google.com/apppasswords; 2-Step Verification must be on)."
echo "- Try it now without sending:  .venv/bin/python run.py --morning-print"
echo "- The job's output goes to data/logs/morning.log. Remove the jobs with: ./setup_morning.sh --remove"
