#!/usr/bin/env bash
# Run the Telegram bot locally (Windows git-bash / MSYS / Linux).
# Usage:  bash bot/run_local.sh
# Before: copy bot/.env.example -> bot/.env and fill TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
#
# This is the ONE canonical launcher. It kills any existing bot/ui.py instance
# BEFORE starting a new one, so restarts never accumulate duplicate processes
# (which would cause TelegramConflictError).
set -e
cd "$(dirname "$0")/.."   # project root (where src/ and bot/ live)

# --- kill any running bot/ui.py instance (single-instance enforcement) ---
# Windows (git-bash / MSYS): list python processes via wmic, filter by command
# line in bash (avoids the quoted-WHERE mangling that breaks on Win11).
if command -v cmd.exe >/dev/null 2>&1; then
  pids=$(cmd.exe /c "wmic process where name='python.exe' get processid,commandline" 2>/dev/null \
         | tr -d '\r' | grep -E "python(\.exe)?\"? +.*bot/ui\.py" | grep -Eo '[0-9]+ *$' | tr -d ' ' || true)
  for p in $pids; do
    taskkill /F /PID "$p" >/dev/null 2>&1 || true
  done
fi
# Linux / Oracle: pkill by interpreter+script path (anchored to avoid editors/grep).
pkill -f "[p]ython.*bot/ui.py" >/dev/null 2>&1 || true
# Give the OS a moment to release the polling slot.
sleep 1

# load .env if present
if [ -f bot/.env ]; then
  set -a
  . ./bot/.env
  set +a
fi

# Pick the venv interpreter (Windows vs Linux layout).
PY="./.venv/Scripts/python.exe"
if [ -x ./.venv/bin/python ]; then
  PY="./.venv/bin/python"
fi

echo "Starting bot (local)..."
exec "$PY" bot/ui.py
