# Trend Seeker — Market-Signal Discovery Engine (design)

**Status:** proposed · **Date:** 2026-08-04 · **Author:** OpenJarvis pipeline session

Builds on: the on-demand IT-market research pipeline (`scripts/research.sh` +
`scripts/research_phases.py` + `scripts/research_lib.sh` + `scripts/prompts/`,
brought to C1–C5/D3–D7 in this workspace). Source proposal: *"OpenJarvis - Trend
Seeker Research Analyst.md"* (Obsidian). Section 11 maps every engineering
standard to where this design honors it.

## 1. Context

The existing pipeline is an excellent *deep-dive* engine: given a subject, it
runs GATHER → VERIFY → PART1 → PART2 with deterministic validators, tool gates,
honest degrade, provenance checking, and per-phase feedback into the trace DB
(C7/D7). What it cannot do is **find** the subjects worth deep-diving. Today a
run starts from a human-chosen topic ("Subject: Storage systems for AI
training").

The proposal's own architecture makes this explicit: a **Discovery Engine** sits
*in front of* the specialized research agents and only triggers the full
multi-agent deep-dive when a signal crosses a threshold. That deep-dive is what
we already built. This design therefore adds the discovery front-end (Layer 1)
and a structured state handoff (Layer 2) **without touching the deep-dive
pipeline's behavior**.

### Scope decisions (user)

1. **Order:** Layer 1 (Discovery Engine) first, then Layer 2 (structured state).
2. **Collectors:** low-hanging, no-auth sources in v1; interface-level
   placeholders for the complex sources (SEC EDGAR, Reddit OAuth, job boards,
   cloud marketplaces).
3. **Trigger:** fully automatic — a signal above threshold launches
   `scripts/research.sh` headless.
4. **Cadence:** leverage Jarvis built-ins (`jarvis scheduler` cron tasks) rather
   than a custom daemon.
5. **Deliverables:** markdown reports in v1 (no interactive calculators).

### Goals

- Continuously detect high-value market signals (repo velocity, developer
  sentiment/churn, adoption telemetry, pricing changes) at near-zero marginal
  hardware cost (CPU + network only; the 6 GB VRAM budget is untouched).
- Auto-trigger the existing deep-dive pipeline on high-value signals, with a
  machine-readable linkage from signal → research run → report.
- Keep the deterministic/LLM seam identical to the pipeline (D1/D3/D5): rules,
  filters, dedupe, and thresholds are pure code; the model only scores
  relevance in a strict JSON contract.
- Give every new deterministic layer the C3 treatment: committed fixtures +
  offline tests.

### Non-goals (v1)

- No changes to the deep-dive phases, gates, validators, prompts, or degrade
  branches (Layer 0 is treated as a stable black box with a documented seam).
- No interactive deliverables, stakeholder briefings, or scenario calculators.
- No parallel multi-engine inference; the single llamacpp engine is shared
  serially (triage and deep-dive never run concurrently — see §5.7 lock).
- No new agent types or MCP servers in v1: collectors are host-side stdlib
  Python; triage uses the existing `jarvis ask` direct-engine seam.

## 2. Architecture overview

```
            jarvis scheduler (cron)         [Layer 1 — DISCOVERY, CPU/RAM only]
                 │  prompt: "run discovery cycle"
                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ discovery.sh (bash launcher, mirrors research.sh)           │
   │  └─ discovery.py (stdlib orchestrator)                      │
   │       1. collect    collectors.py   (GitHub, HN, RedditRSS, │
   │                                      PyPI, pricing-diff)    │
   │       2. filter     rules.py        (pure deterministic)    │
   │       3. store      store.py        (signals.db, dedupe)    │
   │       4. triage     triage.py       (jarvis ask → JSON,     │
   │                                      engine = existing llamacpp)│
   │       5. decide     decide.py       (thresholds, cooldowns, │
   │                                      run-lock)              │
   └───────────────┬─────────────────────────────────────────────┘
                   │ score ≥ 7 AND cooldown OK AND run-lock free
                   ▼
   [Layer 0 — DEEP-DIVE, unchanged]
   scripts/research.sh "<signal subject> | Scope: <category>"
        │  GATHER → VERIFY → PART1 → PART2 (gates, validators, feedback)
        ▼
   ~/Git/openjarvis-workspace/<slug>/report.md   (markdown deliverable)
        │
        └─► signals.db: signal status TRIGGERED→DONE/FAILED + slug + trace
```

**Layer 2 (structured state handoff)** — after each deep-dive phase, a
versioned `state.json` is written to the workspace alongside the artifacts,
formalizing the machine-readable context we already pass between phases (today
only files + feedback scores). v1: produced and fixture-tested; the future
orchestrator (query decomposition, §6.4) consumes it.

## 3. Layer 0 — the deep-dive pipeline (frozen)

Treated as a black box with one documented seam:

```
run:  scripts/research.sh "<topic>"
seam: scripts/research_phases.py drives
      make -C <root> jarvis-exec CMD="jarvis agents ask <agent> \"<prompt>\""
      (agent it-market-analyst, template deploy/templates/it_market_analyst.toml)
out:  $WORKSPACE_HOST/<slug>/{findings.md, numbers.md, report.md}
      feedback rows + traces in $STATE_DIR/traces.db
```

No source changes in v1. The only interaction from discovery is invocation +
outcome observation recorded into signals.db — where the outcome check **reuses
the pipeline's own bash validators** (`check_report_sections`, the numbers-table
evaluator) through the same `bash -c` seam, never a reimplementation
(D3/C3/C5).

