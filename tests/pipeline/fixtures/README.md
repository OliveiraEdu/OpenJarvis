# Pipeline regression fixtures (trace-replay)

Real artifacts and execution traces from the on-demand research pipeline
(`scripts/research.sh`), exported so the offline harness in this directory
can replay production failures without a model or network (**C3**). The
tests run the SAME bash functions from `scripts/research_lib.sh` that the
live pipeline uses.

## Origin

- **hpc/** — `Subject: High Performance Computing Servers` run, 2026-08-03
  (workspace `subject-high-performance-computing-serve`). The run completed
  end-to-end with degradation: VERIFY looped 3x (calculator called but
  numbers.md never written -> UNVERIFIED banner), 3b passed on the 3rd
  attempt, provenance flagged 5/5 fabricated URLs.
- **arm/** — `Subject: ARM Processors (CPU) use on servers` run, 2026-08-03
  (workspace `subject-arm-proceesors-cpu-use-on-server`). Pre-fix artifact:
  `report.md` has `## Sources & References` and `## Confidence Assessment`
  glued to paragraph text (regression fixture for `fix_glued_headings`);
  `report.part1` is the clean part-1 snapshot.
- **edgeai/** — `Subject: Edge AI inference chips market` run, 2026-08-03
  (workspace `subject-edge-ai-inference-chips-market-s`). The first run on
  the typed Python launcher (research_phases.py): GATHER and VERIFY passed
  first-try with the canonical-`^`-dialect prompt, and 3a exposed a new
  failure mode — the model drifted its file_write path to a wrong slug
  (`subject-edge-inference-chips-market-s`, dropped `ai-`) so the gate
  failed and the run aborted honestly; on retry 3a and 3b passed. The
  provenance check flagged 10/10 fabricated report URLs.
- **storagesys/** — `Subject: Storage systems for AI training` run, 2026-08-04
  (workspace `subject-storage-systems-for-ai-training-`). The first fully
  clean end-to-end run: all four phases passed on the first attempt, the
  report contains all six sections, provenance flagged 0/1 fabricated
  URLs (the one `www` link traced back to its source), and no glued
  headings (gather `fix_glued_headings` normalize active). Feedback
  0.9 / 0.9 / 0.9 / 1.0.
- **asklogs/** — the `jarvis agents ask` live-trace log per phase, rebuilt
  from `trace_steps` in the CLI format (`  ↳ <tool> <k=v ...>`); the
  tool-usage gate counts these lines. `verify-degenerate.txt` preserves the
  historical broken `**` calculator expression as a regression fixture.
- **traces/** — per-trace metadata (outcome, feedback, tokens, tool-call
  histogram) for every phase trace of the hpc, edgeai, and storagesys
  runs; the ground truth the asklogs and artifact fixtures derive from.
- **state/** — the Layer 2 `state.json` (design §5) machine summary of
  the clean storagesys run, reconstructed by this exporter from the
  same ground-truth sources production uses: artifact bytes, tool
  counts over the asklogs (`count_tool_calls`), and the feedback the
  run actually recorded on its traces. Note: part1's `report.md` was
  later merged by part2, so its phase-time bytes (and thus
  `feedback_score`) are not derivable from the fixtures — the recorded
  trace feedback is the ground truth there. Re-running the exporter
  produces zero diff.

## Refresh

```bash
python3 scripts/export_trace_fixtures.py   # from repo root
```
The exporter refuses to run when sources are missing. After a refresh,
re-run `uv run pytest tests/pipeline/ -q` and check for secrets
(`tests/pipeline/test_fixture_hygiene.py` guards this in CI).
