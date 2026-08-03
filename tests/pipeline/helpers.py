"""Shared fixtures/helpers for the pipeline regression harness.

The harness exercises the SAME deterministic bash functions that
scripts/research.sh runs in production — they live in scripts/research_lib.sh
(single source of truth, no global state) and are invoked here via ``bash -c``
with explicit positional arguments. This is deliberate (C4 — test the seams,
not a Python reimplementation): a failing check here is a failing check in the
live pipeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
LIB_SH = SCRIPTS / "research_lib.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

HPC = FIXTURES / "artifacts" / "hpc"
ARM = FIXTURES / "artifacts" / "arm"
ASKSLOGS = FIXTURES / "asklogs"


def run_lib(fn_call: str, *args: str) -> subprocess.CompletedProcess:
    """Run one research_lib.sh function via ``bash -c``.

    ``fn_call`` is a bash expression that may use ``$1``..``$n`` bound to
    ``args``, e.g. ``run_lib('check_report_sections "$1"', str(path))``. The
    script runs under ``set -euo pipefail`` like the live pipeline, so a
    validator that returns 1 yields returncode 1.
    """
    script = f"set -euo pipefail\nsource {LIB_SH}\n{fn_call}"
    return subprocess.run(
        ["bash", "-c", script, "pipeline-test", *args],
        capture_output=True,
        text=True,
    )


def count_tool_calls(asklog: Path, tool: str) -> int:
    """Run the production tool-gate counter over a fixture asklog."""
    proc = run_lib('count_tool_calls "$1" "$2"', str(asklog), tool)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())
