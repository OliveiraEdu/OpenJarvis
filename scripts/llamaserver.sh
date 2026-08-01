#!/usr/bin/env bash
# ── OpenJarvis llama-server management CLI ────────────────────────────────────
# Manages the single host-side llama-server that OpenJarvis depends on
# (Qwen3-8B, port 8080, reached from the container via host.docker.internal).
#
# Production config notes:
#   * --ctx-size 8192  — adopted from the ctx-8k experiment (2026-08-01):
#     -20% latency / +21% throughput vs 20480 by offloading 32/37 layers
#     instead of 25/37 (see benchmarks/llamacpp-comparison/results/matrix-summary.md)
#   * --metrics/--perf — kept so the server-side throughput cross-check and
#     monitoring keep working; remove if you want zero-overhead serving.
#   * The bge embedding server (:8081) is DEPRECATED and intentionally NOT
#     managed here — OpenJarvis does not use it.
#
# Config resolution: defaults → $OJ_LLAMA_* env vars → ~/.config/openjarvis/llamaserver.conf
# ───────────────────────────────────────────────────────────────────────────────
set -uo pipefail

VERSION="1.0.0"

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_BIN="$HOME/Git/llama.cpp/build/bin/llama-server"
DEFAULT_MODEL="$HOME/Git/llama.cpp/models/Qwen3-8B-Q3_K_M.gguf"
DEFAULT_PORT="8080"
DEFAULT_LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs"

# Only ever match the OpenJarvis production server (never other llama processes)
PATTERN="llama-server --model .*Qwen3-8B-Q3_K_M"

# Validated production flags (ctx-8k). Env var OJ_LLAMA_FLAGS can override.
DEFAULT_FLAGS=(
  --threads 12
  --threads-batch 8
  --ctx-size 8192
  --batch-size 512
  --ubatch-size 512
  --n-predict 8192
  --gpu-layers -1
  --kv-offload
  --flash-attn on
  --jinja
  --reasoning off
  --reasoning-budget 0
  --cache-type-k q8_0
  --cache-type-v q8_0
  --parallel 1
  --perf
  --metrics
)

# OpenJarvis container (backend API published on host port 9000). Auto-detected
# by image/name so it tracks compose recreates; override with OJ_LLAMA_CONTAINER.
CONTAINER_ID="${OJ_LLAMA_CONTAINER:-}"
if [[ -z "$CONTAINER_ID" ]]; then
  CONTAINER_ID="$(docker ps -q --filter ancestor=openjarvis:lean 2>/dev/null | head -1)"
fi
if [[ -z "$CONTAINER_ID" ]]; then
  CONTAINER_ID="$(docker ps -q --filter name=jarvis-1 2>/dev/null | head -1)"
fi
API_PORT="${OJ_LLAMA_API_PORT:-9000}"