## 4. Layer 1 — Discovery Engine

### 4.1 Layout

```
scripts/discovery/
  discovery.sh        bash launcher — env injection, sanity (make jarvis-health),
                      run-lock, delegates to discovery.py (mirrors research.sh)
  discovery.py        stdlib orchestrator: collect → filter → store → triage →
                      decide → trigger; CLI:  run --cycle / run --once [--source X]
  config.toml         committed defaults: thresholds, enable flags, cadence,
                      subject template, caps (no secrets)
  config.py           typed frozen dataclasses parsing config.toml (C1); Ctx.from_env
  collectors.py       one Collector per source (dataclass + fetch() -> list[Signal])
  rules.py            pure functions: evaluate(signal, metrics) -> pass|skip|pre_qualify
  store.py            signals.db schema + dedupe + queries (sqlite3, stdlib)
  triage.py           builds prompt from triage_prompt.txt, calls jarvis ask via
                      the make jarvis-exec seam, parses/validates JSON reply
  decide.py           thresholds, per-source cooldown, daily cap (run-lock is
                      launcher-side, in discovery.sh)
  prompts/triage_prompt.txt   string.Template, rendered newline-free (C2)
tests/discovery/      offline harness (fixtures + fake network layer)
tests/discovery/fixtures/     canned GitHub/HN/Reddit/PyPI payloads (C3)
```

Constraint carried over from the pipeline: **`scripts/discovery/*.py` is
stdlib-only** (host `python3`), matching C1/C5. `collectors.py` uses
`urllib.request` with an injectable opener so tests never touch the network.

### 4.2 Signal lifecycle

1. **collect** — each enabled collector polls its source once per cycle and
   returns `Signal(source, source_key, title, url, metrics: dict, raw: str)`
   candidates. `source_key` is a stable per-item id used for dedupe.
2. **filter** — `rules.py` drops noise (dotfiles, tutorials, <n stars, single-user
   rants), computes deltas (star acceleration, mention frequency), and attaches
   `pre_qualify` tags (`HIGH_VELOCITY_OS`, `CHURN_SIGNAL`, `PRICING_DIFF`,
   `ADOPTION_SPIKE`).
3. **store** — candidates are upserted into `signals.db` (dedupe on
   `(source, source_key)`). Items already TRIAGED/TRIGGERED are skipped unless
   the metric moved past a re-triage threshold.
4. **triage** — only candidates that pass a rule pre-qualification reach the LLM.
   `triage.py` renders the JSON-contract prompt and calls the existing engine
   (see §4.6). Score + category + one-line reason are written back.
