#!/usr/bin/env bash
# Foreground process for systemd: inbound webhook in the background, heartbeat in front.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/sellbird" ]]; then
  BIN="$ROOT/.venv/bin/sellbird"
else
  BIN="$(command -v sellbird)"
fi
cleanup() {
  if [[ -n "${INBOUND_PID:-}" ]]; then
    kill "$INBOUND_PID" 2>/dev/null || true
    wait "$INBOUND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM
"$BIN" inbound &
INBOUND_PID=$!
exec "$BIN" run
