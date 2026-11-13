#!/usr/bin/env bash
# The self-hosted MiroFish backend for one-click Research (4.5.2): the official open-source repository at the pinned,
# reviewed commit, its own Python environment and its own .env, listening on this computer only.
#
# Usage:  ./setup_mirofish.sh install   clone github.com/666ghj/MiroFish at the pinned commit, install its backend, make its .env
#         ./setup_mirofish.sh start     start the backend in the background (127.0.0.1:5001); log in data/logs/mirofish.log
#         ./setup_mirofish.sh stop      stop it
#         ./setup_mirofish.sh restart   stop, then start
#         ./setup_mirofish.sh status    is it running, and does it answer /health?
#         ./setup_mirofish.sh logs      the last lines of its log (logs -f to follow)
#         ./setup_mirofish.sh check     the app's free readiness check (python run.py --scenario-check)
#
# MIROFISH_DIR chooses where it lives (default: a MiroFish folder beside this one). Its language-model and Zep keys go
# in MIROFISH_DIR/.env, never in Trade Assistant's .env; Trade Assistant sends it only the shareable evidence packet.
# Docker is not needed. Needs: git, and uv (https://docs.astral.sh/uv/; `brew install uv`), which fetches the Python
# MiroFish needs (3.11 or 3.12) by itself. Node is only for MiroFish's own web dashboard, which is not used.
set -euo pipefail
cd "$(dirname "$0")"

PIN=7657031ac01184afe2cb220f5ee3545573b5e843
REPO=https://github.com/666ghj/MiroFish.git
HERE="$(pwd -P)"
DIR="${MIROFISH_DIR:-$(dirname "$HERE")/MiroFish}"
PORT="${MIROFISH_PORT:-5001}"
LOG="$HERE/data/logs/mirofish.log"
PIDFILE="$HERE/data/mirofish.pid"
mkdir -p "$HERE/data/logs"

need() { command -v "$1" >/dev/null 2>&1 || { echo "$1 is not installed. $2"; exit 1; }; }

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

health() { curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null; }

install() {
  need git "Install the Xcode command line tools: xcode-select --install"
  need uv "Install it with: brew install uv   (or see https://docs.astral.sh/uv/)"
  if [ -d "$DIR/.git" ]; then
    echo "MiroFish is already at $DIR."
  else
    echo "Cloning the official MiroFish into ${DIR}..."
    git clone --quiet "$REPO" "$DIR"
  fi
  git -C "$DIR" fetch --quiet origin "$PIN" 2>/dev/null || git -C "$DIR" fetch --quiet origin
  git -C "$DIR" checkout --quiet "$PIN"
  echo "At the reviewed commit ${PIN:0:7} (Trade Assistant is tested against this one; upgrade only after a review)."
  echo "Installing its backend (uv sync; uv brings Python 3.12 if this Mac lacks it)..."
  (cd "$DIR/backend" && uv sync --quiet --python 3.12)
  if [ ! -f "$DIR/.env" ]; then
    # MiroFish's example ends with an optional "boost" model block that must be absent unless used: it is left out.
    sed '/LLM_BOOST/d' "$DIR/.env.example" > "$DIR/.env"
    printf '\n# Trade Assistant: listen on this computer only (MiroFish defaults to 0.0.0.0, every network).\nFLASK_HOST=127.0.0.1\nFLASK_PORT=%s\n' "$PORT" >> "$DIR/.env"
    chmod 600 "$DIR/.env"
    echo "Made $DIR/.env. Edit it now: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME and ZEP_API_KEY."
  else
    grep -q '^FLASK_HOST=127.0.0.1' "$DIR/.env" || echo "WARNING: $DIR/.env has no FLASK_HOST=127.0.0.1: add it, or MiroFish listens on every network."
  fi
  echo "Then: ./setup_mirofish.sh start   and in Trade Assistant's .env: SCENARIO_PROVIDER=mirofish, MIROFISH_URL=http://127.0.0.1:$PORT"
}

start() {
  [ -d "$DIR/backend" ] || { echo "MiroFish is not installed at $DIR: run ./setup_mirofish.sh install first."; exit 1; }
  if running; then echo "MiroFish is already running (pid $(cat "$PIDFILE"))."; return; fi
  grep -q '^FLASK_HOST=127.0.0.1' "$DIR/.env" 2>/dev/null || { echo "Refusing to start: $DIR/.env must have FLASK_HOST=127.0.0.1 (this computer only)."; exit 1; }
  grep -Eq '^LLM_API_KEY=.+' "$DIR/.env" && ! grep -q '^LLM_API_KEY=your_api_key_here' "$DIR/.env" \
    || { echo "Set LLM_API_KEY (and LLM_BASE_URL, LLM_MODEL_NAME, ZEP_API_KEY) in $DIR/.env first."; exit 1; }
  echo "Starting MiroFish's backend on 127.0.0.1:$PORT (log: $LOG)..."
  (cd "$DIR/backend" && FLASK_HOST=127.0.0.1 FLASK_PORT="$PORT" nohup uv run python run.py >> "$LOG" 2>&1 & echo $! > "$PIDFILE")
  for _ in $(seq 1 30); do
    if health >/dev/null; then echo "Up: $(health)"; return; fi
    running || { echo "It stopped while starting. The end of its log:"; tail -n 20 "$LOG"; exit 1; }
    sleep 1
  done
  echo "Started (pid $(cat "$PIDFILE")) but /health has not answered yet; see ./setup_mirofish.sh logs"
}

stop() {
  if running; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    for _ in $(seq 1 10); do running || break; sleep 1; done
    running && kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true
    echo "Stopped MiroFish."
  else
    echo "MiroFish is not running (from this script)."
  fi
  rm -f "$PIDFILE"
}

status() {
  if running; then echo "Running (pid $(cat "$PIDFILE")) from $DIR"; else echo "Not running from this script."; fi
  if h=$(health); then echo "/health: $h"; else echo "/health: no answer on 127.0.0.1:$PORT"; fi
  [ -d "$DIR/.git" ] && echo "Commit: $(git -C "$DIR" rev-parse --short HEAD) (pinned ${PIN:0:7})"
}

case "${1:-}" in
  install) install ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) if [ "${2:-}" = "-f" ]; then tail -f "$LOG"; else tail -n 60 "$LOG"; fi ;;
  check) exec .venv/bin/python run.py --scenario-check "${2:-SPY}" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
