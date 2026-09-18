#!/usr/bin/env bash
# Restart the bot locally while keeping the shared PostgreSQL container alive.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.bot-dev.pid"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/bot-dev.log"
SCREEN_NAME="telegram-family-budget-bot-dev"
WORKER_SCREEN_NAME="telegram-family-budget-worker-dev"

mkdir -p "$LOG_DIR"

if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  screen -S "$WORKER_SCREEN_NAME" -X quit >/dev/null 2>&1 || true
fi

if [[ -f "$PID_FILE" ]]; then
  BOT_PID="$(<"$PID_FILE")"
  if kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Stopping local bot process $BOT_PID..."
    kill "$BOT_PID"
    for _ in {1..20}; do
      kill -0 "$BOT_PID" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$PID_FILE"
fi

cd "$ROOT_DIR"
# The deployed URL uses Docker's `postgres` network alias.  A host process
# reaches the very same published database through loopback instead.
set -a
source "$ROOT_DIR/.env"
set +a
export DATABASE_URL="${DATABASE_URL/@postgres:/@127.0.0.1:}"

echo "Applying migrations..."
"$ROOT_DIR/.venv/bin/alembic" upgrade head

echo "Starting local bot; logs: $LOG_FILE"
if command -v screen >/dev/null 2>&1; then
  # `screen` keeps the bot alive after this script (and the calling terminal)
  # finishes. Activation is explicit so development always uses this venv.
  screen -dmS "$SCREEN_NAME" bash -lc "cd '$ROOT_DIR' && source '$ROOT_DIR/.venv/bin/activate' && set -a && source '$ROOT_DIR/.env' && set +a && export DATABASE_URL=\"\${DATABASE_URL/@postgres:/@127.0.0.1:}\" && exec python -m app.main >>'$LOG_FILE' 2>&1"
  screen -dmS "$WORKER_SCREEN_NAME" bash -lc "cd '$ROOT_DIR' && source '$ROOT_DIR/.venv/bin/activate' && set -a && source '$ROOT_DIR/.env' && set +a && export DATABASE_URL=\"\${DATABASE_URL/@postgres:/@127.0.0.1:}\" && exec python -m app.worker >>'$LOG_DIR/worker-dev.log' 2>&1"
  sleep 0.5
  if ! screen -ls 2>/dev/null | grep -q "[.]$SCREEN_NAME"; then
    echo "Bot failed to start. Inspect $LOG_FILE" >&2
    exit 1
  fi
  echo "Bot and worker are running locally in screen sessions. Re-run ./run_dev.sh after code changes."
else
  nohup "$ROOT_DIR/.venv/bin/python" -m app.main >>"$LOG_FILE" 2>&1 < /dev/null &
  BOT_PID=$!
  echo "$BOT_PID" > "$PID_FILE"
  sleep 0.5
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Bot failed to start. Inspect $LOG_FILE" >&2
    exit 1
  fi
  echo "Bot is running locally (PID $BOT_PID). Re-run ./run_dev.sh after code changes."
fi
