# Trend Seeker — Discovery Engine

The discovery half of the Autonomous IT-Market Research Analyst (design:
[`docs/design/2026-08-04-trend-seeker-discovery-engine-design.md`](../../docs/design/2026-08-04-trend-seeker-discovery-engine-design.md),
§4). It watches market sources, filters noise, scores candidates with the
local LLM, decides what deserves a full deep-dive, and triggers
[`scripts/research.sh`](../research.sh) — the frozen deep-dive pipeline —
when a signal clears the bar.

## Architecture

```
collectors.py   fetch candidates (github, hn, hf, reddit, pypi, pricing;
                placeholders for sec_edgar, reddit_oauth, job_boards,
                cloud_marketplaces)                    §4.3
rules.py        deterministic noise filters + pre_qualify tags   §4.4
store.py        signals.db: dedupe on (source, source_key), status
                transitions NEW -> TRIAGED -> TRIGGERED -> DONE/FAILED  §4.5
triage.py       LLM score (1-10) + category + one-line reason, strict
                JSON contract; parse failure scores 0 (honest degrade)  §4.6
decide.py       TRIGGER / DEFER / SKIP table + the --calibrate precision
                query (D7 consumer)                    §4.7
trigger.py      decide -> research.sh seam; slugify mirrors research.sh  §4.7
discovery.py    CLI orchestrator: run --cycle, stats, calibrate, hf, signals,
                timeline                                            §4.2
discovery.sh    launcher: env injection + run-lock (one cycle at a time)
config.toml     committed defaults, no secrets (C7)     §4.1
```

Every module is **stdlib-only** (host `python3` cannot import `openjarvis`),
pure w.r.t. the injected `now` timestamp, and testable with zero network
(`tests/discovery/` uses a `FakeOpener`). The deep-dive pipeline is never
modified from here — `research.sh` is a frozen contract.

## Quick start

```bash
# Full cycle (network + engine). Requires the stack: make boot.
./scripts/discovery/discovery.sh run --cycle

# Narrow a collection pass to one source (no triage changes).
./scripts/discovery/discovery.sh run --once --source hn

# Offline pass: no network, no engine, no triggering.
OJ_OFFLINE=1 ./scripts/discovery/discovery.sh run --cycle

# Report: signals.db counts by status.
./scripts/discovery/discovery.sh stats

# Calibrate: per-category decision precision (score >= threshold -> DONE
# rate over launched runs) — the D7 consumer that turns (score, outcome)
# pairs into a threshold-tuning signal.
./scripts/discovery/discovery.sh calibrate

# Read the HuggingFace discovery results: metrics decoded, sorted by
# downloads desc. --all shows every row; --top N caps it (default 10).
./scripts/discovery/discovery.sh hf --top 10
./scripts/discovery/discovery.sh hf --all

# Generic reader: any source, metrics decoded, sorted by that source's
# primary metric (github->stars, hn->points, pypi->downloads_last_week,
# hf->downloads; pricing keeps id order). No --source = one block per source.
./scripts/discovery/discovery.sh signals --source github --top 20
./scripts/discovery/discovery.sh signals --all

# Timeline: chronological reference of every research artifact (signal-
# triggered deep-dives AND on-demand subject runs), each with its signal
# chain (first_seen -> pre_qualify -> triage -> trigger), phase windows from
# traces.db, artifact mtimes, and outcome. FAILED runs are kept (evidence the
# gate works); all times render local UTC-3.
./scripts/discovery/discovery.sh timeline

# The scheduler cycle ledger (every discovery-cycle fire, empty cycles
# included) is included automatically when the opencode scheduler runs dir
# exists; OJ_SCHEDULER_RUNS overrides it for other layouts (C7: the default
# is derived by glob, never a hardcoded path).
OJ_SCHEDULER_RUNS="/custom/path/to/runs" ./scripts/discovery/discovery.sh timeline
```

## Configuration

`config.toml` holds committed defaults only — no secrets, no machine paths.
Secrets (e.g. `GITHUB_TOKEN` for rate-limit headroom) live in gitignored env
files. The state dir and workspace come from `OJ_STATE_DIR` /
`OJ_WORKSPACE_HOST` (same names `research.sh` reads).

| Key | Meaning |
|---|---|
| `[discovery] threshold` | LLM score (1-10) at/above which a signal triggers (§4.7) |
| `[discovery] max_triggers_per_day` | global cap protecting the single engine |
| `[discovery] re_triage_delta` | fractional metric growth that re-opens a TRIAGED item |
| `[cooldown] <source>` | per-source seconds between triggers (§4.7) |
| `[collectors] enabled` | sources fetched this cycle |
| `[collectors.<name>]` | per-collector settings (queries, floors, caps) |

## Testing

```bash
uv run pytest tests/discovery/ -q -m "not live and not cloud and not hub"
```

The offline harness exercises the same pure modules the live engine runs
(C3): fake-network collector tests, table-driven rules/decide tests, the
triage JSON-contract parse tests, and the bash seam for `discovery.sh`.
Real production payloads are replayed from `tests/discovery/fixtures/`
(regenerated with `python3 scripts/export_discovery_fixtures.py` — §6).

`tests/discovery/test_fixture_hygiene.py` guards the fixtures against
secret-shaped tokens and URL-bearing metrics (C7, §10.6).

## Consumers for every recorded signal (D7)

Each column of `signals.db` has a concrete reader shipped with its writer:

1. `(score, category, status)` rows → `discovery.sh calibrate` (per-category
   precision, this README's parent project).
2. The cycle summary line (`collected / triaged / triggered / failed /
   DONE`) → the scheduler task's report back.
3. `research_slug` → links each trigger to its deep-dive workspace and
   `traces.db` run.
4. `triage_reason` / `category` / sanitized metrics → the fixture exporter.
5. Every `signals` row → `discovery.sh hf` and `discovery.sh signals
   --source X` (metrics decoded, sorted by the source's primary metric).
6. Every research artifact → `discovery.sh timeline` (chronological run
   reference: signal chain, phase windows from `traces.db`, artifact mtimes,
   outcome — local UTC-3; scheduler cycle ledger included automatically,
   `OJ_SCHEDULER_RUNS` overrides the launcher-derived default).

## Milestones

See design §7. M1–M8 are implemented; the cron (host `systemd` user timer,
00/06/12/18) runs the live cycle.

## Operations

- `systemctl --user list-timers` → two user timers under
  `opencode-job-openjarvis-4f1aad65c715-*`:
  - `trend-seeker-discovery`: runs the live cycle at 00/06/12/18.
  - `trend-seeker-outcome-check`: 20 min after the 12:00/18:00 cycles, snapshots
    `signals.db` counts by status into its scheduler log (works even if the
    engine is down, since it only reads the DB).
- Run state lives in the scheduler job JSONs under
  `~/.config/opencode/scheduler/scopes/openjarvis-4f1aad65c715/jobs/`
  (`lastRunAt` / `lastRunStatus`), not in the systemd journal. Trust those files
  for health checks.
- Both timers were hand-fixed (2026-08-05/06): the scheduler plugin's
  `OnCalendar=* *-*-* …` translation is rejected by systemd 255; keep the valid
  single-line `OnCalendar=*-*-* HH,HH:MM:SS` form.
