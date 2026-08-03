#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenJarvis — On-demand IT-Market Research Analyst launcher
#
# Runs a sourced, math-consistent IT-market research report on demand. Because
# the deployment model (Qwen3-8B-Q3, the max that fits the 6GB GPU at ctx-8192)
# tends to shortcut big open-ended tasks, research is split into two small,
# verifiable phases:
#
#   Phase 1 — GATHER:  search the live web + fetch pages, save raw findings
#                      (facts with sources, URLs, dates) to findings.md
#   Phase 2 — VERIFY:  run every CAGR/projection/share through the calculator
#                      tool, persist verified figures to numbers.md
#   Phase 3 — REPORT:  read findings + numbers, write the full structured
#                      report to report.md in sequential file_write chunks
#                      (a single large write breaks the tool-call JSON grammar)
#   Phase 4 — FEEDBACK: after each successful phase a deterministic quality
#                      score (retries needed, artifact size) is written as
#                      feedback on that phase's trace, feeding the
#                      Trace-Driven Learning loop (Phase B of the TDL work).
#
# Each phase is checked for its artifact file (up to two retries, three
# attempts per phase — MAX_ATTEMPTS in research_phases.py), so a
# shortcutting model cannot silently return without the deliverable. Phase 2
# (VERIFY) and phase 3b (REPORT part 2) degrade instead of aborting: a weak
# VERIFY marks numbers.md UNVERIFIED and the run continues (a weak VERIFY
# must not throw away the GATHER work); a weak 3b accepts the best-effort
# report with an explicit caveat and still runs the provenance check (a weak
# 3b must not throw away the part-1 work).
#
# Phase B feedback is written for every phase outcome — high on success
# (retries, artifact size), low on gate failure — feeding the TDL loop.
#
# Reports land in the container workspace /workspace/<slug>/ which is
# bind-mounted to the host workspace (default ~/Git/openjarvis-workspace).
#
# Usage:
#   ./scripts/research.sh "Subject: AI infrastructure | Scope: global, 2025-2030"
#   ./scripts/research.sh "Subject: RISC-V CPUs | Scope: Europe, 2024-2028"
#
# Requirements:
#   - Stack up: `make boot` (or `make jarvis-up` + llama-server up)
#   - Template: deploy/templates/it_market_analyst.toml (auto-synced when changed)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TOPIC="${1:?Usage: $0 \"<subject + scope + constraints>\"}"
AGENT_NAME="${OJ_AGENT_NAME:-it-market-analyst}"
TEMPLATE_ID="it_market_analyst"
TEMPLATE_FILE="deploy/templates/${TEMPLATE_ID}.toml"
STATE_DIR="${OJ_STATE_DIR:-$HOME/.openjarvis}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Deterministic leaf functions (validators, normalize, provenance, gates,
# banners, feedback scoring) live in research_lib.sh — the SAME code the
# offline harness in tests/pipeline/ exercises via `bash -c`.
# shellcheck source=research_lib.sh
source "${ROOT}/scripts/research_lib.sh"

# Host workspace (same default as deploy/docker/.env.example)
WORKSPACE_HOST="${OPENJARVIS_WORKSPACE_HOST:-$HOME/Git/openjarvis-workspace}"

slug="$(printf '%s' "$TOPIC" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
  | sed 's/^-//;s/-$//' | cut -c1-40)"
[ -n "$slug" ] || slug="research"

echo "[research] root:     $ROOT"
echo "[research] agent:    $AGENT_NAME (template: $TEMPLATE_ID)"
echo "[research] topic:    $TOPIC"
echo "[research] slug:     $slug"

# ── 0. Clean the target slug dir so artifact checks see only fresh files
rm -rf "${WORKSPACE_HOST}/${slug}"
mkdir -p "${WORKSPACE_HOST}/${slug}"

# ── 1. Sync template into the persistent state dir (bind-mounted into container).
# Re-sync whenever the repo copy changes, so template fixes take effect on the
# next run (system_prompt propagation below then updates the agent too).
mkdir -p "${STATE_DIR}/templates"
if [ -f "${STATE_DIR}/templates/${TEMPLATE_ID}.toml" ] \
   && cmp -s "${ROOT}/${TEMPLATE_FILE}" "${STATE_DIR}/templates/${TEMPLATE_ID}.toml"; then
  echo "[research] template already synced (${STATE_DIR}/templates/${TEMPLATE_ID}.toml)"
else
  cp "${ROOT}/${TEMPLATE_FILE}" "${STATE_DIR}/templates/${TEMPLATE_ID}.toml"
  echo "[research] synced template -> ${STATE_DIR}/templates/${TEMPLATE_ID}.toml"
fi

# ── 2. Sanity: stack reachable
if ! make -C "$ROOT" jarvis-health >/dev/null 2>&1; then
  echo "[research] ERROR: Jarvis API not reachable on :9000. Start the stack (make boot)." >&2
  exit 1
fi

# ── 3. Create the managed agent once (idempotent).
# Host-side check on the bind-mounted agents.db (deterministic; the CLI table
# output is not reliable to grep from a background process).
agent_count="$(
  python3 -c "import sqlite3; print(sqlite3.connect('$STATE_DIR/agents.db').execute('select count(*) from managed_agents where name=?', ('$AGENT_NAME',)).fetchone()[0])" 2>/dev/null || echo 0
)"
if [ "$agent_count" -gt 0 ]; then
  echo "[research] agent already exists, reusing it"
else
  echo "[research] creating agent from template..."
  make -C "$ROOT" jarvis-exec CMD="jarvis agents create -n $AGENT_NAME -t $TEMPLATE_ID"
fi

