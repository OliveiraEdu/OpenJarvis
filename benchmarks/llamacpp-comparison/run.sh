#!/usr/bin/env bash
# ── OpenJarvis × llama.cpp binary comparison — orchestrator ────────────────
#
# Phases:
#   0. Environment fingerprint (GPU, model hash, binary hashes/versions)
#   1. Controlled engine microbenchmark via `jarvis bench run`
#      Order A/B/B/A (stock, custom, custom, stock) to cancel thermal drift.
#      Host-side nvidia-smi power sampling + llama.cpp /metrics snapshots.
#   2. Application-level workload through the real API (:9000) + telemetry
#      (`jarvis telemetry clear` → fixed workload → `telemetry stats/export`).
#   3. compare.py → comparison_report.md + comparison.json
#
# Usage:
#   ./run.sh [tag]     # tag defaults to a timestamp; results land in results/<tag>
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$SCRIPT_DIR/config.env"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"
# Which phases to run: all | phase1 | phase2 (Phase 0 + 3 always run)
PHASES="${PHASES:-all}"
RUN_DIR="$RESULTS_DIR/$TAG"
mkdir -p "$RUN_DIR"/{env,phase1,phase2}

log() { echo -e "\033[1;34m[run]\033[0m $*"; }
die() { echo -e "\033[1;31m[run]\033[0m ERROR: $*" >&2; exit 1; }

# ── prerequisites ─────────────────────────────────────────────────────────
command -v curl >/dev/null || die "curl required"
command -v nvidia-smi >/dev/null || die "nvidia-smi required"
command -v docker >/dev/null || die "docker required"
python3 -c "import sys; assert sys.version_info >= (3, 9)" 2>/dev/null \
  || die "python3 >= 3.9 required"
docker inspect "$CONTAINER_ID" >/dev/null 2>&1 \
  || die "container $CONTAINER_ID not running"
[[ -f "$MODEL" ]] || die "model not found: $MODEL"
[[ -x "$STOCK_DIR/build/bin/llama-server" ]] || die "stock binary missing: $STOCK_DIR/build/bin/llama-server"
[[ -x "$CUSTOM_DIR/bin/llama-server" ]] || die "custom binary missing: $CUSTOM_DIR/bin/llama-server"

log "tag: $TAG"
log "phases: $PHASES"
log "run dir: $RUN_DIR"

# ── Phase 0 — environment fingerprint ────────────────────────────────────
log "Phase 0: environment fingerprint"
python3 "$SCRIPT_DIR/fingerprint.py" "$RUN_DIR/env" \
  --config "$SCRIPT_DIR/config.env" --container "$CONTAINER_ID" || die "fingerprint failed"

# ── Phase 1 — controlled engine microbenchmark (A/B/B/A) ─────────────────
if [[ "$PHASES" == "all" || "$PHASES" == "phase1" ]]; then
log "Phase 1: engine microbenchmark (A/B/B/A)"
declare -a ORDER=(stock custom custom stock)
run_idx=0
for variant in "${ORDER[@]}"; do
  run_idx=$((run_idx + 1))
  tag="${variant}_${run_idx}"
  log "── $tag — starting ${variant} llama-server ──"
  "$SCRIPT_DIR/server_ctl.sh" stop || true
  sleep 2
  "$SCRIPT_DIR/server_ctl.sh" start "$variant" || die "failed to start ${variant} server"

  # 30s idle baseline (power at rest with model loaded)
  local_start="$(date +%s)"
  python3 "$SCRIPT_DIR/power_sample.py" "$RUN_DIR/phase1/${tag}_idle.csv" --seconds 30
  local_end="$(date +%s)"
  echo -e "${tag}\tidle\t${local_start}\t${local_end}" >> "$RUN_DIR/phase1/windows.tsv"

  for bench in latency throughput energy; do
    log "  bench: ${bench}"
    mkdir -p "$RUN_DIR/phase1/${tag}"
    start_epoch="$(date +%s)"
    python3 "$SCRIPT_DIR/power_sample.py" "$RUN_DIR/phase1/${tag}_${bench}_power.csv" --seconds 1800 &
    power_pid=$!

    curl -sf -m 5 "http://localhost:${PORT}/metrics" \
      > "$RUN_DIR/phase1/${tag}_${bench}_metrics_start.txt" 2>/dev/null || true

    docker exec "$CONTAINER_ID" bash -c "
      mkdir -p /tmp/reports/bench/${tag} &&
      jarvis bench run \
        -e llamacpp -m '${MODEL_ID}' \
        -b '${bench}' \
        -o /tmp/reports/bench/${tag}/${bench}.jsonl \
        --json" \
      > "$RUN_DIR/phase1/${tag}_${bench}_summary.json" \
      2> "$RUN_DIR/phase1/${tag}_${bench}.stderr.log" || true

    end_epoch="$(date +%s)"
    curl -sf -m 5 "http://localhost:${PORT}/metrics" \
      > "$RUN_DIR/phase1/${tag}_${bench}_metrics_end.txt" 2>/dev/null || true

    kill "$power_pid" 2>/dev/null || true
    wait "$power_pid" 2>/dev/null || true
    echo -e "${tag}\t${bench}\t${start_epoch}\t${end_epoch}" >> "$RUN_DIR/phase1/windows.tsv"
    log "    done (${bench}) in $((end_epoch - start_epoch))s"
  done
