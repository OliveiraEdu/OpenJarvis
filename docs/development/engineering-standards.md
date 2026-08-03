# Engineering Standards

The engineering standards for OpenJarvis, written in response to real
production failures. Every rule below is a constraint adopted *because* a
failure proved it necessary — each maps to a concrete incident (see
[Where these rules come from](#where-these-rules-come-from)) and is enforced
by the regression harness in `tests/pipeline/` where possible.

**Status:** these standards apply to the on-demand research pipeline
(`scripts/research.sh`, `scripts/research_lib.sh`) and to any agentic
application code added on top of the framework. The framework core (layer 1)
already conforms; the research pipeline (layer 2) was brought up to the same
bar by this document.

---

## The unifying principle

> **Deterministic core, probabilistic shell; contracts at the boundary.**

An LLM is a probabilistic component that supplies *content and decisions*,
never *guarantees*. Anything that must hold is enforced deterministically by
code at a typed boundary — a validator, a gate, a deterministic repair, an
honest flag. The pipeline treats the model like any other fallible subsystem:
it runs checks against the model's *output*, not the model's *intentions*.

OpenJarvis corollary: **the reliability boundary is made visible.** A report
that could not be verified ships with `> **UNVERIFIED**` as its first line;
URLs that do not trace to gathered findings carry a `PROVENANCE NOTE`; a
failed phase degrades with an explicit `PARTIAL` flag instead of silently
shipping degraded output. These flags are features, not defects.

---

## Design foundations

### D1 — One bar for two layers

The framework (layer 1: agents, engines, tools, registries, telemetry) has a
mature test suite (~7,200 tests). Applications built on it (layer 2: the
research pipeline) have historically been untested glue — where every bug
lived. The same quality bar applies to both. New application code that cannot
run the standard test suite does not merge.

### D2 — The reliability boundary is code, never prompt

Prompts are advice; the model can ignore them (it has, repeatedly). Anything
that must hold — a banner present, a file written, a gate satisfied — is
enforced by script-side logic (`apply_unverified_banner`,
`mark_numbers_unverified`, the `run_phase` artifact/tool gates). If a
behavior must be guaranteed, guarantee it deterministically; use the prompt
only to improve the *quality* of what the model produces.

### D3 — Validators verify properties, not text

Validators should check what the artifact *means*, not what it *looks like*.
The trace execution log is ground truth: it records what tools were actually
called, which is stronger evidence than any reply text. Where feasible,
validators should re-evaluate — e.g. run every claimed CAGR through the
calculator — and resolve every report URL against findings. Grep-level
checks are acceptable only as a first pass, and every validator must be
covered by a fixture-derived test that pins its real behavior (including its
failure modes).

### D4 — One dialect per tool; prompt↔tool contracts are machine-checked

A tool accepts one notation (`^`, not `**`; `mode=`, not `write:`). Every
example the prompt teaches the model must be machine-checked to parse and
evaluate against the real tool (`tests/pipeline/test_prompt_calculator_contract.py`
extracts every `calculator(expression=...)` from the prompts and evaluates
it). This would have caught the `**` regression before the first live run.

### D5 — Structure from code, content from model

The model provides the *content*; the pipeline provides the *structure*.
Markdown section headings, file chunking, and blank-line separation are
deterministic concerns. When the model glues headings together
(`...servers.## Sources & References`), the pipeline repairs them
(`fix_glued_headings`) rather than relaxing the validator — and the repair
must be **idempotent** (safe on every attempt, including retries).

### D6 — Honest degrade by default

When a phase cannot meet its contract within retries, the pipeline does not
abort and does not silently pass: it completes with an explicit, machine-
readable flag (UNVERIFIED / PARTIAL / PROVENANCE NOTE) so the reader always
knows what was and was not guaranteed. A weak VERIFY must not throw away
GATHER work; a weak 3b must not throw away part-1 work.

### D7 — Every recorded signal has a consumer

Feedback is only written where something reads it. The per-phase TDL scores
feed the framework's trace-driven learning loop; a signal with no consumer
is dead code and should not be recorded. If you add a recorded signal, add
its consumer in the same change.

---

## Coding foundations

### C1 — No positional-argument plumbing

`run_phase` historically took eight positional arguments and three
name-string callbacks; every retry happened because a phase call misplaced
one of them. Phase configuration is a typed object (label, prompt, artifact,
validator, tool gate, snapshot, feedback keyword, normalize hook). Bash
scripts are thin launchers that render a typed spec; orchestration logic
lives in typed Python.

### C2 — Prompts are code

Prompts are versioned template files, rendered through a typed function —
never inline shell strings, and never nested quoting
(`bash → make CMD= → docker exec → CLI`). Prompt changes ship with a test
that extracts and validates every tool-call example they contain (D4).

### C3 — Every production failure becomes a regression test at the failing layer

Before a fix ships, capture the failure as a fixture: export the real
artifact and the real trace (`scripts/export_trace_fixtures.py`), then write
a test that replays the exact failure through the exact production code
(`tests/pipeline/` sources `scripts/research_lib.sh` via `bash -c` — no
reimplementation, no model, no network, seconds to run). The fixture corpus
is a growing history of known failure modes; the `**` calculator bug, the
glued-heading bug, and the file-write gate shortcut are all frozen as
regression fixtures today.

### C4 — Test the seams, not just the units

Every bug in this pipeline lived at a seam: prompt↔tool dialect, tool↔gate
counts, artifact↔validator anchoring, CLI↔log format. Tests must cover those
joints — e.g. the asklog fixtures are reconstructed in the exact CLI format
the gate greps, and the validators are tested against real artifacts, not
hand-crafted happy paths.

### C5 — Deterministic I/O boundary around the probabilistic core

The LLM sits behind an engine abstraction; every application code path is
testable with zero model calls. The pipeline's deterministic leaf functions
(validators, repairs, gates, banners, scoring) have no network or LLM
dependency and are fully covered offline.

### C6 — The development cycle is experiment-shaped

Inputs = prompt + config; data = execution trace; metrics = gates + feedback;
reproducibility = fixtures. A change to the pipeline is evaluated by running
its fixture-derived tests and, for prompt changes, a live run whose trace is
then exported as the next fixture. Iterate on the growing failure-mode corpus
rather than on anecdote.

### C7 — The project's values are constraints

Offline-first, hardware-aware, telemetry-native, OpenAI-compatible,
Python-first, portable shell (no GNU-only syntax), and **no secrets in
repos** (real credentials live only in gitignored files like
`deploy/docker/.env`; `tests/pipeline/test_trace_fixtures.py` guards
fixtures against key-shaped tokens). Machine-specific paths and tools must
never appear in committed code.

---

## Where these rules come from

| Incident (live run, 2026-08) | Root cause | Rule |
|---|---|---|
| VERIFY: 3 attempts, every calculator call rejected | prompt taught `**`; Rust meval backend accepts only `^` | D4, C3 |
| VERIFY: 27-turn degenerate loop, numbers.md never written | model repeated a failing call; artifact gate caught it only after retries | D3, D6 |
| 3b: 2 attempts wrote the whole report in one `file_write` | model shortcut the chunked-append workflow; `file_write:2` gate caught it | D3, C4 |
| report.md headings glued to paragraphs | model controlled formatting (structure from model) | D5 |
| model twice ignored "carry the UNVERIFIED banner" | reliability boundary in prompt, not code | D2, D6 |
| report cited fabricated URLs | provenance was not checked | D6, C3 |
| `run_phase` mis-wired across 8 positional args | untyped plumbing | C1 |
| banner logic historically orphaned `numbers.md.tmp` | fragile inline shell idiom | C3 (covered by tests) |
| no tests covered any of the above | layer-2 app code had no harness | D1, C3 |

---

## PR checklist

Before opening a pull request against the research pipeline (or any layer-2
application code), verify each item:

**Correctness**
- [ ] `uv run pytest tests/pipeline/ -q` passes (the trace-replay harness).
- [ ] Full suite passes: `uv run pytest tests/ -n auto -q --tb=short -m "not live and not cloud and not hub"`.
- [ ] `bash -n scripts/research.sh scripts/research_lib.sh` (both scripts).
- [ ] Every prompt/template tool example evaluates: `calculator(expression=...)` examples are all `^`-dialect and parse with `safe_eval` (D4).

**New failure modes (C3)**
- [ ] If the change fixes a production failure, a trace-derived fixture was exported (`python3 scripts/export_trace_fixtures.py`) and a regression test replays the failure through the real `research_lib.sh` code.
- [ ] New deterministic logic went into `research_lib.sh` (not inline in `research.sh`) so it is testable.
- [ ] New bash functions are pure: no global shell variables, explicit positional args.

**Safety (C7)**
- [ ] Fixtures contain no secrets: `grep -rE "tvly-|sk-|AKIA|ghp_" tests/pipeline/fixtures/` is empty (also guarded in CI).
- [ ] No machine-specific paths, no GNU-only shell syntax, no real credentials in committed files.

**Design**
- [ ] Reliability boundary enforced by code, not prompt (D2).
- [ ] Validator added/updated has a test pinning its real behavior, including its failure mode (D3).
- [ ] Degrade paths are explicit and honest, never silent (D6).
- [ ] Recorded signals have consumers (D7).
- [ ] Prompts are versioned templates or clearly marked prompt constants, not nested-quoted inline strings (C2).

---

## Related documents

- [Contributing guide](../../CONTRIBUTING.md) — how to submit changes.
- [Development guide](contributing.md) — code conventions and structure.
- [Agent QA runbook](../testing/agent-qa-runbook.md) — manual scenario checklists (complementary to the automated harness).
- [Design principles](../architecture/design-principles.md) — the framework's eight principles.
- [Learning architecture](../architecture/learning.md) — the Trace-Driven Learning loop the pipeline's feedback feeds.
- [Roadmap](roadmap.md) — post-training from execution traces (the consumer for recorded signals).
