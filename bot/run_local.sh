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
# Windows (git-bash / MSYS): use taskkill filtered by command line.
if command -v cmd.exe >/dev/null 2>&1; then
  cmd.exe /c "taskkill /F /FI \"IMAGENAME eq python.exe\" /FI \"COMMANDLINE eq *bot/ui.py*\" " >/dev/null 2>&1 || true
fi
# Linux / Oracle: pkill by script path.
pkill -f "bot/ui.py" >/dev/null 2>&1 || true
# Give the OS a moment to release the polling slot.
sleep 1

# load .env if present
if [ -f bot/.env ]; then
  set -a
  . ./bot/.env
  set +a
fi

echo "Starting bot (local)..."
exec ./.venv/Scripts/python.exe bot/ui.py