done

# Pull bench JSONL out of the container
docker cp "$CONTAINER_ID:/tmp/reports/bench" "$RUN_DIR/phase1/container_bench" 2>/dev/null || true
fi

# ── Phase 2 — application-level workload + telemetry ─────────────────────
if [[ "$PHASES" == "all" || "$PHASES" == "phase2" ]]; then
log "Phase 2: API workload + telemetry (per binary)"
for variant in stock custom; do
  log "── $variant — starting server, clearing telemetry, running workload ──"
  "$SCRIPT_DIR/server_ctl.sh" stop || true
  sleep 2
  "$SCRIPT_DIR/server_ctl.sh" start "$variant" || die "failed to start ${variant} server"

  log "  clearing container telemetry"
  docker exec "$CONTAINER_ID" bash -c "jarvis telemetry clear -y" \
    > "$RUN_DIR/phase2/${variant}_clear.log" 2>&1 || true

  start_epoch="$(date +%s)"
  python3 "$SCRIPT_DIR/power_sample.py" "$RUN_DIR/phase2/${variant}_power.csv" --seconds 1800 &
  power_pid=$!

  python3 "$SCRIPT_DIR/api_workload.py" \
    --base-url "http://localhost:${API_PORT}" \
    --api-key "$API_KEY" \
    --model "$MODEL_ID" \
    --output "$RUN_DIR/phase2/${variant}_requests.csv" \
    || true

  end_epoch="$(date +%s)"
  kill "$power_pid" 2>/dev/null || true
  wait "$power_pid" 2>/dev/null || true
  echo -e "${variant}\tapi\t${start_epoch}\t${end_epoch}" >> "$RUN_DIR/phase2/windows.tsv"

  log "  exporting telemetry"
  docker exec "$CONTAINER_ID" bash -c "jarvis telemetry stats" \
    > "$RUN_DIR/phase2/${variant}_stats.txt" 2>&1 || true
  docker exec "$CONTAINER_ID" bash -c "mkdir -p /tmp/reports && jarvis telemetry export -f json -o /tmp/reports/telemetry_${variant}.json" \
    || true
  docker cp "$CONTAINER_ID:/tmp/reports/telemetry_${variant}.json" \
    "$RUN_DIR/phase2/${variant}_telemetry.json" 2>/dev/null || true
done
fi

# ── Phase 3 — comparison report ──────────────────────────────────────────
log "Phase 3: comparison report"
python3 "$SCRIPT_DIR/compare.py" "$RUN_DIR" --tag "$TAG" || die "compare failed"

log ""
log "Done. Results in $RUN_DIR"
log "Report: $RUN_DIR/comparison_report.md"
log ""
log "NOTE: the llama-server that is currently up is the LAST one started ($variant)."
