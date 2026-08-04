#!/usr/bin/env python3
"""Discovery engine CLI — the cycle orchestrator.

Pipeline (design §4.2): collect -> filter -> store -> triage -> decide ->
trigger. M2 wired the v1 collectors (github, hn, reddit, pypi, pricing) plus
placeholders; M3 wired the filter stage (noise drop + pre_qualify tags); M4
wired the LLM triage stage for pre-qualified signals. The decide/trigger
wiring (M4/M5) follows.

The cycle is honest (D6): per-source failures are counted, never fatal;
noise is filtered before storage; offline mode (``OJ_OFFLINE=1``) skips
network collectors AND triage (no engine); a source listed in config but not
registered/enabled is reported, not silently dropped; a triage reply that
fails the JSON contract scores 0 (``parse_failed``) instead of crashing.

Stdlib-only (host python3, no openjarvis import) — same constraint as the
research pipeline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Callable, Optional

import collectors
import config
import rules
import store as store_mod
import triage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle(
    ctx: config.Ctx,
    cfg: config.DiscoveryConfig,
    registry: dict[str, collectors.Collector],
    *,
    once: bool = False,
    source: str | None = None,
    triage_ask: Optional[Callable[[config.Ctx, str], str]] = None,
) -> int:
    """One discovery cycle. ``once``/``source`` narrow the scope; ``once``
    semantics land with the cadence work (M5).

    ``triage_ask`` is the injectable engine seam (C5): tests pass a fake, so
    the cycle never touches the LLM offline. ``None`` means the real seam
    (``triage.ask_engine``). Triage only runs when NOT offline — the engine
    is a network/local-LLM call (design §4.6). ``--source`` narrows
    *collection*; triage still covers every NEW pre-qualified signal, because
    triage is not a collector.
    """
    del once  # scheduling semantics arrive in M5; single-pass for now
    if source is not None and source not in registry:
        print(f"[discovery] collector '{source}' is not registered")
        return 2
    if source is not None and not registry[source].enabled:
        print(f"[discovery] collector '{source}' is registered but not enabled")
        return 2

    with store_mod.SignalStore(ctx.signals_db) as st:
        if source is not None:
            enabled: list[str] = [source]
        elif ctx.offline:
            print(
                "[discovery] offline mode (OJ_OFFLINE=1): skipping network collectors"
            )
            enabled = []
        else:
            enabled = []
            for name in cfg.enabled_collectors:
                if name not in registry:
                    print(
                        f"[discovery] collector '{name}' (config) is not"
                        " registered; skipped"
                    )
                elif not registry[name].enabled:
                    print(
                        f"[discovery] collector '{name}' (config) is not"
                        " enabled; skipped"
                    )
                else:
                    enabled.append(name)

        if not enabled:
            print("[discovery] no enabled collectors in this cycle")

        now = _now()  # one cycle timestamp: collectors and rules share it (C5)
        collected = 0
        noise = 0
        failed = 0
        for name in enabled:
            collector = registry[name]
            try:
                signals = collector.fetch(now)
            except Exception as exc:  # per-source failures are counted, not fatal
                print(
                    f"[discovery] collector '{name}' failed:"
                    f" {type(exc).__name__}: {exc}"
                )
                failed += 1
                continue
            for sig in signals:
                if rules.noise_filters(sig):
                    noise += 1
                    continue
                # The stored row is the previous cycle's snapshot until the
                # upsert refreshes it — that is the delta baseline (rules §4.4).
                prior = st.get(sig.source, sig.source_key)
                sig.pre_qualify = ",".join(rules.pre_qualify(sig, prior, now=now))
                st.upsert(sig)
                collected += 1

        # triage stage (M4, design §4.6): pre-qualified NEW signals reach the
        # LLM. Only tagged signals (rules §4.4) are scored; a reply that fails
        # the JSON contract scores 0 with triage_reason="parse_failed" (D6)
        # instead of crashing. Offline mode skips the engine entirely.
        triaged = 0
        parse_failed = 0
        if not ctx.offline:
            ask = triage_ask or triage.ask_engine
            for sig in st.list_by_status("NEW"):
                if not sig.pre_qualify:
                    continue
                verdict = triage.triage_signal(ctx, sig, ask=ask)
                st.set_status(
                    sig.id or 0,
                    "TRIAGED",
                    score=verdict.score,
                    category=verdict.category,
                    triage_reason=verdict.reason,
                )
                triaged += 1
                if verdict.reason == triage.PARSE_FAILED:
                    parse_failed += 1

        stats = st.stats()
        summary = (
            f"[discovery] cycle complete: collected={collected} noise={noise}"
            f" triaged={triaged} parse_failed={parse_failed}"
            f" failed={failed} total={stats['total']}"
            f" NEW={stats['NEW']} TRIAGED={stats['TRIAGED']}"
            f" TRIGGERED={stats['TRIGGERED']} DONE={stats['DONE']}"
            f" FAILED={stats['FAILED']}"
        )
        print(summary)
    return 0


def cmd_stats(ctx: config.Ctx) -> int:
    with store_mod.SignalStore(ctx.signals_db) as st:
        stats = st.stats()
    print(
        f"signals.db: total={stats['total']} NEW={stats['NEW']}"
        f" TRIAGED={stats['TRIAGED']} TRIGGERED={stats['TRIGGERED']}"
        f" DONE={stats['DONE']} FAILED={stats['FAILED']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="discovery",
        description="Trend Seeker discovery engine (design §4).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a discovery cycle")
    run.add_argument("--cycle", action="store_true", help="full cycle (default)")
    run.add_argument(
        "--once", action="store_true", help="single pass (same as cycle until M5)"
    )
    run.add_argument("--source", default=None, help="restrict to one collector name")

    sub.add_parser("stats", help="print signals.db counts by status")

    args = ap.parse_args(argv)
    cfg = config.load_config()
    ctx = config.Ctx.from_env()
    registry = collectors.build_registry(cfg)

    if args.cmd == "run":
        return run_cycle(ctx, cfg, registry, once=args.once, source=args.source)
    if args.cmd == "stats":
        return cmd_stats(ctx)
    return 2


if __name__ == "__main__":
    sys.exit(main())
