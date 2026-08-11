#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenJarvis — Trend Seeker discovery engine launcher
#
# Runs one market-signal discovery cycle. Deterministic orchestration lives in
# scripts/discovery/*.py (stdlib-only); this launcher only injects context
# (state dir, workspace, repo root) and holds the run-lock (design §4.7), so
# discovery and the deep-dive pipeline never contend for the single engine.
#
# Usage:
#   ./scripts/discovery/discovery.sh run --cycle
#   ./scripts/discovery/discovery.sh run --once --source hn
#   ./scripts/discovery/discovery.sh stats
#   ./scripts/discovery/discovery.sh calibrate
#   ./scripts/discovery/discovery.sh hf --top 10
#   ./scripts/discovery/discovery.sh signals --source github --top 20
#   ./scripts/discovery/discovery.sh timeline
#   # Every `run` appends one line to the pipeline-owned scheduler cycle
#   # ledger (design §4.8); `timeline` renders it. OJ_SCHEDULER_RUNS
#   # overrides the default runs dir.
#
# Env (same names as research.sh):
#   OJ_STATE_DIR          signals.db + lock dir (default ~/.openjarvis)
#   OJ_WORKSPACE_HOST     triggered deep-dive report workspace
#                         (default ~/Git/openjarvis-workspace)
#   OJ_SCHEDULER_RUNS     scheduler runs dir for the timeline cycle ledger
#                         (default: $STATE_DIR/scheduler-runs, written by this
#                         launcher itself; C7 — no hardcoded path, env
#                         override for other layouts)
#   OJ_SKIP_SANITY=1      skip the make jarvis-health check (offline tests)
#
# Exit codes: 0 done or deferred (lock held); 1 stack unreachable or phase
# failure; 2 CLI/usage error from discovery.py.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${OJ_STATE_DIR:-$HOME/.openjarvis}"
mkdir -p "$STATE_DIR"

# ── Timeline cycle ledger: the pipeline-owned runs dir under the state dir.
# Every `run` below appends one JSONL line there, so the cycle history follows
# the pipeline across schedulers (design §4.8 — no opencode dependency). An
# explicit OJ_SCHEDULER_RUNS always wins; the dir may simply not exist yet,
# and the timeline says so honestly instead of erroring.
if [ -z "${OJ_SCHEDULER_RUNS:-}" ]; then
  OJ_SCHEDULER_RUNS="$STATE_DIR/scheduler-runs"
fi
export OJ_SCHEDULER_RUNS

# ── Run-lock: one cycle at a time. mkdir is atomic (no GNU-only flock), so a
# concurrent cycle defers instead of double-running (D6: honest, explicit).
LOCK_DIR="${STATE_DIR}/discovery.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[discovery] another cycle holds the run-lock ($LOCK_DIR); deferring."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# ── Sanity: stack reachable (skippable for offline tests; the deep-dive and
# triage need the engine, a bare collection pass does not). The started
# timestamp is captured before the run so the ledger reflects the fire time,
# not the (possibly much later) completion time.
_cycle_started="$(python3 -c 'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))')"
set +e
if [ "${OJ_SKIP_SANITY:-0}" != "1" ] && ! make -C "$ROOT" jarvis-health >/dev/null 2>&1; then
  echo "[discovery] ERROR: Jarvis API not reachable on :9000. Start the stack (make boot)." >&2
  _cycle_rc=1
else
  # ── Delegate. Without exec the EXIT trap fires after python3 returns, which
  # releases the run-lock; _cycle_rc captures the real exit code for the
  # ledger below.
  OJ_STATE_DIR="$STATE_DIR" \
  OJ_WORKSPACE_HOST="${OJ_WORKSPACE_HOST:-$HOME/Git/openjarvis-workspace}" \
    python3 "$ROOT/scripts/discovery/discovery.py" "$@"
  _cycle_rc=$?
fi
set -e

# ── Cycle ledger: one JSONL line per discovery run, in the exact shape the
# timeline reader expects (startedAt, exitCode — design §4.8). A usage error
# (rc=2) means no cycle was attempted, so it is not recorded; a held run-lock
# exits before this point, so a deferred fire is never double-counted;
# read-only subcommands (stats/timeline/...) never append.
if [ "${1:-}" = "run" ] && [ "$_cycle_rc" -ne 2 ]; then
  mkdir -p "$OJ_SCHEDULER_RUNS"
  printf '{"startedAt": "%s", "exitCode": %d}\n' "$_cycle_started" "$_cycle_rc" \
    >> "$OJ_SCHEDULER_RUNS/trend-seeker-discovery.jsonl"
fi

exit "$_cycle_rc"
