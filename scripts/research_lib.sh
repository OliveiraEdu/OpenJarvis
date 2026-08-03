# ─────────────────────────────────────────────────────────────────────────────
# OpenJarvis — research pipeline library (pure, deterministic leaf functions)
#
# Single source of truth for every deterministic check/repair used by the
# on-demand research pipeline (scripts/research.sh). Kept as plain bash
# functions with explicit positional arguments so the SAME code that runs in
# production is exercised by the offline regression harness
# (tests/pipeline/, which sources this file via `bash -c`).
#
# Contract for every function here:
#   - NO global shell variables: every input arrives as an explicit argument
#     (the one historical exception — check_sources_provenance reading
#     ${WORKSPACE_HOST}/${slug}/findings.md implicitly — was removed so the
#     function is testable and reusable).
#   - Deterministic: same inputs -> same outputs, no network, no LLM.
#   - Side effects are limited to the artifact path passed in, and are
#     idempotent where they mutate (banner prepend, heading repair).
#   - Exit 0 = the check passed / the repair succeeded; exit 1 = failed.
#     Diagnostics go to stdout, never stderr, so callers can tee them.
#
# Add a new check here before wiring it into research.sh, then cover it in
# tests/pipeline/ with a real trace-derived fixture (C3).
# ─────────────────────────────────────────────────────────────────────────────

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

# Normalize hook for the report phases: the model sometimes glues a markdown
# heading to the end of the previous paragraph ("...ARM-based servers.##
# Sources & References") with no blank line. check_report_sections anchors on
# line-start headings, so a content-complete report would fail the gate
# purely over formatting. Insert a blank line before any heading whose whole
# hash run is glued directly to text; the leading title, already-separated
# headings, and headings inside code blocks are untouched. Idempotent (safe
# on every attempt, including retries and the degrade path). NB: the
# lookbehinds must exclude '#' too — matching at the 2nd+ '#' of a line-start
# heading would eat a hash and break idempotency.
fix_glued_headings() {
  local f="$1"
  [ -f "$f" ] || return 0
  python3 - "$f" <<'PYEOF'
import re
import sys

p = sys.argv[1]
with open(p, encoding="utf-8") as fh:
    text = fh.read()
fixed = re.sub(r"(?<=.)(?<![#\n])(#{1,6} )(?!#)", r"\n\n\1", text)
with open(p, "w", encoding="utf-8") as fh:
    fh.write(fixed)
PYEOF
}

# Provenance check (SOFT — never fails the run): every source URL in the
# report should trace back to findings.md, which only stores URLs returned
# by web_search. The model tends to fabricate plausible-looking URLs (e.g.
# .../report-...-123456789.html); any report URL with no match in findings is
# printed and a caveat is appended to the artifact, so fabrication is visible
# to the reader instead of silently shipping as a citation.
# Signature: check_sources_provenance <report> <findings>
check_sources_provenance() {
  local f="$1" findings="$2"
  local report_urls findings_urls norm total=0 unmatched=0 url
  report_urls="$(grep -oE 'https?://[^ )>]+' "$f" 2>/dev/null | sed 's/[.,;:)]*$//' || true)"
  findings_urls="$(grep -oE 'https?://[^ )>]+' "$findings" 2>/dev/null | sed 's/[.,;:)]*$//' || true)"
  if [ -z "$report_urls" ]; then
    echo "[research] provenance: no URLs found in report"
    return 0
  fi
  while IFS= read -r url; do
    [ -n "$url" ] || continue
    total=$((total + 1))
    norm="${url%/}"
    if ! grep -qF -- "$norm" <<<"$findings_urls"; then
      unmatched=$((unmatched + 1))
      echo "[research] provenance: UNMATCHED source URL -> $url"
    fi
  done <<<"$report_urls"
  echo "[research] provenance: ${unmatched}/${total} report URL(s) not found in findings.md"
  if [ "$unmatched" -gt 0 ]; then
    printf '\n> **PROVENANCE NOTE** — %s of %s source URL(s) in this report were not found in the gathered findings; they may be fabricated — verify every URL before citing.\n' "$unmatched" "$total" >> "$f"
    echo "[research] appended provenance caveat to $(basename "$f")"
  fi
  return 0
}