# ── config resolution ─────────────────────────────────────────────────────────
CONF_FILE="${OJ_LLAMA_CONF:-$HOME/.config/openjarvis/llamaserver.conf}"
if [[ -f "$CONF_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONF_FILE"
fi

BIN="${OJ_LLAMA_BIN:-$DEFAULT_BIN}"
MODEL="${OJ_LLAMA_MODEL:-$DEFAULT_MODEL}"
PORT="${OJ_LLAMA_PORT:-$DEFAULT_PORT}"
LOG_DIR="${OJ_LLAMA_LOG_DIR:-$DEFAULT_LOG_DIR}"
LOG_FILE="$LOG_DIR/llamaserver.log"
PID_FILE="$LOG_DIR/llamaserver.pid"

if [[ -n "${OJ_LLAMA_FLAGS:-}" ]]; then
  read -r -a FLAGS <<< "$OJ_LLAMA_FLAGS"
else
  FLAGS=("${DEFAULT_FLAGS[@]}")
fi

# ── helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[llama]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[llama]\033[0m $*"; }
err()   { echo -e "\033[1;31m[llama]\033[0m ERROR: $*" >&2; }
die()   { err "$*"; exit 1; }

is_running() { pgrep -f "$PATTERN" >/dev/null 2>&1; }
server_pid() { pgrep -f "$PATTERN" | head -1; }

ctx_value() { # value following --ctx-size in FLAGS
  local i
  for ((i = 0; i < ${#FLAGS[@]}; i++)); do
    if [[ "${FLAGS[$i]}" == "--ctx-size" ]]; then
      echo "${FLAGS[$((i + 1))]:-?}"
      return 0
    fi
  done
  echo "?"
}

health_ok() {
  curl -sf -m 2 "http://localhost:${PORT}/health" >/dev/null 2>&1
}

wait_port_gone() { # give the port up to N seconds to release
  local n="${1:-30}" i
  for ((i = 0; i < n; i++)); do
    health_ok || return 0
    sleep 1
  done
  return 1
}

container_up() {
  docker inspect "$CONTAINER_ID" >/dev/null 2>&1
}

model_id() { basename "$MODEL"; }

# ── commands ──────────────────────────────────────────────────────────────────
cmd_start() {
  if is_running; then
    warn "llama-server already running (pid $(server_pid)) on :${PORT} — use 'restart' to cycle it."
    return 0
  fi
  [[ -x "$BIN" ]] || die "binary not found/executable: $BIN (set OJ_LLAMA_BIN)"
  [[ -f "$MODEL" ]] || die "model not found: $MODEL (set OJ_LLAMA_MODEL)"
  mkdir -p "$LOG_DIR"

  info "starting llama-server on :${PORT} (ctx $(ctx_value), log: $LOG_FILE)"
  info "  bin:   $BIN"
  info "  model: $MODEL"

  LD_LIBRARY_PATH="$(dirname "$BIN")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    nohup "$BIN" \
      --model "$MODEL" \
      --host 0.0.0.0 \
      --port "$PORT" \
      "${FLAGS[@]}" \
      >"$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"

  for i in $(seq 1 120); do
    if health_ok; then
      info "healthy after ${i}s (pid $(server_pid))"
      # Warmup: prime CUDA kernels once (not part of any measurement)
      curl -sf -m 120 -X POST "http://localhost:${PORT}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"'"$(model_id)"'","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
        >/dev/null 2>&1 || true
      curl -sf -m 2 "http://localhost:${PORT}/metrics" >/dev/null 2>&1 \
        && info "metrics endpoint enabled" \
        || warn "/metrics unavailable"
      container_up && info "OpenJarvis container present (${CONTAINER_ID})" \
        || warn "OpenJarvis container NOT running — server is up but nothing is consuming it"
      return 0
    fi
    if ! is_running; then
      err "server process died during startup; log tail:"
      tail -25 "$LOG_FILE" >&2
      return 1
    fi
    sleep 1
  done
  err "server failed to become healthy within 120s; log tail:"
  tail -25 "$LOG_FILE" >&2
  return 1
}

cmd_stop() {
  if ! is_running; then
    info "no llama-server running — nothing to stop"
    return 0
  fi
  local pid
  pid="$(server_pid)"
  info "stopping llama-server (pid ${pid})..."
  pkill -f "$PATTERN" 2>/dev/null || true
  if wait_port_gone 30; then
    info "stopped"
  else
    warn "still responsive after 30s — sending SIGKILL"
    pkill -9 -f "$PATTERN" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
  return 0
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

cmd_health() {
  if is_running && health_ok; then
    echo "ok"
    return 0
  else
    echo "down"
    return 1
  fi
}

cmd_status() {
  local pid=""
  if is_running; then pid="$(server_pid)"; fi

  echo "OpenJarvis llama-server ($(model_id))"
  echo "─────────────────────────────────────────────"
  if [[ -n "$pid" ]]; then
    echo "  state:   RUNNING (pid $pid, port $PORT)"
    health_ok && echo "  health:  OK" || echo "  health:  FAIL"
    # ctx + offload from the running log (last load_tensors block)
    if [[ -s "$LOG_FILE" ]]; then
      local ctx offload mapped
      ctx="$(grep -m1 -oE '\-\-ctx-size [0-9]+' "$LOG_FILE" | head -1)"
      offload="$(grep -oE 'offloaded [0-9]+/37 layers' "$LOG_FILE" | tail -1)"
      mapped="$(grep -oE 'CPU_Mapped model buffer size = *[0-9.]+ MiB' "$LOG_FILE" | tail -1)"
      [[ -n "$ctx" ]]     && echo "  flags:   ${ctx}"
      [[ -n "$offload" ]] && echo "  gpu:     ${offload}"
      [[ -n "$mapped" ]]  && echo "  cpu-map: ${mapped}"
    fi
    command -v nvidia-smi >/dev/null 2>&1 && \
      nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw --format=csv,noheader \
        | sed 's/^/  gpu-now: /'
  else
    echo "  state:   STOPPED"
    echo "  port:    $PORT (not listening)"
  fi

  echo "  bin:     $BIN"
  echo "  ctx:     $(ctx_value)"
  echo "  log:     $LOG_FILE"
  echo ""
  echo "OpenJarvis container"
  echo "─────────────────────"
  if container_up; then
    local api_status="(port ${API_PORT})"
    if curl -sf -m 2 "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
      api_status="OK on :${API_PORT}"
    else
      api_status="up but :${API_PORT}/health not responding"
    fi
    echo "  container: ${CONTAINER_ID} — $api_status"
  else
    echo "  container: NOT running (${CONTAINER_ID})"
  fi
  echo ""
  echo "  embedding :8081 is deprecated — not managed, not started"
}

cmd_logs() {
  local mode="tail"
  local n=50
  if [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]]; then mode="follow"; n="${2:-50}"; fi
  [[ -s "$LOG_FILE" ]] || { err "no log yet at $LOG_FILE"; return 1; }
  if [[ "$mode" == "follow" ]]; then
    tail -n "$n" -f "$LOG_FILE"
  else
    tail -n "${1:-$n}" "$LOG_FILE"
  fi
}

cmd_config() {
  echo "llamaserver.sh $VERSION — effective configuration"
  echo "  bin:       $BIN"
  echo "  model:     $MODEL"
  echo "  port:      $PORT"
  echo "  log dir:   $LOG_DIR"
  echo "  pid file:  $PID_FILE"
  echo "  config:    ${OJ_LLAMA_CONF:-$CONF_FILE}${CONF_FILE:+ ($([[ -f "$CONF_FILE" ]] && echo loaded || echo not found))}"
  echo "  flags:"
  printf '    %s\n' "${FLAGS[@]}"
}

cmd_help() {
  cat <<EOF
OpenJarvis llama-server management CLI (v$VERSION)

Usage: llamaserver.sh <command> [options]

Commands:
  start            Start the OpenJarvis llama-server (:${PORT}) with the
                   ctx-8192 production config; waits for health + warmup.
  stop             Stop the running llama-server (SIGTERM → SIGKILL fallback).
  restart          Stop, then start.
  status           Server state: pid, health, ctx, GPU offload, VRAM, and
                   OpenJarvis container connectivity.
  health           Print 'ok'/'down'; exit 0/1 (scriptable).
  logs [N]         Tail the last N log lines (default 50).
  logs -f [N]      Follow the log.
  config           Print the effective configuration and server flags.
  help             Show this help.

Config resolution (low → high): built-in defaults, \$OJ_LLAMA_* env vars,
~/.config/openjarvis/llamaserver.conf (sourced if present).

Env vars:
  OJ_LLAMA_BIN      llama-server binary      (default $DEFAULT_BIN)
  OJ_LLAMA_MODEL    model path               (default $DEFAULT_MODEL)
  OJ_LLAMA_PORT     listen port              (default $DEFAULT_PORT)
  OJ_LLAMA_LOG_DIR  logs + pidfile location  (default $DEFAULT_LOG_DIR)
  OJ_LLAMA_FLAGS    quoted server flags string (overrides DEFAULT_FLAGS)
  OJ_LLAMA_CONTAINER OpenJarvis container id
  OJ_LLAMA_API_PORT OpenJarvis backend port (default 9000)

Note: the deprecated bge embedding server (:8081) is intentionally NOT
managed here.
EOF
}

# ── dispatch ──────────────────────────────────────────────────────────────────
CMD="${1:-help}"
shift 1 2>/dev/null || true
case "$CMD" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  health)  cmd_health ;;
  logs)    cmd_logs "$@" ;;
  config)  cmd_config ;;
  help|-h|--help|"") cmd_help ;;
  *) err "unknown command: $CMD"; cmd_help; exit 2 ;;
esac
