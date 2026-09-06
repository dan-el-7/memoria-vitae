#!/usr/bin/env bash
# Visual Memory Agent launcher for Linux/macOS (Windows: run_desktop.bat).
# Optional environment overrides:
#   VMA_HOST (default 127.0.0.1)  VMA_PORT (default 8619)  VMA_DATA_DIR (default ./data)
#   VMA_RELAY_URL / VMA_RELAY_REG_TOKEN — force online mode for this run
#     (an empty VMA_RELAY_URL disables online mode without touching config.toml)
set -euo pipefail
cd "$(dirname "$0")/desktop"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Desktop Python environment was not found: $(pwd)/.venv"
  echo "Create it once with:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -e . qrcode psutil uvicorn"
  exit 1
fi

# ---- online mode ask (asked once; persisted via config.toml) ----
# Skipped when VMA_RELAY_URL is exported or a relay is already configured.
if [ -z "${VMA_RELAY_URL:-}" ] && ! "$PY" -m vma.set_relay --check >/dev/null 2>&1; then
  "$PY" -m vma.set_relay --wizard
fi

echo "Starting Visual Memory Agent at http://${VMA_HOST:-127.0.0.1}:${VMA_PORT:-8619}"
echo "Press Ctrl+C to stop the desktop server."
exec "$PY" -m uvicorn vma.app:app --host "${VMA_HOST:-127.0.0.1}" --port "${VMA_PORT:-8619}"
