#!/usr/bin/env python3
"""Export real research-pipeline traces + artifacts into test fixtures.

PURPOSE (C3 — trace-replay fixtures): the offline harness in
tests/pipeline/ exercises the SAME deterministic bash functions that
scripts/research.sh runs in production (validators, heading repair,
provenance, tool gate, banners, feedback scoring). This script turns real
production state — the artifacts a live run produced and the execution
trace a phase left behind — into committed fixtures, so a production failure
becomes a permanent regression test at the failing layer.

WHAT IT EXPORTS (fixtures tree under tests/pipeline/fixtures/):
  artifacts/<run>/            the report/numbers/findings files a live run
                              produced (drives validator + provenance tests)
  asklogs/<name>.txt          the `jarvis agents ask` live-trace log for a
                              phase, reconstructed from trace_steps. The CLI
                              prints one "  ↳ <tool> <k=v ...>" line per tool
                              call (src/openjarvis/cli/agent_cmd.py:
                              _format_tool_args + line 378), so the gate tests
                              count real production-shaped logs.
  traces/<trace_id>.json      per-trace metadata (outcome, feedback, token
                              counts, tool-call histogram) — the ground-truth
                              record the artifact/asklog fixtures derive from.
  README.md                   origin + refresh instructions (auto-written).

USAGE (from repo root):
    python3 scripts/export_trace_fixtures.py
    python3 scripts/export_trace_fixtures.py --db PATH --workspace PATH

The exporter refuses to run when a required source is missing, so it cannot
silently produce empty fixtures. Committed fixtures are the record; re-run
only to refresh them after a new live run. Fixtures must contain no secrets
(grep for tvly-/sk- tokens) — see tests/pipeline/test_fixture_hygiene.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "pipeline" / "fixtures"

DEFAULT_DB = Path.home() / ".openjarvis" / "traces.db"
DEFAULT_WORKSPACE = Path.home() / "Git" / "openjarvis-workspace"

# Live run workspace dirs (container /workspace/<slug> bind mount) -> fixture
# subdir name. These are the runs that surfaced the failures under test.
ARTIFACT_RUNS = {
    "hpc": "subject-high-performance-computing-serve",
    "arm": "subject-arm-proceesors-cpu-use-on-server",
    "edgeai": "subject-edge-ai-inference-chips-market-s",
    "storagesys": "subject-storage-systems-for-ai-training-",
}
ARTIFACT_FILES = ["findings.md", "numbers.md", "report.md", "report.part1"]

# asklog fixture name -> trace that produced it (HPC run, 2026-08-03).
ASKSLOGS = [
    ("gather-ok", "d528115ea38a4fd1"),  # 5 web_search + 5 file_write
    ("verify-degenerate", "65598dcad07343f8"),  # 2 calculator, 0 file_write
    ("part1-three-writes", "dec136a37df44c75"),  # 3 file_write (write+append)
    ("part2-one-write", "7a28b8aae48a4346"),  # 1 file_write -> gate FAILS
    ("part2-three-writes", "bb53823f3cee418d"),  # 3 file_write -> gate passes
    # edgeai run, 2026-08-03 (first run on the typed Python launcher).
    ("edgeai-gather", "d7b88a51de054c99"),  # 4 web_search + 3 file_write
    ("edgeai-verify", "aff4155eecab4d4f"),  # calculator + numbers.md (0.8)
    (
        "edgeai-part1-slug-drift",
        "9fe872cd42b34067",
    ),  # writes drift to wrong slug -> gate FAILS
    ("edgeai-part1-ok", "4a0a5dad9f3d4ff9"),  # 5 file_write, single path (0.9)
    ("edgeai-part2", "b2165aac5c2f4e3b"),  # 3 appends from part1 snapshot (1.0)
    # storagesys run, 2026-08-04 (first fully clean end-to-end run: all four
    # phases first-try, provenance 0/1 unmatched, no glued headings).
    ("storagesys-gather", "08a649271cb142de"),  # 4 web_search + 2 file_write (0.9)
    (
        "storagesys-verify",
        "6ca6bd98241a404d",
    ),  # file_read + 2 calculator + 1 write (0.9)
    ("storagesys-part1", "5c3a606a5bb041e3"),  # 3 file_write, single path (0.9)
    ("storagesys-part2", "370cd3c65c74462a"),  # 3 appends -> gate passes (1.0)
]

# Every phase trace of the exported runs, for the per-trace metadata record.
TRACE_IDS = [
    "d528115ea38a4fd1",  # gather      (feedback 1.0)
    "65598dcad07343f8",  # verify 1    (degenerate loop)
    "1cf051ae71994ffb",  # verify 2    (degenerate loop)
    "f2505e725d79400a",  # verify 3    (feedback 0.2)
    "d60f6d29d7ef4552",  # 3a "what task?" continuation
    "dec136a37df44c75",  # 3a success  (feedback 0.8)
    "7a28b8aae48a4346",  # 3b attempt 1 (1 write -> gate)
    "05870f2b0f004bf8",  # 3b attempt 2 (1 write -> gate)
    "bb53823f3cee418d",  # 3b attempt 3 (3 writes, feedback 0.8)
    # edgeai run traces (5 phase asks + 2 executor continuations).
    "d7b88a51de054c99",  # gather      (feedback 0.8)
    "aff4155eecab4d4f",  # verify      (feedback 0.8)
    "6b35140208ab403b",  # 3a attempt continuation (short tick, no artifact)
    "9fe872cd42b34067",  # 3a attempt 3, slug-drift (feedback 0.2)
    "6a1773a2578d44a4",  # 3a attempt continuation (short tick, no artifact)
    "4a0a5dad9f3d4ff9",  # 3a retry      (feedback 0.9)
    "b2165aac5c2f4e3b",  # 3b            (feedback 1.0)
    # storagesys run traces (2026-08-04, fully clean run).
    "08a649271cb142de",  # gather        (feedback 0.9)
    "6ca6bd98241a404d",  # verify        (feedback 0.9)
    "5c3a606a5bb041e3",  # 3a            (feedback 0.9)
    "370cd3c65c74462a",  # 3b            (feedback 1.0)
]


def _fmt_tool_args(args) -> str:
    """Mirror src/openjarvis/cli/agent_cmd.py:_format_tool_args exactly."""
    if not isinstance(args, dict):
        return ""
    parts = [f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2]]
    return " " + " ".join(parts) if parts else ""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""


def export_artifacts(workspace: Path) -> list[str]:
    exported = []
    for sub, dirname in ARTIFACT_RUNS.items():
        src_dir = workspace / dirname
        if not src_dir.is_dir():
            raise SystemExit(f"[export] missing source workspace dir: {src_dir}")
        dst_dir = FIXTURES / "artifacts" / sub
        dst_dir.mkdir(parents=True, exist_ok=True)
        found = 0
        for name in ARTIFACT_FILES:
            src = src_dir / name
            if not src.is_file():
                continue
            shutil.copy2(src, dst_dir / name)
            found += 1
            exported.append(f"{dst_dir.relative_to(REPO_ROOT)}/{name}")
        if found < 3:
            raise SystemExit(
                f"[export] {src_dir}: only {found} of {len(ARTIFACT_FILES)} artifacts "
                "present — refusing to write a partial fixture tree"
            )
    return exported


def export_asklogs(con: sqlite3.Connection) -> list[str]:
    exported = []
    ask_dir = FIXTURES / "asklogs"
    ask_dir.mkdir(parents=True, exist_ok=True)
    for name, trace_id in ASKSLOGS:
        rows = con.execute(
            "SELECT input FROM trace_steps WHERE trace_id=? AND step_type='tool_call'"
            " ORDER BY step_index",
            (trace_id,),
        ).fetchall()
        if not rows:
            raise SystemExit(f"[export] trace {trace_id} has no tool_call steps")
        lines = []
        for (inp,) in rows:
            d = json.loads(inp)
            lines.append(
                f"  \u21b3 {d.get('tool', '?')}{_fmt_tool_args(d.get('arguments'))}"
            )
        path = ask_dir / f"{name}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        exported.append(str(path.relative_to(REPO_ROOT)))
    return exported


def export_trace_metadata(con: sqlite3.Connection) -> list[str]:
    exported = []
    tr_dir = FIXTURES / "traces"
    tr_dir.mkdir(parents=True, exist_ok=True)
    for trace_id in TRACE_IDS:
        row = con.execute(
            "SELECT query, agent, model, engine, result, outcome, feedback,"
            " started_at, ended_at, total_tokens, total_latency_seconds"
            " FROM traces WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"[export] trace {trace_id} not found in db")
        (
            query,
            agent,
            model,
            engine,
            result,
            outcome,
            feedback,
            started,
            ended,
            tokens,
            latency,
        ) = row
        tool_calls = {}
        for (inp,) in con.execute(
            "SELECT input FROM trace_steps WHERE trace_id=? AND step_type='tool_call'",
            (trace_id,),
        ):
            try:
                tool = json.loads(inp).get("tool", "?")
            except json.JSONDecodeError:
                tool = "?"
            tool_calls[tool] = tool_calls.get(tool, 0) + 1
        meta = {
            "trace_id": trace_id,
            "agent": agent,
            "model": model,
            "engine": engine,
            "query_snippet": (query or "")[:120],
            "outcome": outcome,
            "feedback": feedback,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "total_tokens": tokens,
            "total_latency_seconds": latency,
            "result_snippet": (result or "")[:120],
            "tool_call_counts": tool_calls,
        }
        path = tr_dir / f"{trace_id}.json"
        path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        exported.append(str(path.relative_to(REPO_ROOT)))
    return exported


def write_readme(exported: list[str]) -> None:
    readme = FIXTURES / "README.md"
    readme.write_text(
        "# Pipeline regression fixtures (trace-replay)\n"
        "\n"
        "Real artifacts and execution traces from the on-demand research pipeline\n"
        "(`scripts/research.sh`), exported so the offline harness in this directory\n"
        "can replay production failures without a model or network (**C3**). The\n"
        "tests run the SAME bash functions from `scripts/research_lib.sh` that the\n"
        "live pipeline uses.\n"
        "\n"
        "## Origin\n"
        "\n"
        "- **hpc/** — `Subject: High Performance Computing Servers` run, 2026-08-03\n"
        "  (workspace `subject-high-performance-computing-serve`). The run completed\n"
        "  end-to-end with degradation: VERIFY looped 3x (calculator called but\n"
        "  numbers.md never written -> UNVERIFIED banner), 3b passed on the 3rd\n"
        "  attempt, provenance flagged 5/5 fabricated URLs.\n"
        "- **arm/** — `Subject: ARM Processors (CPU) use on servers` run, 2026-08-03\n"
        "  (workspace `subject-arm-proceesors-cpu-use-on-server`). Pre-fix artifact:\n"
        "  `report.md` has `## Sources & References` and `## Confidence Assessment`\n"
        "  glued to paragraph text (regression fixture for `fix_glued_headings`);\n"
        "  `report.part1` is the clean part-1 snapshot.\n"
        "- **edgeai/** — `Subject: Edge AI inference chips market` run, 2026-08-03\n"
        "  (workspace `subject-edge-ai-inference-chips-market-s`). The first run on\n"
        "  the typed Python launcher (research_phases.py): GATHER and VERIFY passed\n"
        "  first-try with the canonical-`^`-dialect prompt, and 3a exposed a new\n"
        "  failure mode — the model drifted its file_write path to a wrong slug\n"
        "  (`subject-edge-inference-chips-market-s`, dropped `ai-`) so the gate\n"
        "  failed and the run aborted honestly; on retry 3a and 3b passed. The\n"
        "  provenance check flagged 10/10 fabricated report URLs.\n"
        "- **storagesys/** — `Subject: Storage systems for AI training` run, 2026-08-04\n"
        "  (workspace `subject-storage-systems-for-ai-training-`). The first fully\n"
        "  clean end-to-end run: all four phases passed on the first attempt, the\n"
        "  report contains all six sections, provenance flagged 0/1 fabricated\n"
        "  URLs (the one `www` link traced back to its source), and no glued\n"
        "  headings (gather `fix_glued_headings` normalize active). Feedback\n"
        "  0.9 / 0.9 / 0.9 / 1.0.\n"
        "- **asklogs/** — the `jarvis agents ask` live-trace log per phase, rebuilt\n"
        "  from `trace_steps` in the CLI format (`  \u21b3 <tool> <k=v ...>`); the\n"
        "  tool-usage gate counts these lines. `verify-degenerate.txt` preserves the\n"
        "  historical broken `**` calculator expression as a regression fixture.\n"
        "- **traces/** — per-trace metadata (outcome, feedback, tokens, tool-call\n"
        "  histogram) for every phase trace of the hpc, edgeai, and storagesys\n"
        "  runs; the ground truth the asklogs and artifact fixtures derive from.\n"
        "\n"
        "## Refresh\n"
        "\n"
        "```bash\n"
        "python3 scripts/export_trace_fixtures.py   # from repo root\n"
        "```\n"
        "The exporter refuses to run when sources are missing. After a refresh,\n"
        "re-run `uv run pytest tests/pipeline/ -q` and check for secrets\n"
        "(`tests/pipeline/test_fixture_hygiene.py` guards this in CI).\n",
        encoding="utf-8",
    )
    print(f"[export] wrote {readme.relative_to(REPO_ROOT)}")
    print(f"[export] {len(exported)} fixture files")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = ap.parse_args()

    if not args.db.is_file():
        raise SystemExit(f"[export] traces db not found: {args.db}")
    con = sqlite3.connect(args.db)
    try:
        exported = []
        exported += export_artifacts(args.workspace)
        exported += export_asklogs(con)
        exported += export_trace_metadata(con)
        write_readme(exported)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
