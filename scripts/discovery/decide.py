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

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

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


def calibrate(
    rows: Iterable[tuple[Optional[int], str, str]],
    threshold: int,
) -> list[dict[str, Any]]:
    """Per-category decision precision — the D7 consumer (design §4.7).

    ``rows`` are ``(score, category, status)`` triples read from signals.db
    (``store.SignalStore.decision_rows``). Only signals with
    ``score >= threshold`` were ever trigger-eligible; among them precision is
    the DONE rate over *launched* runs (DONE + FAILED) — the pure measure of
    triage quality, independent of cooldown/cap timing. Still-open rows
    (TRIAGED after a DEFER, or TRIGGERED from an interrupted run) are reported
    as ``pending`` so the funnel's coverage is visible; a category with no
    launched run reports precision ``None`` (no evidence yet) instead of a
    bogus 0. Rows sort by category; a blank category counts as
    ``uncategorized``. Pure and unit-testable (C5/D3) — the CLI prints the
    result.
    """
    per: dict[str, dict[str, int]] = defaultdict(
        lambda: {"done": 0, "failed": 0, "pending": 0}
    )
    for score, category, status in rows:
        if score is None or score < threshold:
            continue
        bucket = per[category or "uncategorized"]
        if status == "DONE":
            bucket["done"] += 1
        elif status == "FAILED":
            bucket["failed"] += 1
        else:  # TRIAGED (deferred) or TRIGGERED (interrupted run)
            bucket["pending"] += 1

    summary: list[dict[str, Any]] = []
    for category in sorted(per):
        bucket = per[category]
        launched = bucket["done"] + bucket["failed"]
        summary.append(
            {
                "category": category,
                "eligible": launched + bucket["pending"],
                "launched": launched,
                "done": bucket["done"],
                "failed": bucket["failed"],
                "pending": bucket["pending"],
                "precision": (bucket["done"] / launched) if launched else None,
            }
        )
    return summary


__all__ = [
    "DEFER",
    "SKIP",
    "TRIGGER",
    "calibrate",
    "daily_cap_reached",
    "decision",
    "source_in_cooldown",
]
