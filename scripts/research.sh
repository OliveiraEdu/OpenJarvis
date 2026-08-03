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
# Each phase is checked for its artifact file (one retry per phase), so a
# shortcutting model cannot silently return without the deliverable. Phase 2
# (VERIFY) degrades instead of aborting: if the calculator tool gate cannot
# be met after retries, numbers.md is marked UNVERIFIED and the run
# continues, so a weak VERIFY phase cannot throw away the GATHER work.
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
# Host workspace (same default as deploy/docker/.env.example)
WORKSPACE_HOST="${OPENJARVIS_WORKSPACE_HOST:-$HOME/Git/openjarvis-workspace}"

slug="$(printf '%s' "$TOPIC" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
  | sed 's/^-//;s/-$//' | cut -c1-40)"
[ -n "$slug" ] || slug="research"
SLUG_DIR="/workspace/${slug}"
FINDINGS="${SLUG_DIR}/findings.md"
NUMBERS="${SLUG_DIR}/numbers.md"
REPORT="${SLUG_DIR}/report.md"

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

# ── 4. Helper: run one phase, verify its artifact exists on the host
# (bind mounts are synchronous, so the host sees the file immediately; this
# avoids docker/make/quoting entirely). Two retries per phase.
# Optional 4th arg: validator function (default: true) that receives the host
# artifact path and must return 0 for the phase to pass (structural checks).
# Optional 5th arg: tool-usage gate "tool:mincount" — the captured ask trace
# must contain at least mincount calls of `↳ <tool>`, else the phase is
# treated as a failure and retried (enforces the workflow mechanically).
# Optional 6th arg: snapshot path — restored (cp) over the artifact before
# every attempt. Used for append-phases so a failed attempt's appends do not
# pollute the artifact for the next attempt.
# Optional 7th arg: feedback keyword — when set, on success a deterministic
# TDL score is written to the trace that produced the artifact (Phase B).
run_phase() {
  local label="$1" prompt="$2" host_artifact="$3" validator="${4:-true}" tool_req="${5:-}" snapshot="${6:-}" fb_keyword="${7:-}" attempt=1
  while :; do
    echo ""
    echo "[research] ${label} — attempt ${attempt}..."
    if [ -n "$snapshot" ] && [ -f "$snapshot" ]; then
      cp -f "$snapshot" "$host_artifact"
      echo "[research] ${label}: restored artifact from snapshot"
    fi
    # Reset summary_memory so the executor's input stays clean (a stale tick
    # note makes the model answer with a status report instead of the task).
    python3 -c "import sqlite3; c=sqlite3.connect('$STATE_DIR/agents.db', timeout=15); c.execute('update managed_agents set summary_memory=? where name=?', ('', '$AGENT_NAME')); c.commit()" 2>/dev/null || true
    local asklog
    asklog="$(mktemp)"
    make -C "$ROOT" jarvis-exec CMD="jarvis agents ask $AGENT_NAME \"$prompt\"" 2>&1 | tee "$asklog" || true
    local ok=0
    if ! { [ -f "$host_artifact" ] \
           && [ "$(stat -c%s "$host_artifact" 2>/dev/null || echo 0)" -ge "${MIN_ARTIFACT_SIZE:-200}" ] \
           && $validator "$host_artifact"; }; then
      ok=1
    fi
    if [ -n "$tool_req" ]; then
      local req_tool="${tool_req%%:*}" req_min="${tool_req##*:}"
      local n
      n="$(grep -c "↳ ${req_tool}" "$asklog" 2>/dev/null || true)"
      if [ "${n:-0}" -lt "$req_min" ]; then
        ok=1
        echo "[research] ${label}: tool-usage gate failed (${n:-0} ${req_tool} call(s) < ${req_min})"
      fi
    fi
    rm -f "$asklog"
    if [ "$ok" -eq 0 ]; then
      echo "[research] ${label} OK -> $host_artifact"
      if [ -n "$fb_keyword" ]; then
        record_feedback "$label" "$fb_keyword" "$attempt" "$host_artifact"
      fi
      return 0
    fi
    if [ "$attempt" -ge 3 ]; then
      echo "[research] ERROR: ${label} did not produce a valid $host_artifact after ${attempt} attempts." >&2
      # Record a low feedback score on the failing trace so the TDL loop
      # sees the failure (score floor well below any passing phase).
      if [ -n "$fb_keyword" ]; then
        record_feedback "$label" "$fb_keyword" "$attempt" "$host_artifact" no
      fi
      return 1
    fi
    echo "[research] artifact missing, too small, failing validation, or tool gate unmet ($host_artifact); retrying..."
    attempt=$((attempt + 1))
  done
}

