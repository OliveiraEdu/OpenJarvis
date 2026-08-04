#!/usr/bin/env python3
"""Discovery engine CLI — the cycle orchestrator.

Pipeline (design §4.2): collect -> filter -> store -> triage -> decide ->
trigger. M2 wired the v1 collectors (github, hn, reddit, pypi, pricing) plus
placeholders; M3 wired the filter stage (noise drop + pre_qualify tags).
Triage (M4) and the decide/trigger wiring (M4/M5) follow.

The cycle is honest (D6): per-source failures are counted, never fatal;
noise is filtered before storage; offline mode (``OJ_OFFLINE=1``) skips
network collectors; a source listed in config but not registered/enabled is
reported, not silently dropped.

Stdlib-only (host python3, no openjarvis import) — same constraint as the
research pipeline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import collectors
import config
import rules
import store as store_mod


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle(
    ctx: config.Ctx,
    cfg: config.DiscoveryConfig,
    registry: dict[str, collectors.Collector],
    *,
    once: bool = False,
    source: str | None = None,
) -> int:
    """One discovery cycle. ``once``/``source`` narrow the scope; ``once``
    semantics land with the cadence work (M5)."""
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

        stats = st.stats()
        summary = (
            f"[discovery] cycle complete: collected={collected} noise={noise}"
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
