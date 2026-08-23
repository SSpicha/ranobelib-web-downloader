#!/usr/bin/env bash
# Run the Telegram bot locally (Windows git-bash / MSYS).
# Usage:  bash bot/run_local.sh
# Before: copy bot/.env.example -> bot/.env and fill TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
set -e
cd "$(dirname "$0")/.."   # project root (where src/ and bot/ live)

# load .env if present
if [ -f bot/.env ]; then
  set -a
  . ./bot/.env
  set +a
fi

echo "Starting bot (local)..."
exec ./.venv/Scripts/python.exe bot/ui.py