# ── Phase B — deterministic per-phase feedback (Trace-Driven Learning).
# After a phase succeeds, derive a quality score [0,1] from signals we
# already collected (retry-free run means the model did not shortcut;
# artifact size means substance) and write it as feedback on the trace that
# produced the artifact. Phase A made this possible: the trace's query now
# carries the phase prompt, so the right trace is located by keyword.
record_feedback() {
  local label="$1" keyword="$2" attempts="$3" artifact="$4" passed="${5:-yes}"
  local size attempts_bonus size_bonus score agent_uuid
  size="$(stat -c%s "$artifact" 2>/dev/null || echo 0)"
  case "$attempts" in
    1) attempts_bonus=0.2 ;;
    2) attempts_bonus=0.1 ;;
    *) attempts_bonus=0.0 ;;
  esac
  if [ "$size" -ge 4000 ]; then
    size_bonus=0.2
  elif [ "$size" -ge 1500 ]; then
    size_bonus=0.1
  else
    size_bonus=0.0
  fi
  if [ "$passed" = "no" ]; then
    # Failed phase: workflow contract violated (e.g. tool gate unmet).
    # Keep the score low and well below any passing phase, regardless of
    # artifact size — size alone must never mask a broken workflow.
    score="$(python3 -c "print(f'{min(0.3, 0.2 + $size_bonus):.3f}')")"
  else
    # base 0.6 = passed every gate (artifact present + validator + tool gate)
    score="$(python3 -c "print(f'{min(1.0, 0.6 + $attempts_bonus + $size_bonus):.3f}')")"
  fi
  agent_uuid="$(
    python3 -c "import sqlite3; print(sqlite3.connect('$STATE_DIR/agents.db').execute(\"select id from managed_agents where name=? and status != 'archived' order by last_run_at desc limit 1\", ('$AGENT_NAME',)).fetchone()[0])" 2>/dev/null || true
  )"
  if [ -z "$agent_uuid" ]; then
    echo "[research] ${label}: agent uuid not found, feedback skipped"
    return 0
  fi
  python3 - "$label" "$keyword" "$score" "$agent_uuid" "$STATE_DIR" <<'PYEOF'
import sqlite3
import sys

label, keyword, score, agent_uuid, state_dir = sys.argv[1:]
db = sqlite3.connect(f"{state_dir}/traces.db", timeout=15)
row = db.execute(
    "select trace_id from traces where agent = ? and query like ?"
    " order by started_at desc limit 1",
    (agent_uuid, f"%{keyword}%"),
).fetchone()
if row:
    db.execute(
        "update traces set feedback = ? where trace_id = ?",
        (float(score), row[0]),
    )
    db.commit()
    print(f"[research] {label}: feedback {score} -> trace {row[0]}")
else:
    print(
        f"[research] {label}: no trace matched keyword '{keyword}', feedback skipped"
    )
db.close()
PYEOF
}

# Validator: the numbers table must have at least 3 data rows and show at
# least one parenthesized formula (evidence the calculator was really used).
check_numbers_table() {
  local f="$1"
  local rows
  rows="$(grep -cE '^\|' "$f" 2>/dev/null || true)"
  [ "${rows:-0}" -ge 3 ] || return 1
  grep -qE '\([^)]*[0-9][^)]*\)' "$f" || return 1
  return 0
}