5. **decide** — score ≥ `threshold` AND cooldown/daily-cap OK AND run-lock free
   → `scripts/research.sh "<subject template>"` with the signal's title/category
   substituted; the trigger + slug + outcome are recorded on the signal row. The
   outcome check reuses the pipeline's bash validators (§3), and the
   `(score, outcome)` pair is a **recorded signal with a consumer** (D7) — the
   calibration query in §4.7.

### 4.3 Collectors — v1 (low-hanging) and placeholders

| Collector | Source | Auth / rate limit | v1 | Notes |
|---|---|---|---|---|
| GitHub velocity | `api.github.com/search/repositories` (created:>…, stars:>…) | none (10 req/min unauthenticated; `GITHUB_TOKEN` env raises to 30) | **yes** | Star acceleration, contributor spikes, license/fork diversion |
| Hacker News | Firebase API (`hn.algolia.com`/firebase) + Algolia search | none | **yes** | Keyword velocity, engagement ratio (comments/upvotes) |
| Reddit RSS | subreddit RSS feeds (`r/devops`, `r/sysadmin`, `r/dataengineering`, `r/LocalLLaMA`) | none (RSS) | **yes** | Churn-phrase regex on titles; **placeholder**: OAuth JSON API for full-text sentiment |
| PyPI | `pypi.org/pypi/<pkg>/json` for a configured watch-list | none | **yes** | Download delta vs. prior cycle (ADOPTION_SPIKE) |
| Pricing page diff | `urllib` fetch + normalized hash per watched URL | none | **yes** (static HTML only) | Any change → PRICING_DIFF candidate; **placeholder**: headless-browser rendering for JS pages |
| SEC EDGAR | `data.sec.gov` | rate-limited | no | **Placeholder**: 10-K/10-Q risk-factor diffing |
| Job boards | — | auth/varied | no | **Placeholder**: keyword-spike tracking |
| Cloud marketplaces | — | auth/varied | no | **Placeholder**: listing/price changes |

Every collector implements the same `Collector` contract (`name`, `enabled`,
`fetch(now) -> list[Signal]`, `idempotent source_key`), so placeholders are
stubs that raise `NotImplementedError`-style "not wired" markers and register a
clean TODO in the collector table — no special-casing in the orchestrator.

### 4.4 Rule filters (deterministic, pure)

`rules.py` exports pure functions (unit-tested, no I/O):

- `star_acceleration(sig, window_days) -> float` — Δstars/Δt over the signal's
  recorded window; pre-qualify at >200/week.
