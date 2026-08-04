#!/usr/bin/env python3
"""Trigger decision — deterministic, pure (design §4.7).

``decision()`` implements the design's decision table exactly:

    score < threshold                        -> SKIP
    score >= threshold but source in cooldown -> DEFER
    cooldown OK but daily cap reached        -> DEFER
    engine busy (deep-dive run-lock held)    -> DEFER
    else                                     -> TRIGGER

The cycle (or the M4 triage stage) computes the boolean inputs from the
store and passes them in; this module stays free of I/O so the table is
unit-testable (C5/D3). Consumers (D7): the trigger wiring in M4 and the
``--calibrate`` precision query in M7.
"""

from __future__ import annotations

from datetime import datetime, timezone

from store import Signal

SKIP = "SKIP"
DEFER = "DEFER"
TRIGGER = "TRIGGER"


def _parse(dt: str) -> datetime:
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def source_in_cooldown(now_iso: str, triggered_at: str, cooldown_seconds: int) -> bool:
    """True when a source's last trigger is newer than its cooldown window.

    An empty ``triggered_at`` (never triggered) is never in cooldown.
    """
    if not triggered_at:
        return False
    try:
        elapsed = (_parse(now_iso) - _parse(triggered_at)).total_seconds()
    except ValueError:
        return False
    return elapsed < cooldown_seconds


def daily_cap_reached(triggers_today: int, max_per_day: int) -> bool:
    return triggers_today >= max_per_day


def decision(
    sig: Signal,
    *,
    threshold: int,
    in_cooldown: bool,
    cap_reached: bool,
    engine_busy: bool,
) -> str:
    """TRIGGER | DEFER | SKIP per the design §4.7 table.

    ``sig.score`` is the LLM relevance score (1-10); a missing score is
    below any threshold (SKIP). ``engine_busy`` mirrors the run-lock check
    (deep-dive engine in use) computed launcher-side.
    """
    if sig.score is None or sig.score < threshold:
        return SKIP
    if in_cooldown or cap_reached or engine_busy:
        return DEFER
    return TRIGGER


__all__ = [
    "DEFER",
    "SKIP",
    "TRIGGER",
    "daily_cap_reached",
    "decision",
    "source_in_cooldown",
]
