#!/usr/bin/env bash
# start | stop | status for a Linux box, with or without systemd.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PIDDIR="$ROOT/data"
mkdir -p "$PIDDIR"
RUN_PID="$PIDDIR/sellbird.pid"
IN_PID="$PIDDIR/inbound.pid"
if [[ -x "$ROOT/.venv/bin/sellbird" ]]; then
  BIN="$ROOT/.venv/bin/sellbird"
else
  BIN="$(command -v sellbird)"
fi

have_unit() {
  command -v systemctl >/dev/null 2>&1 && systemctl cat sellbird.service >/dev/null 2>&1
}

start_bg() {
  local name="$1" pidfile="$2"
  shift 2
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  nohup "$@" >>"$PIDDIR/${name}.log" 2>&1 &
  echo $! >"$pidfile"
  echo "$name started (pid $(cat "$pidfile"))"
}

case "${1:-}" in
  start)
    if "$BIN" signals --confirm free; then
      echo "Free signals confirmed so discovery can store what it finds."
    else
      echo "Could not confirm free signals. Discovery may store nothing until you do."
    fi
    if have_unit; then
      systemctl start sellbird.service
      systemctl --no-pager --lines=0 status sellbird.service || true
    else
      start_bg sellbird "$RUN_PID" "$BIN" run
      start_bg inbound "$IN_PID" "$BIN" inbound
    fi
    ;;
  stop)
    if have_unit; then
      systemctl stop sellbird.service
    else
      for pidfile in "$RUN_PID" "$IN_PID"; do
        if [[ -f "$pidfile" ]]; then
          kill "$(cat "$pidfile")" 2>/dev/null || true
          rm -f "$pidfile"
        fi
      done
      echo "stopped"
    fi
    ;;
  status)
    if have_unit; then
      systemctl --no-pager status sellbird.service || true
    else
      for pidfile in "$RUN_PID" "$IN_PID"; do
        if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
          echo "$(basename "$pidfile" .pid) running (pid $(cat "$pidfile"))"
        else
          echo "$(basename "$pidfile" .pid) stopped"
        fi
      done
    fi
    ;;
  *)
    echo "Usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
