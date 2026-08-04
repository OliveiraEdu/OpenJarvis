#!/usr/bin/env python3
"""Collector contract + registry (design §4.3).

M1 ships the typed contract and an empty registry; M2 wires the concrete
collectors (github, hn, reddit, pypi, pricing) and registers the complex-
source placeholders (SEC EDGAR, Reddit OAuth, job boards, cloud
marketplaces) behind the same contract.

A collector produces ``Signal`` candidates with a stable ``source_key`` per
item; dedupe lives in ``SignalStore.upsert``. Collectors must be idempotent
per fetch (re-polls overwrite, never duplicate) and must not touch the LLM —
triage is a later, separate stage.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from store import Signal


@runtime_checkable
class Collector(Protocol):
    """One market source. Implementations are plain stdlib (urllib with an
    injectable opener for offline tests)."""

    name: str

    def fetch(self, now: str) -> list[Signal]:
        """Return this cycle's candidates. ``now`` is an ISO timestamp so
        collectors stay pure w.r.t. time (C5)."""
        ...


# name -> Collector instance; the cycle iterates only enabled ones
# (config.toml [collectors] enabled). Empty until M2.
REGISTRY: dict[str, Collector] = {}


__all__ = ["Collector", "REGISTRY"]