- `contributor_spike(sig) -> bool` — repo age < 30d with >15 contributors.
- `churn_phrases(title, body) -> list[str]` — regex set ("migrating off/away
  from X", "too expensive", "deprecated", "alternatives to Y"); pre-qualify on
  ≥1 match in ≥1 thread (cross-thread threshold applied in decide).
- `engagement_ratio(points, comments) -> float` — high comments:upvotes.
- `download_delta(current, previous) -> float` — PyPI downloads vs. last cycle.
- `noise_filters(sig) -> bool` — dotfiles, demo/tutorial repos, single-user
  rant posts without engagement.

Rules carry the pre-qualification burden so the LLM only scores a handful of
candidates per cycle (energy budget: the proposal's "lightweight gathering on
CPU, semantic scoring only on real candidates").

### 4.5 signals.db (schema v1)

```sql
CREATE TABLE signals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,            -- github | hn | reddit | pypi | pricing
  source_key    TEXT NOT NULL,            -- stable per-source id (dedupe)
  title         TEXT NOT NULL,
  url           TEXT,
  metrics       TEXT NOT NULL,            -- JSON: stars, forks, delta, phrases...
  pre_qualify   TEXT,                     -- HIGH_VELOCITY_OS | CHURN_SIGNAL | ...
  score         INTEGER,                  -- LLM relevance 1-10
  category      TEXT,                     -- "db", "infra", "security", ...
  triage_reason TEXT,
  status        TEXT NOT NULL DEFAULT 'NEW',  -- NEW|TRIAGED|TRIGGERED|DONE|FAILED
  research_slug TEXT,                     -- workspace slug of the triggered run
  triggered_at  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (source, source_key)
);
CREATE INDEX idx_signals_status ON signals(status);
```

`signals.db` lives in `$STATE_DIR` (`~/.openjarvis/signals.db`) — gitignored
by location, like `traces.db`. **Never committed** (live data). Committed
fixtures carry only sanitized samples (§7).

### 4.6 LLM triage (the only model touch in Layer 1)

- Prompt template `prompts/triage_prompt.txt` — `string.Template`, rendered
  with `{source, title, metrics, category_hint}`, then **stripped to a single
  line** (the make-recipe newline bug from the pipeline applies here too, C2).
- Strict contract, mirroring D4 machine-checked contracts:

```
Reply with ONLY a JSON object: {"relevance_score": <1-10>, "category": "<kebab>", "reason": "<<=40 chars>"}
```

- Invocation: the existing engine seam — `make -C <root> jarvis-exec
  CMD="jarvis ask '<prompt>'"` (direct engine mode, no agent). Serializes with
  the deep-dive because both hit the same engine (§5.7 run-lock).
- Parsing: extract the JSON block (regex), `json.loads`, clamp score to 1–10,
  coerce category to a known enum; on any failure score = 0 and the item is
  marked TRIAGED with `triage_reason="parse_failed"` (D6: honest degrade).
- `temperature: 0.1` style low-temperature guidance in the prompt; no engine
  parameter (engine is config-resolved — the doc's `engine=` per-call idea does
  not exist in the SDK).
- **Machine-checked contract (D4):** a test extracts the JSON skeleton from
  `triage_prompt.txt` and validates it — parses as JSON, score in 1–10,
  category in the known enum — mirroring
  `test_prompt_calculator_contract.py`. The triage prompt change ships with
  that test.

### 4.7 Trigger decision (decide.py — pure, tested)

```
decision(signal) -> TRIGGER | DEFER | SKIP
  score < threshold                          → SKIP            (default threshold 7)
  score >= threshold but source in cooldown  → DEFER (bump updated_at, retry next cycle)
  source cooldown OK but daily cap reached   → DEFER
  run-lock held (deep-dive or discovery)     → DEFER
  else                                       → TRIGGER
```

Defaults in `config.toml`: `threshold = 7`, per-source cooldown (github 24 h,
reddit 24 h, hn 12 h, pypi 7 d, pricing 7 d), `max_triggers_per_day = 3`,
`re_triage_delta = 0.3` (fractional metric growth that re-opens a TRIAGED item).

Trigger runs `scripts/research.sh` with the subject template
`"{title} | Scope: {category}"` — reusing the exact launcher contract. On
completion (success or honest degrade — the pipeline always exits 0 unless
gather/part1 hard-fail), the signal row is updated to DONE/FAILED with the
workspace slug; the trace linkage is the `(agent_name=it-market-analyst, slug)`
pair already queryable in `traces.db`.

**Consumer for every recorded signal (D7).** Each discovery column has a
concrete reader shipped in the same change as the writer:
(a) `signals.db` score/status rows feed a `decide.py --calibrate` query that
reports per-category precision (`score ≥ threshold` → DONE rate), so thresholds
are tuned from data, not anecdote (C6); (b) the cycle summary line (n collected
/ n triaged / n triggered / n failed / n DONE) is the discovery agent's report
back to the scheduler task; (c) `research_slug` links each trigger to its run
and report; (d) `triage_reason`/`category` feed the discovery fixture exporter
(§6). No signal is written without its consumer.

### 4.8 Cadence — jarvis built-in scheduler

Primary: one `jarvis scheduler` cron task (task type `cron`; `jarvis scheduler
create --cron "0 */6 * * *" …`). The built-in scheduler executes an *agent
ask*, so we register a thin **discovery agent** from a new template
(`deploy/templates/trend_discovery.toml`) whose system prompt is:
"run the discovery cycle: invoke `scripts/discovery/discovery.sh --cycle` via
shell_exec and report the number of signals triaged/triggered". This mirrors
the `it_market_analyst` pattern (template → `jarvis agents create` → synced
system_prompt) and keeps everything inside `jarvis`.

Implementation detail to confirm during M5: the container must reach
`scripts/discovery/` — same bind-mount question as the deep-dive seam
(`make jarvis-exec` + `shell_exec` tool reachability). If the container cannot
see the repo, fall back to a host-side cron task via the scheduler plugin
(`schedule_job`) calling `discovery.sh --cycle` directly; decision recorded as
an open item (§9).

## 5. Layer 2 — structured state handoff

Rationale (proposal §4): pass compact structured state between phases, not raw
chat logs. We already do this implicitly (artifacts + feedback scores); Layer 2
makes it explicit and machine-readable.

### 5.1 state.json (v1 schema)

Written to `$WORKSPACE_HOST/<slug>/state.json` after every phase by
`research_phases.py`:

```json
{
  "schema": 1,
  "run_id": "<slug>",
  "topic": "<topic>",
  "phases": [
    {
      "phase": "gather",
      "attempts": 1,
      "status": "OK",                     // OK | GATE_FAIL | RETRIED | DEGRADED
      "artifact": "findings.md",
      "artifact_bytes": 1732,
      "tool_counts": {"web_search": 4, "file_write": 2},
      "feedback": 0.9,
      "gate": "pass"
    }
  ]
}
```

Values are deterministic and bash-verified (artifact bytes, gate counters,
feedback score — all already computed by `research_lib.sh`; this is a
serialization, not a new source of truth).

### 5.2 Changes to research_phases.py

- `PhaseSpec` gains an optional `state_keys` mapping (which counters/bytes to
  record). Minimal and additive; no behavior change to gates/prompts.
- After each phase, `write_state()` updates `state.json` (create + merge).
- `state.json` is removed by `research.sh` cleanup alongside `report.part1`
  (add to the `rm -f` list) so each run starts clean — or kept if we want the
  run record; decision: **keep** it, it is small and is the run's machine
  summary (matches C7 "every recorded signal has a consumer": the consumer is
  the discovery trigger bookkeeping + future orchestrator).

### 5.3 Tests (C3)

- Fixture: a committed `state.json` from the storagesys run (exported exactly
  like trace fixtures).
- Assertions: schema shape, per-phase bytes/tool_counts/feedback match the
  artifact + asklog fixtures (cross-fixture consistency, same pattern as
  `test_asklog_matches_trace_metadata_tool_counts`).

### 5.4 Future: orchestrator consumption

Not in v1. When query decomposition lands (OrchestratorAgent parsing a request
into `{subject, scope, competitors, sectors}` and fanning out parallel
`research.sh` runs), the orchestrator reads `state.json` per run — compact
payloads, no log scraping.

## 6. Testing & fixtures strategy (C3/C4/C5)

`tests/discovery/` (new, offline):

- **rules**: table-driven tests on canned metrics (pure functions).
- **store**: sqlite in `tmp_path`; dedupe upsert, status transitions.
- **decide**: threshold/cooldown/cap/lock matrix (pure).
- **triage parse**: JSON extraction, out-of-range clamping, garbage input → 0.
- **triage contract (D4)**: JSON skeleton extracted from `triage_prompt.txt` is
  validated (parses, score ∈ 1–10, category ∈ enum) — the machine-checked
  prompt↔tool contract.
- **collectors**: committed fixture payloads + injected fake `urllib` opener
  (tests never hit the network; mirrors how `tests/pipeline/` exercises
  `research_lib.sh` through the bash seam).
- **prompt rendering**: `triage_prompt.txt` renders newline-free (C2 regression,
  same shape as the pipeline's render test).
- **decide→research.sh seam (C4)**: offline test that subject-template
  substitution produces a valid `research.sh` topic whose slug matches the
  launcher's slug rule (no positional/text plumbing between the layers).
- **discovery fixture exporter (C3/C6)**: `scripts/export_discovery_fixtures.py`
  mirrors `export_trace_fixtures.py` — real signal payloads and triage replies
  (sanitized) become committed fixtures; every discovery production failure
  (collector parse break, rule edge, triage drift, trigger mis-fire) is frozen
  as a regression test at the failing layer as it happens, and prompt changes
  are followed by a live run whose triage payload is exported as the next
  fixture.
- **config typing (C1)**: `config.toml` is parsed into typed frozen dataclasses
  (mirroring `PhaseSpec`/`Ctx.from_env`); no positional-argument plumbing
  through the launcher.
- **fixture hygiene**: no secrets (`test_fixture_hygiene.py` pattern extended —
  GitHub tokens, Reddit creds never in fixtures).
- **state.json**: Layer 2 cross-consistency tests (§5.3).

Live-marked tests (skipped by default, like the pipeline's `live` marker):
one smoke that runs `discovery.sh --cycle --once --source hn` against the real
engine and asserts ≥0 signals with a valid JSON triage reply.

The full offline suite (`uv run pytest -q -m "not live and not cloud and not
hub"`) must stay green; `bash -n` on the new shell scripts.

## 7. Milestones (each: tests green, logical commit, per C6)

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Skeleton + store | `scripts/discovery/` scaffold, `signals.db` schema, store tests, `config.toml`, launcher + run-lock |
| M2 | Collectors v1 | GitHub, HN, Reddit RSS, PyPI, pricing-diff collectors + fixtures + fake-network tests; placeholder stubs registered |
| M3 | Rules + decide | `rules.py`, `decide.py`, table-driven tests |
| M4 | Triage | `triage_prompt.txt` (newline-free) + D4 contract test, `triage.py` + seam, parse tests, live-marked smoke |
| M5 | Cadence + auto-trigger | `trend_discovery` template + `jarvis scheduler` cron task, end-to-end manual run, bind-mount resolution, first real payloads exported |
| M6 | Layer 2 | `state.json` in `research_phases.py`, exported fixture, cross-consistency tests |
| M7 | Docs + calibration | `scripts/discovery/README.md`, engineering-standards "related docs" pointer, roadmap note, `--calibrate` consumer wired (D7) |

Every milestone ships with the PR checklist from `engineering-standards.md`:
`tests/pipeline/` + full offline suite green
(`uv run pytest tests/ -n auto -q --tb=short -m "not live and not cloud and not hub"`),
`bash -n` on every new shell script, fixture secret-grep clean (C7). Discovery is
layer-2 application code and is held to the same bar as the pipeline (D1).

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| API rate limits (GitHub 10/min unauth) | Token via gitignored env when present; per-source backoff; small candidate caps per cycle; dedupe on `source_key` |
| Trigger floods / GPU contention | Run-lock (discovery + deep-dive share the engine), per-source cooldown, daily cap; DEFER semantics |
| LLM triage drift (non-JSON, junk scores) | Strict JSON contract + parse-and-clamp; parse failure scores 0 (honest degrade); `(score, outcome)` pairs feed the `--calibrate` consumer shipped in the same change (D7/C6) |
| Noise / marketing content | Rule filters + exclude lists; engagement thresholds; pre-qualify gates before the LLM is ever asked |
| Scheduler container↔repo bind-mount friction | Fallback: host-side cron via scheduler plugin; recorded open item |
| 6 GB VRAM budget | Discovery is CPU/network; triage serializes with the deep-dive via the run-lock; no new model loads |
| Signal DB growth | Prune policy in config (`prune_after_days`), status-based index |

## 9. Decisions recorded

| Question | Decision |
|---|---|
| Build order | Layer 1 (discovery) then Layer 2 (state handoff) |
| Collector breadth | v1: GitHub, HN, Reddit RSS, PyPI, static pricing-diff; placeholders for SEC EDGAR, Reddit OAuth, job boards, cloud marketplaces |
| Trigger mode | Auto — threshold → `scripts/research.sh` headless |
| Cadence | Jarvis built-in scheduler (cron task + discovery agent template); host-cron fallback noted |
| Deliverables | Markdown reports (existing pipeline output) |

## 10. Open items

1. Container bind-mount of `scripts/discovery/` for the scheduler-agent
   `shell_exec` path (M5).
2. GitHub token availability (rate-limit upgrade only).
3. Reddit RSS vs OAuth JSON (v1 uses RSS; OAuth is the placeholder).
4. Exact cron cadence (default proposal: 6-hourly; pricing/pypi are
   longer-cooldown by rule, so a 6 h poll is safe).
5. `state.json` retention vs. cleanup (default: keep as run record).
6. Discovery fixture exporter sanitization rules — what counts as a "sanitized"
   signal payload for committed fixtures (C7).

## 11. Standards compliance

Map of this design to `docs/development/engineering-standards.md` (each rule is
a constraint adopted because a production failure proved it necessary):

| Standard | Where the design honors it |
|---|---|
| **Unifying principle** — deterministic core, probabilistic shell; contracts at the boundary | Rules, dedupe, thresholds, cooldowns, caps, parse-and-clamp, and trigger decision are pure code (`rules.py`, `decide.py`); the model only supplies `{score, category, reason}` inside a strict JSON contract parsed and clamped by code |
| **D1** — one bar for two layers | Discovery is layer-2 application code; every milestone ships with the standards PR checklist (§7) and a `tests/discovery/` harness |
| **D2** — reliability boundary is code, never prompt | JSON parse failure → score 0 + `triage_reason="parse_failed"` (code-enforced); trigger thresholds/cooldowns enforced by `decide.py`, not the prompt; the prompt only improves triage quality |
| **D3** — validators verify properties, not text | Trigger-outcome check reuses the pipeline's bash validators (`check_report_sections`, numbers evaluator) via the `bash -c` seam (§3); rules verify metric properties, not reply text |
| **D4** — one dialect per tool; contracts machine-checked | Triage JSON contract is machine-checked by a test that extracts the skeleton from `triage_prompt.txt` and validates it (§4.6, §6) — same pattern as `test_prompt_calculator_contract.py` |
| **D5** — structure from code, content from model | Categories, thresholds, cooldowns, and the signal schema are code/committed config; the model fills score/category/reason only; `category` is coerced to a known enum |
| **D6** — honest degrade by default | Parse failure, collection failure, and trigger failure are recorded explicitly (score 0, `FAILED` status, reason) — never silently skipped |
| **D7** — every recorded signal has a consumer | Each column has a concrete reader shipped in the same change: `--calibrate` precision query, cycle summary line, `research_slug` linkage, exporter input (§4.7) |
| **C1** — no positional-argument plumbing | `config.toml` parsed into typed frozen dataclasses mirroring `PhaseSpec`/`Ctx.from_env`; `Collector` is a typed contract; `discovery.sh` is a thin launcher (§4.1, §6) |
| **C2** — prompts are code | `prompts/triage_prompt.txt` is a versioned template rendered via `string.Template` and stripped newline-free; the launcher carries no prompt text (§4.6) |
| **C3** — every production failure becomes a regression test at the failing layer | `export_discovery_fixtures.py` mirrors `export_trace_fixtures.py`; live failures (collector parse, rule edge, triage drift, trigger mis-fire) are frozen at the failing layer as they occur (§6) |
| **C4** — test the seams, not just the units | Fixture-derived collector tests through the fake-network layer, decide→`research.sh` seam test (slug contract), state.json cross-fixture consistency, D4 contract test (§6) |
| **C5** — deterministic I/O boundary | Rules/store/decide are pure offline code; collectors take an injectable `urllib` opener; only `collectors.py` (real fetch) and `triage.py` (jarvis ask) touch network/LLM, and both are seam-tested |
| **C6** — development cycle is experiment-shaped | Inputs = collector config + triage prompt; data = `signals.db` + traces; metrics = gates + `--calibrate` precision; reproducibility = fixtures; triage prompt changes are followed by a live run whose payload becomes the next fixture |
| **C7** — values are constraints | Offline-first tests; hardware-aware (single engine, run-lock, 6 GB VRAM budget untouched); telemetry-native (traces, feedback, `signals.db`); OpenAI-compatible (`jarvis ask` seam); Python-first (stdlib-only `scripts/discovery/*.py`); portable shell (`discovery.sh` is POSIX-only, no GNU-only syntax); no secrets in repos (`signals.db` gitignored by location in `$STATE_DIR`, GitHub token via env, fixture secret-grep) |
