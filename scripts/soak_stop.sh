#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/mk-bot-ia/MK-BOT-IA}"
cd "$PROJECT_DIR"

PID_FILE="runtime/soak_executor.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Nao ha PID file. Soak provavelmente ja esta parado."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${PID:-}" ]]; then
  rm -f "$PID_FILE"
  echo "PID vazio. Limpando PID file."
  exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  sleep 1
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" || true
  fi
  echo "SOAK_STOP_OK pid=$PID"
else
  echo "Processo nao estava ativo (pid=$PID)."
fi

rm -f "$PID_FILE"