# Validator: part 1 must contain the first three required sections.
check_report_part1() {
  local f="$1"
  grep -qiE '^#{1,3} *[Ii]ntroduction' "$f" || return 1
  grep -qiE '^#{1,3} *[Ee]xecutive [Ss]ummary' "$f" || return 1
  grep -qiE '^#{1,3} *[Dd]etailed [Aa]nalysis' "$f" || return 1
  return 0
}

# Validator: the report must contain all six required section headings
# (title may vary) and at least one source URL. Tolerates heading level and
# capitalization variants, but a section missing entirely means the report
# is incomplete and must be retried.
check_report_sections() {
  local f="$1"
  grep -qiE '^#{1,3} *[Ii]ntroduction' "$f" || return 1
  grep -qiE '^#{1,3} *[Ee]xecutive [Ss]ummary' "$f" || return 1
  grep -qiE '^#{1,3} *[Dd]etailed [Aa]nalysis' "$f" || return 1
  grep -qiE '^#{1,3} *[Cc]onclusions' "$f" || return 1
  grep -qiE '^#{1,3} *[Ss]ources' "$f" || return 1
  grep -qiE '^#{1,3} *[Cc]onfidence [Aa]ssessment' "$f" || return 1
  grep -qE 'https?://' "$f" || return 1
  return 0
}

# ── 5. Phase 1 — GATHER (scaffold findings early, append as you go)
run_phase "phase 1 (gather)" \
  "GATHER FACTS. Topic: ${TOPIC}. Work this way, in order: (1) Run 3-4 web_search queries with different angles (each result is compact; you may pass a URL as the query to fetch a page - clean text extract). NEVER use http_request. (2) AFTER YOUR SECOND SEARCH, immediately create ${FINDINGS} with file_write (path=${FINDINGS}, mode='write', create_dirs=true) containing the facts you have so far — for every fact include: the fact, source name, publication date if known, and URL. (3) Keep searching / fetching, and after each new finding APPEND it to ${FINDINGS} with file_write mode='append' — never mode='write' again (it would erase what you already saved). Include every market-size figure and CAGR figure found, with base year and currency. (4) When done, reply with just 'done' and the path. Do NOT write the final report yet." \
  "${WORKSPACE_HOST}/${slug}/findings.md" true "web_search:2" "" "GATHER FACTS" || exit 1

# ── 6. Phase 2 — VERIFY (math consistency, artifact-enforced)
if ! run_phase "phase 2 (verify)" \
  "VERIFY THE NUMBERS WITH THE CALCULATOR TOOL. Topic: ${TOPIC}. CRITICAL: this phase is machine-checked — the run is judged FAILED unless the execution log shows you actually CALLED the calculator tool (calculator(expression=...)) at least once. Writing the file without calling calculator counts as a failed phase. Do this now, in order: (1) Read ${FINDINGS} with file_read. (2) For EVERY CAGR, projection, and market-share figure in the findings, call the calculator tool with the math expression and READ the returned result before continuing — e.g. calculator(expression='((79.81/43.19)**(1/5)-1)*100') for a CAGR, calculator(expression='100*(1+0.15)**5') for a projection. If a claimed CAGR does not match the stated size figures, note the discrepancy. (3) ONLY AFTER all calculations are done, write the verified figures to ${NUMBERS} using ONE file_write (mode='write', create_dirs=true): a compact markdown table with one row per figure — columns: metric | base year | end year | source | formula | computed result | discrepancy note. (4) Reply with just 'done' and the path. Do NOT write the report yet." \
  "${WORKSPACE_HOST}/${slug}/numbers.md" check_numbers_table "calculator:1" "" "VERIFY THE NUMBERS"; then
  # Degrade, don't abort: a weak VERIFY phase must not throw away the GATHER
  # work. Prepend an explicit UNVERIFIED banner to numbers.md so the report
  # phases (and the reader) can see the figures were never calculator-checked.
  echo "[research] WARNING: phase 2 (verify) failed after retries — continuing with figures marked UNVERIFIED."
  numbers_host="${WORKSPACE_HOST}/${slug}/numbers.md"
  {
    printf '> **UNVERIFIED** — the calculator tool was not used during VERIFY; every figure below is model-stated only. Re-run verification before relying on any number.\n\n'
    [ -f "$numbers_host" ] && cat "$numbers_host"
  } > "${numbers_host}.tmp" && mv "${numbers_host}.tmp" "$numbers_host"
  echo "[research] ${numbers_host} marked as UNVERIFIED."
