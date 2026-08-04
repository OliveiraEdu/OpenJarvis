#!/usr/bin/env python3
"""Discovery engine CLI — the cycle orchestrator (M1 scaffold).

Pipeline (design §4.2): collect -> filter -> store -> triage -> decide ->
trigger. M1 ships the typed config, the signals.db store, and the cycle
scaffold; collectors, rules, triage, and decide land in M2-M5.

The cycle is honest when nothing is wired yet (D6): with an empty collector
registry it reports the no-op and the store counts — it never pretends to
have collected or triaged.

Stdlib-only (host python3, no openjarvis import) — same constraint as the
research pipeline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import config
import store as store_mod
from collectors import REGISTRY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle(
    ctx: config.Ctx,
    cfg: config.DiscoveryConfig,
    *,
    once: bool = False,
    source: str | None = None,
) -> int:
    """One discovery cycle. ``once``/``source`` narrow the scope; M1 treats
    both as the same pass (scheduling semantics land with the cadence work)."""
    del once  # scheduling semantics arrive in M5; single-pass for now
    if source is not None and source not in REGISTRY:
        print(f"[discovery] collector '{source}' is not registered")
        return 2

    with store_mod.SignalStore(ctx.signals_db) as st:
        enabled = [name for name in cfg.enabled_collectors if name in REGISTRY]
        if source is not None:
            enabled = [source]

        collected = 0
        if not enabled:
            print(
                "[discovery] no collectors registered yet (M2 wires them);"
                " cycle is a no-op"
            )
        for name in enabled:
            collector = REGISTRY[name]
            signals = collector.fetch(_now())
            for sig in signals:
                inserted, _id = st.upsert(sig)
                collected += 1

        stats = st.stats()
        summary = (
            f"[discovery] cycle complete: collected={collected}"
            f" total={stats['total']}"
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
        "--once", action="store_true", help="single pass (M1: same as cycle)"
    )
    run.add_argument("--source", default=None, help="restrict to one collector name")

    sub.add_parser("stats", help="print signals.db counts by status")

    args = ap.parse_args(argv)
    cfg = config.load_config()
    ctx = config.Ctx.from_env()

    if args.cmd == "run":
        return run_cycle(ctx, cfg, once=args.once, source=args.source)
    if args.cmd == "stats":
        return cmd_stats(ctx)
    return 2


if __name__ == "__main__":
    sys.exit(main())
