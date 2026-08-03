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
- **asklogs/** — the `jarvis agents ask` live-trace log per phase, rebuilt
  from `trace_steps` in the CLI format (`  ↳ <tool> <k=v ...>`); the
  tool-usage gate counts these lines. `verify-degenerate.txt` preserves the
  historical broken `**` calculator expression as a regression fixture.
- **traces/** — per-trace metadata (outcome, feedback, tokens, tool-call
  histogram) for all nine HPC-run traces; the ground truth the asklogs and
  artifact fixtures derive from.

## Refresh

```bash
python3 scripts/export_trace_fixtures.py   # from repo root
```
The exporter refuses to run when sources are missing. After a refresh,
re-run `uv run pytest tests/pipeline/ -q` and check for secrets
(`tests/pipeline/test_fixture_hygiene.py` guards this in CI).
