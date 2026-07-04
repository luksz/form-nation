#!/usr/bin/env bash
# Start the whole form-nation stack: web/API server + Telegram bot.
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8477}"

.venv/bin/uvicorn app.main:app --port "$PORT" --reload &
SERVER=$!

# wait for the API before starting the bot
for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.5
done

.venv/bin/python -m app.telegram_bot &
BOT=$!

echo "form-nation up: web http://127.0.0.1:$PORT | bot polling (Ctrl-C to stop)"
trap 'kill $SERVER $BOT 2>/dev/null' INT TERM
wait
