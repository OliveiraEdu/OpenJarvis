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


def test_edgeai_slug_drift_asklog_records_mixed_path_writes():
    """The part1 failure mode of the first typed-launcher run (edgeai,
    2026-08-03): the model wrote the first chunk to the correct slug path
    then drifted to a wrong slug (dropped `ai-`) for the remaining appends.
    The gate caught it — the artifact at the right path stayed too small —
    so the phase retried. The fixture must keep the drifted writes."""
    log = (ASKSLOGS / "edgeai-part1-slug-drift.txt").read_text(encoding="utf-8")
    writes = [l for l in log.splitlines() if "\u21b3 file_write" in l]
    assert len(writes) >= 2
    paths = {w.split("path=", 1)[1].split(" ", 1)[0] for w in writes}
    assert len(paths) > 1  # drifted to a different path mid-run


def test_trace_metadata_covers_the_full_run():
    """All committed trace metadata: the nine traces of the HPC-2026-08-03
    run (gather -> 3x degenerate verify -> 3a -> 3x 3b) plus the seven traces
    of the edgeai-2026-08-03 run on the typed Python launcher (gather ->
    verify -> 3x 3a with slug-drift -> retry -> 3b)."""
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
            # edgeai run
            "4a0a5dad9f3d4ff9",
            "6a1773a2578d44a4",
            "6b35140208ab403b",
            "9fe872cd42b34067",
            "aff4155eecab4d4f",
            "b2165aac5c2f4e3b",
            "d7b88a51de054c99",
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
