#!/usr/bin/env bash
# One command that opens the whole app on one port (used by the Constellation's launcher and by humans):
# installs what is missing, trains the fixtures if absent, builds the UI if stale, records a demo run if none
# exists, then serves the gateway + built control room on 127.0.0.1:${SCW_API_PORT:-5120}.
set -euo pipefail
cd "$(dirname "$0")/.."
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
PORT="${SCW_API_PORT:-5120}"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x .venv/bin/python ] || "$UV" sync --frozen
STATE="${SCW_HOME:-var}"  # the same state directory the Python side uses (src/spectral_canary/paths.py)
[ -f "$STATE/artifacts/v4-hgb-segfilter/manifest.json" ] || .venv/bin/python -m spectral_canary.cli train-fixtures
if [ ! -f web/dist/index.html ] || [ -n "$(find web/src web/index.html web/vite.config.ts -newer web/dist/index.html 2>/dev/null | head -1)" ]; then
  (cd web && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build)
fi
[ -e "$STATE/runs/latest/registry.sqlite" ] || .venv/bin/python -m spectral_canary.cli demo --in-process
exec .venv/bin/python -m spectral_canary.cli serve --port "$PORT"
