# Trend Seeker — Daily Digest

The digest stage (D8/D9) of the Autonomous IT-Market Research Analyst: every
morning it turns a calendar day's deep-dive runs (from
[`scripts/research.sh`](../research.sh), triggered by the
[discovery engine](../discovery/README.md)) into two ready-to-publish
artifacts under `$OJ_WORKSPACE_HOST/digests/<date>/`:

```
digests/<date>/social.md          ≤500-char social post — ONLY clean runs
                                  (signal DONE, all phase gates pass, no
                                  UNVERIFIED/PARTIAL banner): hook ≤140 chars,
                                  one bullet per clean run, footer linking to
                                  the long version + verification caveat.
digests/<date>/newsletter.md      long form — one section per completed report
                                  (verbatim Executive Summary + machine-checked
                                  numbers table + takeaways + sources) plus a
                                  Caveats section that deterministically flags
                                  every UNVERIFIED / PARTIAL / no-state /
                                  FAILED run.
digests/<date>/digest-state.json  per-run digest state (gate, attempts,
                                  feedback, fidelity, parsed content) — the
                                  idempotency ledger: a run whose digest already
                                  passed is reused without another engine call.
```

## Architecture

```
scripts/digest.py            stdlib-only orchestrator (host python3 cannot
                             import openjarvis)
scripts/digest.sh            launcher: env injection + run-lock (one digest
                             at a time; a concurrent run defers)
scripts/digest_prompts/
  digest_per_run.txt         versioned single-line prompt template (C2) — kept
                             OUT of scripts/prompts/, whose templates are all
                             wired to pipeline PHASES and asserted so by
                             tests/pipeline (no orphans allowed)
tests/digest/                regression harness (fake-ask seam, no stack)
```

`scripts/digest.py` reuses the **same tested seams** as the deep-dive pipeline
from `scripts/research_phases.py` — `ask_agent` (the per-run engine call),
`bash_feedback_score` + `record_feedback` (scoring + writing the digest
feedback onto its trace), `reset_summary_memory`, `MAX_ATTEMPTS`. Nothing in
the frozen pipeline is modified; the digest only reads `signals.db`, the run
artifacts, and `traces.db`.

## Design

- **Hybrid, like the pipeline**: ONE bounded engine call per *completed* run
  (reads `report.md` + `numbers.md`, writes a strict per-run digest file via
  `file_write`), then **deterministic code-side assembly** of
  `social.md` / `newsletter.md`. The model never composes the public outputs.
- **Integrity gates mirror the pipeline (D3/D5)**:
  - *numbers fidelity* — every figure in the digest must appear **verbatim**
    in `numbers.md` (no invented, rounded, or recomputed numbers);
  - *sources fidelity* — every URL must appear in `report.md` (no invented
    citations);
  - *flagging is code-injected, never prompt-trusted* — a run whose report
    carries a `> **UNVERIFIED**` banner or a PARTIAL REPORT note is excluded
    from social and flagged in the newsletter, regardless of what the model
    writes. A soft PROVENANCE NOTE does **not** make a run unclean (soft by
    design); it surfaces as a newsletter caveat.
- **Feedback loop (TDL)**: each digest ask is scored (attempts + size via the
  same `research_lib.sh` `feedback_score`) and written onto its trace with the
  keyword `WRITE THE DAILY DIGEST ENTRY`.
- **Scope (v1)**: signal-triggered runs only (they have a `signals.db` row
  with `research_slug` + `triggered_at`). Manual `subject-*` runs predate
  `state.json` and are excluded. The date filter converts `triggered_at`
  (UTC, ISO 8601) to the **local calendar date** (UTC-3 here), so the
  00:30-next-day run cannot mis-classify the 00:00 cycle's runs.

## Quick start

```bash
# Digest of yesterday (local calendar day). Requires the stack: make boot.
./scripts/digest.sh

# A specific day.
./scripts/digest.sh --date 2026-08-08

# Re-ask the engine for every run (ignore a passing digest-state.json).
./scripts/digest.sh --force

# Offline smoke (no stack): skip the jarvis-health sanity, run a day with
# no signals so no engine call is needed.
OJ_SKIP_SANITY=1 OJ_STATE_DIR=/tmp/digest-test-state OJ_WORKSPACE_HOST=/tmp/digest-test-ws \
  ./scripts/digest.sh --date 2026-08-08
```

## Configuration

All context comes from env, with the same names `research.sh` /
`discovery.sh` read (no config file — the digest has no tunables):

| Env | Default | Meaning |
|---|---|---|
| `OJ_STATE_DIR` | `~/.openjarvis` | `signals.db`, `traces.db`, `agents.db`, lock dir `digest.lock.d` |
| `OJ_WORKSPACE_HOST` | `~/Git/openjarvis-workspace` | run dirs (input) + `digests/<date>/` (output) |
| `OJ_AGENT_NAME` | `it-market-analyst` | agent used for the per-run digest asks (the same agent that wrote the reports; the digest prompt fully constrains the task) |
| `OJ_SKIP_SANITY=1` | — | skip the `make jarvis-health` check (offline tests) |

CLI: `--date YYYY-MM-DD|yesterday|today` (default `yesterday`), `--force`.
Exit codes: `0` done or deferred (lock held), `1` stack unreachable, `2`
usage/CLI error.

## Testing

```bash
uv run pytest tests/digest/ -q
```

The harness mirrors `tests/pipeline/` (C3/C4): the deterministic leaves
(contract parser, both fidelity gates, budgeted assembly, run classification)
are tested directly; the per-run engine seam is an injected scripted fake that
writes a fixture digest file; the launcher is exercised through the bash seam
(lock release, defer-when-held, env wiring).

## Consumers for every recorded signal (D7)

1. `research_slug` + `triggered_at` rows → the daily digest's run roster
   (date-filtered on the local calendar date).
2. The digest ask's feedback (keyword `WRITE THE DAILY DIGEST ENTRY`) →
   `traces.db` via the same `record_feedback` seam the deep-dive phases use.
3. `digest-state.json` → the idempotency ledger (re-runs skip passed digests
   unless `--force`), and the source for the run's per-run digest content.
4. `social.md` / `newsletter.md` → the two publishable consumers of the day's
   machine-verified research.

## Operations

- Wired as an opencode scheduler job + host `systemd` user timer
  (`opencode-job-openjarvis-4f1aad65c715-trend-seeker-digest`), firing at
  **00:30 local** for the previous local calendar day.
- The digest holds its own run-lock (`digest.lock.d`) so two digest runs
  never contend for the single engine; discovery and the deep-dive pipeline
  are never blocked by a digest (separate locks, per §5.7 of the design doc).
- Run state lives in the scheduler job JSON under
  `~/.config/opencode/scheduler/scopes/openjarvis-4f1aad65c715/jobs/`
  (`lastRunAt` / `lastRunStatus`), not the systemd journal. Trust those files
  for health checks.
- The timer keeps the hand-fixed single-line `OnCalendar=*-*-* HH:MM:SS`
  form (the scheduler plugin's `* *-*-*` translation is rejected by
  systemd 255 — see the discovery README's Operations note).
