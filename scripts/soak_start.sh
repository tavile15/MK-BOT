#!/usr/bin/env bash
set -euo pipefail

# Start do soak em background (sem depender de navegador aberto).

PROJECT_DIR="${PROJECT_DIR:-/opt/mk-bot-ia/MK-BOT-IA}"
VENV_DIR="${VENV_DIR:-/opt/mk-bot-ia/.venv}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env.testnet}"
SYMBOL="${SOAK_SYMBOL:-BTCUSDT}"
INTERVAL_SEC="${SOAK_INTERVAL_SEC:-15}"
MAX_CYCLES="${SOAK_MAX_CYCLES:-1440}" # 6h com 15s
FORCE_HEURISTIC="${SOAK_FORCE_HEURISTIC:-1}"
RELAX_LIQUIDITY="${SOAK_RELAX_LIQUIDITY:-0}"

cd "$PROJECT_DIR"
mkdir -p runtime auditoria/testnet

PID_FILE="runtime/soak_executor.pid"
LOG_FILE="runtime/soak_executor.log"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
JSONL_FILE="${SOAK_JSONL_OUT:-auditoria/testnet/soak_cloud_${SYMBOL,,}_${STAMP}.jsonl}"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "Ja existe soak rodando (pid=$PID). Pare antes com scripts/soak_stop.sh."
    exit 1
  fi
  rm -f "$PID_FILE"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Arquivo de ambiente nao encontrado: $ENV_FILE"
  exit 1
fi

source "$VENV_DIR/bin/activate"
set -a
source "$ENV_FILE"
set +a
export PYTHONPATH=src

CMD=(
  python -m microtrans.binance_testnet_cli executor-loop
  --symbol "$SYMBOL"
  --interval-sec "$INTERVAL_SEC"
  --max-cycles "$MAX_CYCLES"
  --jsonl-out "$JSONL_FILE"
)

if [[ "$FORCE_HEURISTIC" == "1" ]]; then
  CMD+=(--force-heuristic)
fi

if [[ "$RELAX_LIQUIDITY" == "1" ]]; then
  CMD+=(--relax-liquidity)
else
  CMD+=(--no-relax-liquidity)
fi

# Argumentos extras opcionais (ex.: --max-notional-quote-per-order 10)
if [[ "$#" -gt 0 ]]; then
  CMD+=("$@")
fi

nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

echo "SOAK_START_OK"
echo "pid=$PID"
echo "jsonl=$JSONL_FILE"
echo "log=$LOG_FILE"
