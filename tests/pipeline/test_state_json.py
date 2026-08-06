"""Layer 2 — state.json schema + cross-fixture consistency (design §5.1, §5.3).

The committed state.json fixture is the clean storagesys run's machine summary
(exported by scripts/export_trace_fixtures.py from the SAME ground-truth
sources production uses: artifact bytes, count_tool_calls over the asklog,
and the feedback the run recorded on its traces). A drift between state.json
and its sources is a production regression.

For part1, report.md was later merged by part2, so its phase-time bytes (and
thus feedback_score) are not derivable from the fixtures — the recorded trace
feedback is the ground truth. The derivable phases are gather/verify/part2.
"""

from __future__ import annotations

import json

import pytest

from tests.pipeline.helpers import (
    ASKSLOGS,
    FIXTURES,
    STORAGESYS,
    count_tool_calls,
    run_lib,
)

STATE = FIXTURES / "state" / "storagesys.json"

# phase -> (asklog fixture, trace metadata fixture, artifact fixture)
PHASE_SOURCES = {
    "gather": ("storagesys-gather", "08a649271cb142de", "findings.md"),
    "verify": ("storagesys-verify", "6ca6bd98241a404d", "numbers.md"),
    "part1": ("storagesys-part1", "5c3a606a5bb041e3", "report.md"),
    "part2": ("storagesys-part2", "370cd3c65c74462a", "report.md"),
}

# report.md is shared by part1 and part2 and merged by part2, so only the
# phases whose artifact no later phase touches can re-derive feedback_score.
DERIVABLE_FEEDBACK = {"gather", "verify", "part2"}

ENTRY_KEYS = {
    "phase",
    "attempts",
    "status",
    "artifact",
    "artifact_bytes",
    "tool_counts",
    "feedback",
    "gate",
}


def state_data() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


def trace_meta(trace_id: str) -> dict:
    return json.loads(
        (FIXTURES / "traces" / f"{trace_id}.json").read_text(encoding="utf-8")
    )


def test_state_json_schema_shape():
    data = state_data()
    assert set(data) == {"schema", "run_id", "topic", "phases"}
    assert data["schema"] == 1
    assert data["run_id"] == "subject-storage-systems-for-ai-training-"
    assert "Subject: Storage systems for AI training pipelines" in data["topic"]
    # one entry per phase, in run order
    assert [p["phase"] for p in data["phases"]] == [
        "gather",
        "verify",
        "part1",
        "part2",
    ]
    for entry in data["phases"]:
        assert set(entry) == ENTRY_KEYS
        assert entry["attempts"] == 1
        assert entry["status"] == "OK"  # clean run: all phases first-try
        assert entry["gate"] == "pass"
        assert entry["artifact_bytes"] > 0
        assert 0.0 <= entry["feedback"] <= 1.0


def test_state_json_artifact_bytes_match_artifact_fixtures():
    for entry in state_data()["phases"]:
        assert (
            entry["artifact_bytes"] == (STORAGESYS / entry["artifact"]).stat().st_size
        )


def test_state_json_tool_counts_match_asklogs():
    """tool_counts must equal count_tool_calls over the asklog fixture — both
    directions, so no tool is missed and none is fabricated."""
    for entry in state_data()["phases"]:
        asklog = ASKSLOGS / f"{PHASE_SOURCES[entry['phase']][0]}.txt"
        recorded = entry["tool_counts"]
        assert isinstance(recorded, dict) and recorded
        for tool, n in recorded.items():
            assert count_tool_calls(asklog, tool) == n
        used = {
            parts[1]
            for line in asklog.read_text(encoding="utf-8").splitlines()
            if len(parts := line.split()) >= 2 and parts[0] == "\u21b3"
        }
        assert set(recorded) == used


def test_state_json_tool_counts_match_trace_metadata():
    for entry in state_data()["phases"]:
        meta = trace_meta(PHASE_SOURCES[entry["phase"]][1])
        for tool, n in entry["tool_counts"].items():
            assert meta["tool_call_counts"].get(tool) == n


def test_state_json_feedback_matches_recorded_and_score():
    for entry in state_data()["phases"]:
        # the machine summary records the feedback the run actually wrote
        meta = trace_meta(PHASE_SOURCES[entry["phase"]][1])
        assert entry["feedback"] == pytest.approx(meta["feedback"])
        # and, where the fixture artifact is phase-final, that value equals
        # the production score function over the recorded bytes
        if entry["phase"] in DERIVABLE_FEEDBACK:
            proc = run_lib(
                'feedback_score "$1" "$2" "$3"',
                str(entry["attempts"]),
                str(entry["artifact_bytes"]),
                "yes",
            )
            assert proc.returncode == 0, proc.stderr
            assert entry["feedback"] == pytest.approx(float(proc.stdout.strip()))
