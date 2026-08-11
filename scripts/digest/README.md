# Trend Seeker — Daily Digest

The digest stage (D8/D9) of the Autonomous IT-Market Research Analyst: every
morning it turns a calendar day's deep-dive runs (from
[`scripts/research.sh`](../research.sh), triggered by the
[discovery engine](../discovery/README.md)) into two ready-to-publish
artifacts under `$OJ_WORKSPACE_HOST/digests/<date>/`:

```
digests/<date>/social.md          ≤500-char social post (X/LinkedIn) — every
                                  completed run whose digest passed, hook
                                  ≤140 chars + one labeled novelty bullet per
                                  run; UNVERIFIED/PARTIAL runs are included
                                  with an explicit footer marker ("some
                                  figures unverified"); the footer is a real
                                  post footer ("AI-generated · verify before
                                  acting"), never a local filesystem path.
digests/<date>/newsletter.md      long form, novelty-first — one section per
                                  completed report (status, verbatim Executive
                                  Summary, "What's notable" from the digest,
                                  inline caveats, sources), plus an "Also
                                  flagged" list for runs without a report and
                                  an appendix with the machine-verified
                                  numbers tables (clean runs only, verbatim
                                  from numbers.md).
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
- **Novelty-first contract (HOOK/NOVELTY/SPEC/SOURCE)**: the digest entry
  captures what is genuinely *new* about the discovery (capabilities, design
  or business decisions, specs) — market-size/CAGR projections belong in the
  appendix, not the entry.
- **Integrity gates mirror the pipeline (D3/D5)**:
  - *figure grounding* — every figure in the digest's claim lines
    (HOOK/NOVELTY/SPEC) must appear **verbatim** in `numbers.md` **or**
    `report.md` (no invented, rounded, or recomputed numbers, no made-up
    specs); SOURCE lines are exempt because URLs are covered by sources
    fidelity;
  - *sources fidelity* — every URL must appear in `report.md` (no invented
    citations);
  - *flagging is code-injected, never prompt-trusted* — a run whose report
    carries a `> **UNVERIFIED**` banner or a PARTIAL REPORT note is still
    shareable (its digest can ground figures in `report.md`) but is marked in
    the social footer ("some figures unverified") and gets inline newsletter
    caveats. A soft PROVENANCE NOTE does **not** make a run unclean (soft by
    design); it surfaces as a newsletter caveat.
- **Feedback loop (TDL)**: each digest ask is scored (attempts + size via the
  same `research_lib.sh` `feedback_score`) and written onto its trace with a
  **slug-scoped** keyword (`WRITE THE DAILY DIGEST ENTRY FOR <slug>`) — so the
  score always lands on the run's own trace and can never overwrite another
  run's (when an ask is answered as a session continuation and its trace never
  carried the prompt, feedback is skipped honestly, best-effort by design).
- **Re-ask cooldown (deterministic)**: when a run's artifacts (`numbers.md` ∪
  `report.md`) contain no figure tokens at all and the digest fails the figure
  gate, re-asking is provably futile — any figure-bearing digest can never
  pass grounding, and the only passable form (a figure-less entry) just
  failed — so the run fails after that one attempt instead of burning the
  full `MAX_ATTEMPTS`. A figure-less digest CAN still pass on attempt 1, and
  sources-fidelity failures still retry normally (a URL can be fixed by
  re-asking). Report-grounding already makes the common placeholder case moot
  (`liquidai` passes via figures copied from `report.md`); this is the safety
  net for artifacts that carry no figures anywhere.
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

## Open items (pending user decisions)

- **Fresh-thread asks.** `jarvis agents ask` can resume the agent's hot
  thread ("Current date: …\n\nContinue your assigned task.") instead of
  delivering the digest prompt. Hit in the first production fire (2026-08-10
  00:30): the 00:30 slot collides with the overnight deep-dive pipeline, so
  all 3 attempts of `liquidai` resumed the thread mid-run. The digest handles
  it honestly (retries, flag, feedback-skip) but the root fix is a
  fresh-thread ask flag (no CLI flag exists today).
- **Strict-fidelity revert option.** `figure_fidelity` accepts one trailing
  `%` / leading `$` via `_figure_core()`; strict token-verbatim (including
  the unit) would fail most real tables that print bare computed results.
  Reverting is a one-line change in `digest.py`.

## Operations

- Wired as a Jarvis built-in scheduler task (design §4.8 — no OpenCode
  scheduler plugin involved) running on the host `systemd` user unit
  `openjarvis-scheduler.service` (see `scripts/scheduler/jarvis-host` and
  `deploy/systemd/openjarvis-scheduler.service`), firing at **00:30 local**
  (`30 3 * * *` UTC) for the previous local calendar day.
- The digest holds its own run-lock (`digest.lock.d`) so two digest runs
  never contend for the single engine; discovery and the deep-dive pipeline
  are never blocked by a digest (separate locks, per §5.7 of the design doc).
- Run state lives in the scheduler DB (`~/.openjarvis/scheduler.db`, task
  `28c4b81a96e742ed`, `last_run`), not in the systemd journal. Trust that for
  health checks:
  `OPENJARVIS_CONFIG=$HOME/.openjarvis/config.host.toml jarvis scheduler list`.
