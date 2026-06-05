#!/usr/bin/env bash
# Update your local nimbo-orchestrator to the latest code from GitHub.
# Safe: it never touches your .env, your projects/, your logs/, or your .venv —
# only the program code is refreshed. Run it from inside the nimbo-orchestrator folder:
#     bash scripts/update.sh
set -euo pipefail

REPO_ZIP="https://github.com/darena724/darena724/archive/refs/heads/claude/music-video-orchestrator-repo-BYdlJ.zip"

# Re-exec from a temp copy so updating this very script mid-run is safe.
if [ "${NIMBO_UPDATE_REEXEC:-}" != "1" ]; then
  PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
  cp "$0" /tmp/nimbo_update_run.sh
  NIMBO_UPDATE_REEXEC=1 exec bash /tmp/nimbo_update_run.sh "$PROJECT_DIR"
fi

PROJECT_DIR="${1:?internal: project dir}"
cd "$PROJECT_DIR"
echo "Updating code in: $PROJECT_DIR"

echo "1/4 downloading latest…"
curl -fSL -o /tmp/nimbo.zip "$REPO_ZIP"

echo "2/4 unpacking…"
rm -rf /tmp/nimbo_update
unzip -q -o /tmp/nimbo.zip -d /tmp/nimbo_update

echo "3/4 copying new code (your .env, projects, logs, and .venv are left untouched)…"
rsync -a \
  --exclude='.env' --exclude='.venv' --exclude='projects/' \
  --exclude='logs/' --exclude='*.mp4' --exclude='*.mp3' --exclude='captions.ass' \
  /tmp/nimbo_update/*/nimbo-orchestrator/ "$PROJECT_DIR/"

echo "4/4 refreshing dependencies…"
if [ -x "$HOME/.local/bin/uv" ]; then
  "$HOME/.local/bin/uv" pip install -e "$PROJECT_DIR" >/dev/null
elif [ -x ".venv/bin/python" ]; then
  ./.venv/bin/python -m pip install -e . >/dev/null
fi

echo ""
echo "✅ Update complete. Restart the app:  ./scripts/start-web.sh"
