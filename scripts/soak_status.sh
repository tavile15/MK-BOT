#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/mk-bot-ia/MK-BOT-IA}"
cd "$PROJECT_DIR"

PID_FILE="runtime/soak_executor.pid"
LOG_FILE="runtime/soak_executor.log"

if [[ ! -f "$PID_FILE" ]]; then
  echo "status=stopped"
  [[ -f "$LOG_FILE" ]] && echo "log=$LOG_FILE"
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "status=running"
  echo "pid=$PID"
  [[ -f "$LOG_FILE" ]] && echo "log=$LOG_FILE"
  exit 0
fi

echo "status=stopped_stale_pid"
echo "pid=${PID:-none}"
[[ -f "$LOG_FILE" ]] && echo "log=$LOG_FILE"
