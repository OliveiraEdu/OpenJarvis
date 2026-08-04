"""M3: trigger decision — the design §4.7 table, pure and table-driven."""

from __future__ import annotations

from decide import (
    DEFER,
    SKIP,
    TRIGGER,
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