fi

# ── 7. Phase 3a — REPORT part 1 (Title..Detailed Analysis, chunked writes)
run_phase "phase 3a (report part 1)" \
  "WRITE PART 1 OF THE FINAL REPORT. Topic: ${TOPIC}. Do this now, in order: (1) Read ${FINDINGS} and ${NUMBERS} with file_read. (2) Write to ${REPORT} using file_write — CRITICAL: write in SEQUENTIAL CHUNKS because a single large write gets rejected by the tool-call JSON grammar and kills the turn. First call: file_write(path=${REPORT}, mode='write', create_dirs=true, content=# Title + blank line + ## Introduction). Then APPEND with mode='append': ## Executive Summary, then ## Detailed Analysis (split into 2 chunks if needed). Keep EVERY single write under ~1500 characters, and never use mode='write' again after the first call. Start every appended chunk with a blank line before its ## heading so sections do not glue together. (3) NUMBERS MUST MATCH ${NUMBERS}: every CAGR/projection/share must be calculator-verified; if a source's claimed CAGR differs from the computed one print both and flag it; never silently mix figures with different base years. (4) This is PART 1 only: Title, Introduction, Executive Summary, Detailed Analysis. DO NOT write Conclusions, Sources, or Confidence Assessment yet — a later step appends them. (5) Reply with 'part 1 done' and the path." \
  "${WORKSPACE_HOST}/${slug}/report.md" check_report_part1 "file_write:2" "" "WRITE PART 1 OF THE FINAL REPORT" || exit 1

# Snapshot part 1 so each part-2 attempt starts from a clean state (a failed
# attempt's appends must not pollute the next attempt).
cp -f "${WORKSPACE_HOST}/${slug}/report.md" "${WORKSPACE_HOST}/${slug}/report.part1"
echo "[research] part 1 snapshot saved (report.part1)"

# ── 8. Phase 3b — REPORT part 2 (append Conclusions..Confidence, chunked)
run_phase "phase 3b (report part 2)" \
  "WRITE PART 2 OF THE FINAL REPORT, APPENDING to the existing file. Topic: ${TOPIC}. (1) The file ${REPORT} already exists with the Title, Introduction, Executive Summary, and Detailed Analysis sections. Read ${NUMBERS} with file_read if you need the verified figures. (2) APPEND the remaining sections to ${REPORT} with file_write mode='append', one section per call, each write under ~1500 characters, each chunk STARTING WITH A BLANK LINE before its ## heading: ## Conclusions, then ## Sources & References (numbered list with publisher, title, date, URL for every claim), then ## Confidence Assessment (per-section high/medium/low with one-line justification, plus an overall assessment). NEVER use mode='write' — that would erase part 1. (3) When all three sections are appended, reply with a 1-2 paragraph summary of the complete report and the path." \
  "${WORKSPACE_HOST}/${slug}/report.md" check_report_sections "file_write:2" "${WORKSPACE_HOST}/${slug}/report.part1" "WRITE PART 2 OF THE FINAL REPORT" || exit 1

rm -f "${WORKSPACE_HOST}/${slug}/report.part1"

echo ""
echo "[research] done."
echo "  container:  ${REPORT}"
echo "  host:       ${WORKSPACE_HOST}/${slug}/report.md"
