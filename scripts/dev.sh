#!/bin/bash
# Local preview with SYNTHETIC data only: backend on http://127.0.0.1:8772/speedtest/ plus a simulated agent.
# Phrase for the local entrance: "correct horse battery staple" (dev database only).
set -euo pipefail
cd "$(dirname "$0")/.."
export SPEEDMON_ENV=development SPEEDMON_DATA_DIR="$PWD/var-dev" SPEEDMON_SECURE_COOKIES=0 SPEEDMON_PORT="${PORT:-8772}"
mkdir -p var-dev
[ -f var-dev/agent.key ] || python3 -c "import secrets;print(secrets.token_urlsafe(48))" > var-dev/agent.key
chmod 600 var-dev/agent.key
export SPEEDMON_AGENT_KEY_FILE="$PWD/var-dev/agent.key"
if [ ! -f var-dev/speedmon.sqlite3 ]; then
  .venv/bin/python -m speedmon.cli seed-synthetic --days "${DAYS:-200}"
  .venv/bin/python -c "from speedmon import auth; from speedmon.config import load_settings; auth.write_phrase_hash(load_settings().phrase_file, 'correct horse battery staple')"
fi
SPEEDAGENT_ENV=development SPEEDAGENT_STATE_DIR="$PWD/var-dev/agent" SPEEDAGENT_BACKEND="http://127.0.0.1:$SPEEDMON_PORT/speedtest/agent/v1" \
  SPEEDAGENT_KEY_FILE="$PWD/var-dev/agent.key" SPEEDAGENT_SIMULATE="${SIMULATE:-good}" SPEEDAGENT_ID=synthetic-agent \
  .venv/bin/python -m speedagent &
AGENT=$!
trap 'kill $AGENT 2>/dev/null' EXIT
exec .venv/bin/python -m speedmon.serve
