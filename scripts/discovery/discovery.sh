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
#
# Env (same names as research.sh):
#   OJ_STATE_DIR          signals.db + lock dir (default ~/.openjarvis)
#   OJ_WORKSPACE_HOST     triggered deep-dive report workspace
#                         (default ~/Git/openjarvis-workspace)
#   OJ_SKIP_SANITY=1      skip the make jarvis-health check (offline tests)
#
# Exit codes: 0 done or deferred (lock held); 1 stack unreachable or phase
# failure; 2 CLI/usage error from discovery.py.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${OJ_STATE_DIR:-$HOME/.openjarvis}"
mkdir -p "$STATE_DIR"

# ── Run-lock: one cycle at a time. mkdir is atomic (no GNU-only flock), so a
# concurrent cycle defers instead of double-running (D6: honest, explicit).
LOCK_DIR="${STATE_DIR}/discovery.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[discovery] another cycle holds the run-lock ($LOCK_DIR); deferring."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# ── Sanity: stack reachable (skippable for offline tests; the deep-dive and
# triage need the engine, a bare collection pass does not).
if [ "${OJ_SKIP_SANITY:-0}" != "1" ] && ! make -C "$ROOT" jarvis-health >/dev/null 2>&1; then
  echo "[discovery] ERROR: Jarvis API not reachable on :9000. Start the stack (make boot)." >&2
  exit 1
fi

# ── Delegate. Without exec the EXIT trap fires after python3 returns, which
# releases the run-lock; set -e propagates a nonzero exit code unchanged.
OJ_STATE_DIR="$STATE_DIR" \
OJ_WORKSPACE_HOST="${OJ_WORKSPACE_HOST:-$HOME/Git/openjarvis-workspace}" \
  python3 "$ROOT/scripts/discovery/discovery.py" "$@"
