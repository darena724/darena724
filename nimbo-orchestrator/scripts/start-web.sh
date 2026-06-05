#!/usr/bin/env bash
# Start the Nimbo Orchestrator local web UI. Double-click friendly on most systems.
set -euo pipefail
cd "$(dirname "$0")/.."

# Prefer the project venv if present.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "Starting Nimbo Orchestrator web UI…"
echo "When it's up, open http://127.0.0.1:8000 in your browser."
exec "$PY" -m web.app
