"""Tool-usage gate tests (the phase gates enforce the workflow mechanically).

Each fixture asklog is the REAL live-trace log of a phase (reconstructed
from trace_steps in the CLI format), so these tests pin what the production
gate sees — including the exact failure modes that burned retries.
"""

from __future__ import annotations

from tests.pipeline.helpers import ASKSLOGS, count_tool_calls

# Phase gate minimums as wired in scripts/research.sh run_phase calls.
GATHER_WEB_SEARCH_MIN = 2
VERIFY_CALCULATOR_MIN = 1
REPORT_FILE_WRITE_MIN = 2


def test_gather_asklog_meets_web_search_gate():
    assert (
        count_tool_calls(ASKSLOGS / "gather-ok.txt", "web_search")
        >= GATHER_WEB_SEARCH_MIN
    )


def test_verify_gate_passes_but_file_write_missing():
    """The degenerate VERIFY run: 2 calculator calls (tool gate satisfied) but
    the numbers.md file_write never happened — the phase failed on the
    artifact check, not the tool gate. This documents the real failure mode."""
    log = ASKSLOGS / "verify-degenerate.txt"
    assert count_tool_calls(log, "calculator") >= VERIFY_CALCULATOR_MIN
    assert count_tool_calls(log, "file_write") == 0


def test_part2_gate_rejects_single_write_attempt():
    """3b attempt 1: a single file_write (all sections in one call) fails the
    file_write:2 gate — the exact shortcut that cost two retries."""
    log = ASKSLOGS / "part2-one-write.txt"
    assert count_tool_calls(log, "file_write") == 1
    assert count_tool_calls(log, "file_write") < REPORT_FILE_WRITE_MIN


def test_part2_gate_passes_three_write_attempt():
    log = ASKSLOGS / "part2-three-writes.txt"
    assert count_tool_calls(log, "file_write") >= REPORT_FILE_WRITE_MIN


def test_part1_asklog_meets_write_gate():
    log = ASKSLOGS / "part1-three-writes.txt"
    assert count_tool_calls(log, "file_write") >= REPORT_FILE_WRITE_MIN
