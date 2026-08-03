"""Fixture integrity + hygiene (C3/C7).

Guards the committed fixtures: they must stay self-consistent with the trace
metadata they derive from, and must never carry secrets (Tavily/OpenAI/GitHub
keys — the deployment keeps the real Tavily key in gitignored
deploy/docker/.env; fixtures are committed, so they can only contain shapes
that look nothing like a key).
"""

from __future__ import annotations

import json
import re

import pytest

from tests.pipeline.helpers import ASKSLOGS, FIXTURES, HPC

SECRET_PATTERNS = [
    re.compile(r"tvly-[A-Za-z0-9_-]{10,}"),  # Tavily
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI/Anthropic style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"\bBearer [A-Za-z0-9._-]{20,}"),  # bearer tokens
]


def test_asklog_matches_trace_metadata_tool_counts():
    """The reconstructed asklog and the trace metadata must agree (the
    asklogs/ are derived from trace_steps; a drift means the reconstruction
    or the metadata export broke)."""
    meta = json.loads(
        (FIXTURES / "traces" / "bb53823f3cee418d.json").read_text(encoding="utf-8")
    )
    log = (ASKSLOGS / "part2-three-writes.txt").read_text(encoding="utf-8")
    assert log.count("\u21b3 file_write") == meta["tool_call_counts"]["file_write"] == 3
    assert log.count("\u21b3 file_read") == meta["tool_call_counts"]["file_read"] == 1


def test_verify_degenerate_asklog_frozen_with_broken_asterisk():
    """The historical failing expression must stay in the fixture as the
    regression record (C3)."""
    log = (ASKSLOGS / "verify-degenerate.txt").read_text(encoding="utf-8")
    assert "**" in log
    assert "calculator expression=" in log


def test_trace_metadata_covers_the_full_run():
    """All nine traces of the HPC-2026-08-03 run, so the failure sequence
    (gather -> 3x degenerate verify -> 3a -> 3x 3b) stays replayable."""
    traces = sorted(p.name for p in (FIXTURES / "traces").glob("*.json"))
    assert traces == sorted(
        f"{tid}.json"
        for tid in [
            "05870f2b0f004bf8",
            "1cf051ae71994ffb",
            "65598dcad07343f8",
            "7a28b8aae48a4346",
            "bb53823f3cee418d",
            "d528115ea38a4fd1",
            "d60f6d29d7ef4552",
            "dec136a37df44c75",
            "f2505e725d79400a",
        ]
    )


def test_artifact_fixtures_are_non_trivial():
    """The artifacts must stay real-sized — a truncated/emptied fixture
    would silently weaken the validator tests."""
    for path in (FIXTURES / "artifacts").rglob("*.md"):
        assert len(path.read_text(encoding="utf-8")) > 100, path


def test_hpc_report_preserves_its_flags():
    """The completed-degraded run's report keeps the honest flags (D6)."""
    report = (HPC / "report.md").read_text(encoding="utf-8")
    assert report.startswith("> **UNVERIFIED**")
    assert "PROVENANCE NOTE" in report


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.pattern)
def test_fixtures_contain_no_secret_shaped_tokens(pattern):
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = pattern.search(text)
        assert match is None, (
            f"possible secret in {path.relative_to(FIXTURES)}: {match.group(0)}"
        )
