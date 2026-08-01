#!/usr/bin/env bash
# ── llama-server control (stock | custom) ──────────────────────────────────
# Starts/stops the Qwen3-8B llama-server on the configured port with IDENTICAL
# flags for both binaries. Never touches the bge-small embedding server (:8081).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$SCRIPT_DIR/config.env"

# Only match the Qwen3-8B server (not the embedding server on :8081)
PATTERN="llama-server --model .*Qwen3-8B-Q3_K_M"

log() { echo -e "\033[1;34m[server_ctl]\033[0m $*"; }

is_running() {
  pgrep -f "$PATTERN" >/dev/null 2>&1
}

port_healthy() {
  curl -sf -m 2 "http://localhost:${PORT}/health" >/dev/null 2>&1
}

status() {
  if is_running; then
    local pid
    pid="$(pgrep -f "$PATTERN" | head -1)"
    echo "llama-server (Qwen3-8B): RUNNING (pid $pid, port $PORT)"
    port_healthy && echo "  health: OK" || echo "  health: FAIL"
  else
    echo "llama-server (Qwen3-8B): not running"
  fi
}

stop() {
  if ! is_running; then
    log "no Qwen3-8B llama-server running — nothing to stop"
    return 0
  fi
  log "stopping llama-server (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))..."
  pkill -f "$PATTERN" 2>/dev/null || true
  for _ in $(seq 1 30); do
    if ! is_running; then
      # give the port a moment to release (health should start failing)
      for __ in $(seq 1 10); do
        if port_healthy; then sleep 0.5; else break; fi
      done
      log "stopped"
      return 0
    fi
    sleep 1
  done
  log "WARN: server still running — sending SIGKILL"
  pkill -9 -f "$PATTERN" 2>/dev/null || true
  sleep 1
}

start() {
  local variant="${1:-}"
  if [[ "$variant" != "stock" && "$variant" != "custom" ]]; then
    echo "usage: $0 start stock|custom" >&2
    return 2
  fi
  if is_running; then
    log "already running — stop first"
    return 1
  fi

  local bin libs
  if [[ "$variant" == "stock" ]]; then
    bin="$STOCK_DIR/build/bin/llama-server"
    libs="$STOCK_DIR/build/bin"
  else
    bin="$CUSTOM_DIR/bin/llama-server"
    libs="$CUSTOM_DIR/lib"
  fi

  if [[ ! -x "$bin" ]]; then
    echo "ERROR: binary not found/executable: $bin" >&2
    return 2
  fi
  if [[ ! -f "$MODEL" ]]; then
    echo "ERROR: model not found: $MODEL" >&2
    return 2
  fi

  mkdir -p "$RESULTS_DIR/logs"
  local logfile="$RESULTS_DIR/logs/llama-server-${variant}.log"
  log "starting ${variant} llama-server on :${PORT} (log: $logfile)"

  LD_LIBRARY_PATH="$libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    nohup "$bin" \
      --model "$MODEL" \
      --host 0.0.0.0 \
      --port "$PORT" \
      "${SERVER_FLAGS[@]}" \
      >"$logfile" 2>&1 &

  # Wait for health (model load on GPU can take a while)
  for i in $(seq 1 120); do
    if port_healthy; then
      log "healthy after ${i}s"
      # Prime CUDA kernels with one warmup completion (not part of any sample)
      curl -sf -m 120 -X POST "http://localhost:${PORT}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"'$MODEL_ID'","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
        >/dev/null 2>&1 || true
      if curl -sf -m 2 "http://localhost:${PORT}/metrics" >/dev/null 2>&1; then
        log "metrics endpoint enabled"
      else
        log "WARN: /metrics not available — server-side cross-check will be skipped"
      fi
      return 0
    fi
    if ! kill -0 "$(pgrep -f "$PATTERN" | head -1)" 2>/dev/null; then
      echo "ERROR: server process died during startup; log tail:" >&2
      tail -25 "$logfile" >&2
      return 1
    fi
    sleep 1
  done
  echo "ERROR: server failed to become healthy within 120s; log tail:" >&2
  tail -25 "$logfile" >&2
  return 1
}

case "${1:-}" in
  start) start "${2:-}" ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 {start stock|custom|stop|status}" >&2; exit 2 ;;
esac
