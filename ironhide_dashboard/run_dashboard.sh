#!/usr/bin/env bash
# Start the Ironhide dashboard detached (setsid + nohup), log to a file, print the URLs.
#
#   ./run_dashboard.sh [--port 8901] [--panel-port 8902] [--python /path/to/python] [--venv .venv] [--foreground]
#
# Python resolution: --python, else $IH_PYTHON, else <repo>/.venv/bin/python (created + requirements installed when
# --venv is given or .venv already exists), else the `python3` on PATH.  Environment passed through / defaulted:
#   IH_LIVE_PORT      panel data server port (default 8902; the Live page's charts load from it in the browser)
#   IH_QUICKDUMP_DIR  built-in 8/28 day (default data/2026-08-28)      IH_ARCHIVE_ROOT  saved flights (default data/archives)
#   IH_SPA_SRC        optional chaos-spa/src for the official grader   IH_NO_SPA=1      force the legacy grader
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${IH_PORT:-8901}"; PANEL="${IH_LIVE_PORT:-8902}"; PY="${IH_PYTHON:-}"; VENV=""; FG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --panel-port) PANEL="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --venv) VENV="${2:-.venv}"; shift 2 ;;
    --foreground) FG=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PY" ]]; then
  if [[ -n "$VENV" || -x "$HERE/.venv/bin/python" ]]; then
    VENV="${VENV:-$HERE/.venv}"
    if [[ ! -x "$VENV/bin/python" ]]; then
      echo "creating venv $VENV and installing requirements.txt"
      python3 -m venv "$VENV"
      "$VENV/bin/python" -m pip install -q -r "$HERE/requirements.txt"
    fi
    PY="$VENV/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi
"$PY" -c "import streamlit, plotly, polars, pymongo, numpy" 2>/dev/null || {
  echo "missing requirements for $PY — run: $PY -m pip install -r requirements.txt   (or ./run_dashboard.sh --venv .venv)" >&2; exit 1; }

export IH_LIVE_PORT="$PANEL"
export IH_QUICKDUMP_DIR="${IH_QUICKDUMP_DIR:-$HERE/data/2026-08-28}"
export IH_ARCHIVE_ROOT="${IH_ARCHIVE_ROOT:-$HERE/data/archives}"
LOG="${IH_LOG:-$HERE/dashboard.log}"
PIDFILE="$HERE/.dashboard.pid"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDFILE")); ./stop_dashboard.sh first" >&2; exit 1
fi

CMD=("$PY" -m streamlit run app.py --server.port "$PORT" --server.address 0.0.0.0 --server.fileWatcherType none --server.headless true)
echo "python: $PY"
echo "log:    $LOG"
echo "env:    IH_LIVE_PORT=$IH_LIVE_PORT IH_QUICKDUMP_DIR=$IH_QUICKDUMP_DIR IH_ARCHIVE_ROOT=$IH_ARCHIVE_ROOT${IH_SPA_SRC:+ IH_SPA_SRC=$IH_SPA_SRC}${IH_NO_SPA:+ IH_NO_SPA=$IH_NO_SPA}"
if [[ $FG -eq 1 ]]; then
  exec "${CMD[@]}"
fi
setsid -f nohup "${CMD[@]}" >"$LOG" 2>&1
sleep 1
PID="$(pgrep -f "streamlit run app.py --server.port $PORT " | head -1 || true)"
[[ -n "$PID" ]] && echo "$PID" >"$PIDFILE"
echo "pid:    ${PID:-?}"

# physical / VPN interfaces only (docker bridges br-* / docker0 are skipped)
for ip in $(ip -4 -o addr show 2>/dev/null | awk '$2!~/^(lo|docker|br-|veth|virbr)/ {split($4,a,"/"); print a[1]}' || hostname -I 2>/dev/null); do
  [[ "$ip" == *:* ]] && continue
  echo "  http://$ip:$PORT   (panel :$PANEL)"
done
if command -v tailscale >/dev/null 2>&1; then
  TS="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  [[ -n "$TS" ]] && echo "  http://$TS:$PORT   (Tailscale; panel :$PANEL)"
fi
echo "stop with: ./stop_dashboard.sh  (or --port $PORT)"
