"""M3: trigger decision — the design §4.7 table, pure and table-driven.

M7 adds the ``calibrate`` precision query — the D7 consumer for the
(score, outcome) pairs the decide stage records.
"""

from __future__ import annotations

from decide import (
    DEFER,
    SKIP,
    TRIGGER,
    calibrate,
    daily_cap_reached,
    decision,
    source_in_cooldown,
)
from store import Signal

NOW = "2026-08-04T12:00:00+00:00"


def _sig(score: int | None = 9) -> Signal:
    return Signal(source="github", source_key="k", title="t", score=score)


# -- decision table ---------------------------------------------------------


def test_below_threshold_is_skip():
    assert (
        decision(
            _sig(6),
            threshold=7,
            in_cooldown=False,
            cap_reached=False,
            engine_busy=False,
        )
        == SKIP
    )
    assert (
        decision(
            _sig(7),
            threshold=8,
            in_cooldown=False,
            cap_reached=False,
            engine_busy=False,
        )
        == SKIP
    )


def test_missing_score_is_skip():
    assert (
        decision(
            _sig(None),
            threshold=7,
            in_cooldown=False,
            cap_reached=False,
            engine_busy=False,
        )
        == SKIP
    )


def test_at_threshold_triggers_when_free():
    assert (
        decision(
            _sig(7),
            threshold=7,
            in_cooldown=False,
            cap_reached=False,
            engine_busy=False,
        )
        == TRIGGER
    )


def test_cooldown_defers():
    assert (
        decision(
            _sig(9), threshold=7, in_cooldown=True, cap_reached=False, engine_busy=False
        )
        == DEFER
    )


def test_daily_cap_defers():
    assert (
        decision(
            _sig(9), threshold=7, in_cooldown=False, cap_reached=True, engine_busy=False
        )
        == DEFER
    )


def test_engine_busy_defers():
    assert (
        decision(
            _sig(9), threshold=7, in_cooldown=False, cap_reached=False, engine_busy=True
        )
        == DEFER
    )


def test_score_check_wins_over_defers():
    """Low score is SKIP even when everything else would defer (table order)."""
    assert (
        decision(
            _sig(1), threshold=7, in_cooldown=True, cap_reached=True, engine_busy=True
        )
        == SKIP
    )


def test_decision_is_case_sensitive_constant():
    assert (SKIP, DEFER, TRIGGER) == ("SKIP", "DEFER", "TRIGGER")


# -- cooldown / cap helpers -------------------------------------------------


def test_source_in_cooldown():
    recent = "2026-08-04T06:00:00+00:00"  # 6 h before NOW
    old = "2026-08-03T12:00:00+00:00"  # 24 h before NOW
    assert source_in_cooldown(NOW, recent, 86400)
    assert not source_in_cooldown(NOW, old, 86400)
    assert source_in_cooldown(NOW, recent, 43200)  # 6h < 12h
    assert not source_in_cooldown(NOW, "", 86400)  # never triggered


def test_daily_cap_reached():
    assert daily_cap_reached(3, 3)
    assert daily_cap_reached(4, 3)
    assert not daily_cap_reached(2, 3)


# -- calibration (M7, D7 consumer) ------------------------------------------


def test_calibrate_reports_per_category_precision():
    """score >= threshold -> DONE rate over launched (DONE+FAILED) runs."""
    rows = [
        (9, "cloud", "DONE"),
        (8, "cloud", "FAILED"),
        (7, "cloud", "TRIAGED"),  # deferred, still open
        (8, "storage", "DONE"),
    ]
    summary = calibrate(rows, threshold=7)
    assert [(r["category"], r["precision"]) for r in summary] == [
        ("cloud", 0.5),
        ("storage", 1.0),
    ]
    cloud, storage = summary
    assert cloud["eligible"] == 3
    assert cloud["launched"] == 2
    assert cloud["done"] == 1
    assert cloud["failed"] == 1
    assert cloud["pending"] == 1
    assert storage["eligible"] == 1
    assert storage["launched"] == 1
    assert storage["pending"] == 0


def test_calibrate_filters_below_threshold_and_missing_scores():
    """Below-threshold and never-triaged (None) rows are ineligible."""
    rows = [
        (9, "cloud", "DONE"),
        (6, "cloud", "DONE"),  # below threshold
        (None, "cloud", "NEW"),  # never triaged
        (7, "storage", "FAILED"),
    ]
    summary = calibrate(rows, threshold=7)
    assert [(r["category"], r["precision"]) for r in summary] == [
        ("cloud", 1.0),
        ("storage", 0.0),
    ]
    assert summary[0]["eligible"] == 1
    assert summary[1]["eligible"] == 1


def test_calibrate_precision_is_none_without_evidence():
    """Eligible but nothing launched yet -> None, not a bogus 0 (D6)."""
    summary = calibrate([(9, "cloud", "TRIAGED")], threshold=7)
    assert summary == [
        {
            "category": "cloud",
            "eligible": 1,
            "launched": 0,
            "done": 0,
            "failed": 0,
            "pending": 1,
            "precision": None,
        }
    ]


def test_calibrate_counts_interrupted_triggered_as_pending():
    """A lingering TRIGGERED row (interrupted run) is pending, not launched —
    its outcome is unknown."""
    rows = [(9, "cloud", "TRIGGERED"), (9, "cloud", "DONE")]
    (cloud,) = calibrate(rows, threshold=7)
    assert cloud["launched"] == 1
    assert cloud["done"] == 1
    assert cloud["pending"] == 1
    assert cloud["precision"] == 1.0


def test_calibrate_blank_category_is_uncategorized():
    summary = calibrate([(9, "", "DONE")], threshold=7)
    assert summary[0]["category"] == "uncategorized"


def test_calibrate_empty_rows_return_empty_list():
    assert calibrate([], threshold=7) == []


def test_calibrate_sorts_by_category():
    summary = calibrate(
        [(9, "zebra", "DONE"), (9, "alpha", "DONE"), (9, "mike", "DONE")],
        threshold=7,
    )
    assert [r["category"] for r in summary] == ["alpha", "mike", "zebra"]
