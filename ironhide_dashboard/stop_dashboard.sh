#!/usr/bin/env bash
# Stop the dashboard started by run_dashboard.sh:  ./stop_dashboard.sh [--port 8901]
# Uses .dashboard.pid when present, else the streamlit process listening for that --server.port.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${IH_PORT:-8901}"
[[ "${1:-}" == "--port" && -n "${2:-}" ]] && PORT="$2"
PIDFILE="$HERE/.dashboard.pid"
PIDS=""
if [[ -f "$PIDFILE" ]]; then PIDS="$(cat "$PIDFILE")"; rm -f "$PIDFILE"; fi
PIDS="$PIDS $(pgrep -f "streamlit run app.py --server.port $PORT " || true)"
PIDS="$(echo $PIDS | tr ' ' '\n' | sort -u | tr '\n' ' ')"
if [[ -z "${PIDS// /}" ]]; then echo "no dashboard running on :$PORT"; exit 0; fi
for p in $PIDS; do kill "$p" 2>/dev/null && echo "stopped pid $p"; done
sleep 1
for p in $PIDS; do kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null && echo "killed pid $p"; done
exit 0
