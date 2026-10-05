#!/usr/bin/env bash
# Start AgriSense: the simulation engine and the dashboard on one port.
#
#   ./run.sh                 # 0.0.0.0:8000, 60x simulation speed
#   ./run.sh 8080            # different port
#   SPEED=300 ./run.sh       # faster clock
#   RELOAD=1 ./run.sh        # auto-reload for development

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
PORT="${PORT:-${1:-8000}}"
HOST="${HOST:-0.0.0.0}"

if ! "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "installing dependencies..."
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
fi

export AGRISENSE_PORT="$PORT"
export AGRISENSE_HOST="$HOST"

echo "AgriSense -> http://localhost:${PORT}/"
cd backend
exec "$PY" -m uvicorn app.main:app \
  --host "$HOST" --port "$PORT" \
  --log-level info \
  $([ "${RELOAD:-0}" = "1" ] && echo --reload)