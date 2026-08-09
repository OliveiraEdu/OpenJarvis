#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenJarvis — Trend Seeker daily digest launcher
#
# Turns a calendar day's deep-dive runs into ready-to-publish outputs
# (social.md ≤500 chars + newsletter.md + digest-state.json) under
# $OJ_WORKSPACE_HOST/digests/<date>/. Deterministic orchestration and the
# per-run engine calls live in scripts/digest.py (stdlib-only); this launcher
# only injects context (state dir, workspace, agent) and holds the run-lock,
# so two digest runs never contend for the single engine, and discovery /
# the deep-dive pipeline are never blocked by a digest.
#
# Usage:
#   ./scripts/digest.sh                       # digest of yesterday (local)
#   ./scripts/digest.sh --date 2026-08-08     # digest of a specific day
#   ./scripts/digest.sh --date yesterday      # explicit, same as default
#   ./scripts/digest.sh --force               # re-ask the engine for every run
#
# Env (same names as research.sh / discovery.sh):
#   OJ_STATE_DIR          signals.db + lock dir (default ~/.openjarvis)
#   OJ_WORKSPACE_HOST     report workspace (default ~/Git/openjarvis-workspace)
#   OJ_AGENT_NAME         agent used for the per-run digest asks
#                         (default it-market-analyst — the same agent that
#                         wrote the reports; the digest prompt fully constrains
#                         the task, so no dedicated digest agent is needed)
#   OJ_SKIP_SANITY=1      skip the make jarvis-health check (offline tests)
#
# Exit codes: 0 done or deferred (lock held); 1 stack unreachable; 2 CLI/usage
# error from digest.py.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${OJ_STATE_DIR:-$HOME/.openjarvis}"
mkdir -p "$STATE_DIR"

# ── Run-lock: one digest at a time (mkdir is atomic; a concurrent run defers
# instead of double-running — D6: honest, explicit).
LOCK_DIR="${STATE_DIR}/digest.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[digest] another digest run holds the run-lock ($LOCK_DIR); deferring."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# ── Sanity: stack reachable (skippable for offline tests). The digest needs
# the engine for its per-run asks, so a down stack aborts like research.sh.
if [ "${OJ_SKIP_SANITY:-0}" != "1" ] && ! make -C "$ROOT" jarvis-health >/dev/null 2>&1; then
  echo "[digest] ERROR: Jarvis API not reachable on :9000. Start the stack (make boot)." >&2
  exit 1
fi

# ── Delegate. Without exec the EXIT trap fires after python3 returns, which
# releases the run-lock; set -e propagates a nonzero exit code unchanged.
OJ_STATE_DIR="$STATE_DIR" \
OJ_WORKSPACE_HOST="${OJ_WORKSPACE_HOST:-$HOME/Git/openjarvis-workspace}" \
OJ_AGENT_NAME="${OJ_AGENT_NAME:-it-market-analyst}" \
  python3 "$ROOT/scripts/digest.py" "$@"
