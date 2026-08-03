"""TDL per-phase feedback scoring tests.

The score derivation is pure (feedback_score in research_lib.sh); record_feedback
only locates the trace in SQLite and writes the value. These tests pin the
deterministic score table — including the invariant that a failed phase can
never score as high as a passing one, regardless of artifact size (D7).
"""

from __future__ import annotations

import pytest

from tests.pipeline.helpers import run_lib


def score(attempts: int, size: int, passed: str = "yes") -> float:
    proc = run_lib('feedback_score "$1" "$2" "$3"', str(attempts), str(size), passed)
    assert proc.returncode == 0, proc.stderr
    return float(proc.stdout.strip())


def test_first_try_large_artifact_max_score():
    # HPC GATHER: 1 attempt, ~10KB findings -> 0.6 + 0.2 + 0.2 = 1.0
    assert score(1, 10_000) == 1.0


def test_first_try_small_artifact():
    # 0.6 base + 0.2 first-try bonus, no size bonus
    assert score(1, 100) == pytest.approx(0.8)


def test_retry_penalizes_attempts():
    assert score(2, 100) == pytest.approx(0.7)
    assert score(3, 100) == pytest.approx(0.6)


def test_size_bonus_thresholds():
    assert score(1, 1_499) == pytest.approx(0.8)
    assert score(1, 1_500) == pytest.approx(0.9)
    assert score(1, 3_999) == pytest.approx(0.9)
    assert score(1, 4_000) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("attempts", "size"),
    [(1, 10_000), (1, 100), (3, 10_000), (3, 0)],
)
def test_failed_phase_never_exceeds_passing_floor(attempts, size):
    # HPC VERIFY failure: score 0.2 (small artifact). A failed phase must
    # stay below 0.3 even with a large artifact — size alone must never mask
    # a broken workflow.
    assert score(attempts, size, "no") <= 0.3
    assert score(attempts, size, "no") < score(attempts, size, "yes")