# Tool-usage gate helper: how many times did the captured ask trace call
# `<tool>`? The CLI live trace prints one "  ↳ <tool> ..." line per tool call
# (see src/openjarvis/cli/agent_cmd.py), so a line count is the faithful
# signal. Prints the count (0 when absent) and always exits 0 — the caller
# compares against the phase's minimum.
count_tool_calls() {
  local asklog="$1" tool="$2"
  grep -c "↳ ${tool}" "$asklog" 2>/dev/null || true
}

# Degrade path for a failed VERIFY phase: mark numbers.md as UNVERIFIED so
# the report phases (and the reader) can see the figures were never
# calculator-checked. Preserves any partial numbers content. Atomic
# (write .tmp then mv); the explicit `if` avoids the orphaned-.tmp bug where
# `[ -f x ] && cat x` short-circuited with exit 1 and skipped the mv.
mark_numbers_unverified() {
  local numbers="$1"
  {
    printf '> **UNVERIFIED** — figures could not be machine-verified (calculator gate not satisfied); every figure below is model-stated only. Re-run verification before relying on any number.\n\n'
    if [ -f "$numbers" ]; then cat "$numbers"; fi
  } > "${numbers}.tmp"
  mv -f "${numbers}.tmp" "$numbers"
}

# Deterministic UNVERIFIED banner (script-side, phase 9): the model has twice
# ignored the prompt instruction to carry the phase-2 UNVERIFIED banner into
# the report. If numbers.md was degraded, prepend the banner to report.md
# regardless of model compliance so the reader can see the figures are not
# machine-verified. Idempotent: never double-prepends.
# Signature: apply_unverified_banner <report> <numbers>
apply_unverified_banner() {
  local report="$1" numbers="$2"
  if grep -q '^> \*\*UNVERIFIED\*\*' "$numbers" 2>/dev/null \
     && ! grep -q '^> \*\*UNVERIFIED\*\*' "$report" 2>/dev/null; then
    local tmp_banner
    tmp_banner="$(mktemp)"
    printf '> **UNVERIFIED** — figures in this report could not be machine-verified; every figure is model-stated only. Re-run verification before relying on any number.\n\n' > "$tmp_banner"
    cat "$report" >> "$tmp_banner"
    mv -f "$tmp_banner" "$report"
    echo "[research] prepended UNVERIFIED banner to $(basename "$report") (deterministic)"
  fi
}

# TDL per-phase score [0,1] derived from signals already collected (retry-free
# run means the model did not shortcut; artifact size means substance).
# Success: base 0.6 + attempts bonus (0.2/0.1/0) + size bonus (0.2/0.1/0),
# capped at 1.0. Failed phase: kept well below any passing phase regardless of
# artifact size — size alone must never mask a broken workflow (cap 0.3).
# Signature: feedback_score <attempts> <artifact_size> <passed: yes|no>
feedback_score() {
  local attempts="$1" size="$2" passed="${3:-yes}"
  local attempts_bonus=0.0 size_bonus=0.0
  case "$attempts" in
    1) attempts_bonus=0.2 ;;
    2) attempts_bonus=0.1 ;;
    *) attempts_bonus=0.0 ;;
  esac
  if [ "$size" -ge 4000 ]; then
    size_bonus=0.2
  elif [ "$size" -ge 1500 ]; then
    size_bonus=0.1
  fi
  if [ "$passed" = "no" ]; then
    python3 -c "print(f'{min(0.3, 0.2 + $size_bonus):.3f}')"
  else
    python3 -c "print(f'{min(1.0, 0.6 + $attempts_bonus + $size_bonus):.3f}')"
  fi
}