# The agent's system_prompt is baked into agents.db at creation; the executor
# reads config_json.system_prompt every tick (the template file is not
# re-rendered). Propagate the CURRENT template's system_prompt so prompt fixes
# take effect on the next run without recreating the agent.
python3 - <<EOF
import sqlite3, json, tomllib
tpl = tomllib.load(open("${STATE_DIR}/templates/${TEMPLATE_ID}.toml", "rb"))
new_sp = tpl["template"]["system_prompt_template"]
db = sqlite3.connect("${STATE_DIR}/agents.db", timeout=15)
for (cid,) in db.execute(
    "select id from managed_agents where name=? and status != 'archived'",
    ("$AGENT_NAME",),
):
    cfg = json.loads(db.execute(
        "select config_json from managed_agents where id=?", (cid,)
    ).fetchone()[0])
    if cfg.get("system_prompt") != new_sp:
        cfg["system_prompt"] = new_sp
        db.execute(
            "update managed_agents set config_json=? where id=?",
            (json.dumps(cfg), cid),
        )
        print(f"[research] pushed template system_prompt ({len(new_sp)} bytes) to agent {cid}")
db.commit()
EOF

# ── 4. Phase orchestration (C1): the retry/gate/feedback loop and the typed
# phase specs (label, prompt template, artifact, validator, tool gate,
# snapshot, feedback keyword, normalize hook) live in typed Python —
# scripts/research_phases.py. This launcher only injects the dynamic context
# (topic, slug, workspace, state dir, agent) and delegates; the validators,
# normalize hooks, gate counters, and scoring still run as the SAME bash
# functions in research_lib.sh that the offline harness in tests/pipeline/
# exercises via `bash -c`.
# Usage: run_phase <gather|verify|part1|part2>
run_phase() {
  local phase="$1"
  OJ_TOPIC="$TOPIC" OJ_SLUG="$slug" OJ_WORKSPACE_HOST="$WORKSPACE_HOST" \
  OJ_STATE_DIR="$STATE_DIR" OJ_AGENT_NAME="$AGENT_NAME" \
  OJ_MIN_ARTIFACT_SIZE="${MIN_ARTIFACT_SIZE:-200}" \
    python3 "$ROOT/scripts/research_phases.py" run --phase "$phase"
}

# ── 5. Phase 1 — GATHER (scaffold findings early, append as you go)
run_phase gather || exit 1

# ── 6. Phase 2 — VERIFY (math consistency, artifact-enforced). Degrades, it
# does not abort: a weak VERIFY must not throw away the GATHER work.
# Prepend an explicit UNVERIFIED banner to numbers.md so the report phases
# (and the reader) can see the figures were never calculator-checked.
if ! run_phase verify; then
  echo "[research] WARNING: phase 2 (verify) failed after retries — continuing with figures marked UNVERIFIED."
  mark_numbers_unverified "${WORKSPACE_HOST}/${slug}/numbers.md"
  echo "[research] ${WORKSPACE_HOST}/${slug}/numbers.md marked as UNVERIFIED."
fi

# ── 7. Phase 3a — REPORT part 1 (Title..Detailed Analysis, chunked writes)
run_phase part1 || exit 1

# Snapshot part 1 so each part-2 attempt starts from a clean state (a failed
# attempt's appends must not pollute the next attempt).
cp -f "${WORKSPACE_HOST}/${slug}/report.md" "${WORKSPACE_HOST}/${slug}/report.part1"
echo "[research] part 1 snapshot saved (report.part1)"

# ── 8. Phase 3b — REPORT part 2 (append Conclusions..Confidence, chunked).
# Degrades, it does not abort: a weak 3b must not throw away the part-1 work.
# Accept the best report we have (part 1 plus whatever the last attempt
# appended), repair glued headings, and make the status explicit to the
# reader. The provenance check below still runs.
if ! run_phase part2; then
  echo "[research] WARNING: phase 3b (report part 2) failed after retries — accepting best-effort report."
  fix_glued_headings "${WORKSPACE_HOST}/${slug}/report.md"
  if check_report_sections "${WORKSPACE_HOST}/${slug}/report.md"; then
    # All sections are present once headings were repaired — the gate failed
    # on formatting only (glued headings), not missing content.
    printf '\n> **NOTE** — phase 3b initially failed validation; after heading repair all required sections are present.\n' >> "${WORKSPACE_HOST}/${slug}/report.md"
    echo "[research] report complete after heading repair (all sections present)."
  else
    printf '\n> **PARTIAL REPORT** — phase 3b could not complete within retries; the report may be missing sections or contain unverified content. Review before relying on it.\n' >> "${WORKSPACE_HOST}/${slug}/report.md"
    echo "[research] accepting PARTIAL report (some sections still missing)."
  fi
fi

# ── 9. UNVERIFIED banner (deterministic, script-side): the model has twice
# ignored the prompt instruction to carry the phase-2 UNVERIFIED banner into
# the report. If numbers.md was degraded, prepend the banner to report.md
# regardless of model compliance so the reader can see the figures are not
# machine-verified.
apply_unverified_banner "${WORKSPACE_HOST}/${slug}/report.md" "${WORKSPACE_HOST}/${slug}/numbers.md"

# ── 10. Provenance note (soft): flag fabricated-looking source URLs.
check_sources_provenance "${WORKSPACE_HOST}/${slug}/report.md" "${WORKSPACE_HOST}/${slug}/findings.md" || true

rm -f "${WORKSPACE_HOST}/${slug}/report.part1"

echo ""
echo "[research] done."
echo "  container:  /workspace/${slug}/report.md"
echo "  host:       ${WORKSPACE_HOST}/${slug}/report.md"
