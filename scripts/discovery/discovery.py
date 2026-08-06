#!/usr/bin/env python3
"""Discovery engine CLI — the cycle orchestrator.

Pipeline (design §4.2): collect -> filter -> store -> triage -> decide ->
trigger. M2 wired the v1 collectors (github, hn, reddit, pypi, pricing) plus
placeholders; M3 wired the filter stage (noise drop + pre_qualify tags); M4
wired the LLM triage stage for pre-qualified signals; M5 wired the decide
stage (SKIP/DEFER/TRIGGER with the re-triage delta) and the research.sh
trigger seam (design §4.7). The cadence/scheduler wiring (M5) follows.

The cycle is honest (D6): per-source failures are counted, never fatal;
noise is filtered before storage; offline mode (``OJ_OFFLINE=1``) skips
network collectors, triage, AND triggering (no engine); a source listed in
config but not registered/enabled is reported, not silently dropped; a triage
reply that fails the JSON contract scores 0 (``parse_failed``) instead of
crashing; a trigger whose deep-dive run raises is recorded FAILED with a
reason, never silently skipped.

Stdlib-only (host python3, no openjarvis import) — same constraint as the
research pipeline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import collectors
import config
import decide
import rules
import store as store_mod
import triage
import trigger


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
    trigger_runner: Optional[trigger.TriggerRunner] = None,
) -> int:
    """One discovery cycle. ``once``/``source`` narrow the scope; ``once``
    semantics land with the cadence work (M5).

    ``triage_ask`` is the injectable engine seam (C5): tests pass a fake, so
    the cycle never touches the LLM offline. ``None`` means the real seam
    (``triage.ask_engine``). ``trigger_runner`` is the same seam for the
    deep-dive launch (default ``trigger.launch_research``); tests inject a
    fake so no research.sh is ever spawned offline. Both stages only run when
    NOT offline — the engine is a network/local-LLM call (design §4.6/§4.7).
    ``--source`` narrows *collection*; triage still covers every NEW
    pre-qualified signal, because triage is not a collector.
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
                inserted, sig_id = st.upsert(sig)
                collected += 1
                if (
                    not inserted
                    and prior is not None
                    and prior.status == "TRIAGED"
                    and sig.pre_qualify
                    and rules.re_triage_needed(sig, prior, cfg.re_triage_delta)
                ):
                    # Design §4.7: fractional metric growth past re_triage_delta
                    # re-opens a TRIAGED item; the triage + decide stages
                    # re-run it in this same cycle. Only TRIAGED reopens —
                    # DONE items stay researched (re-trigger is out of scope).
                    st.set_status(sig_id, "NEW")

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

        # decide stage (M5, design §4.7): TRIAGED rows -> TRIGGER/DEFER/SKIP.
        # The stage re-runs every cycle, so DEFER retries next cycle by
        # construction (its updated_at is bumped so the retry is observable).
        # TRIGGER runs the deep-dive through the injectable runner and records
        # TRIGGERED -> DONE (or FAILED with a reason on a raised runner, D6).
        # engine_busy is always False: research.sh has no lock file yet (the
        # §5.7 run-lock is a recorded open item), so it cannot be detected.
        triggered = 0
        trigger_failures = 0
        deferred = 0
        if not ctx.offline:
            launch = trigger_runner or trigger.launch_research
            for sig in st.list_by_status("TRIAGED"):
                if sig.score is None:
                    continue  # triage always sets a score; guard anyway (D6)
                in_cooldown = decide.source_in_cooldown(
                    now, sig.triggered_at or "", cfg.cooldown_for(sig.source)
                )
                cap_reached = decide.daily_cap_reached(
                    st.count_triggered_today(now), cfg.max_triggers_per_day
                )
                verdict = decide.decision(
                    sig,
                    threshold=cfg.threshold,
                    in_cooldown=in_cooldown,
                    cap_reached=cap_reached,
                    engine_busy=False,
                )
                if verdict == decide.TRIGGER:
                    topic = trigger.subject_topic(sig, cfg.subject_template)
                    slug = trigger.slugify(topic)
                    st.set_status(
                        sig.id or 0,
                        "TRIGGERED",
                        research_slug=slug,
                        triggered_at=now,
                    )
                    triggered += 1
                    try:
                        launch(ctx, topic)
                    except Exception as exc:
                        # Honest degrade: a failed trigger is recorded FAILED
                        # with a reason — never silently skipped (D6).
                        st.set_status(
                            sig.id or 0,
                            "FAILED",
                            triage_reason=(
                                f"trigger_failed: {type(exc).__name__}: {exc}"
                            ),
                        )
                        trigger_failures += 1
                    else:
                        st.set_status(sig.id or 0, "DONE")
                elif verdict == decide.DEFER:
                    st.set_status(sig.id or 0, "TRIAGED")
                    deferred += 1
                # SKIP: stays TRIAGED with its score; re-decided cheaply next
                # cycle (score < threshold is final until re-triage re-scores).

        stats = st.stats()
        summary = (
            f"[discovery] cycle complete: collected={collected} noise={noise}"
            f" triaged={triaged} parse_failed={parse_failed}"
            f" triggered={triggered} trigger_failures={trigger_failures}"
            f" deferred={deferred} failed={failed} total={stats['total']}"
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


def cmd_calibrate(ctx: config.Ctx, cfg: config.DiscoveryConfig) -> int:
    """Per-category decision precision — the D7 calibration consumer
    (design §4.7): ``score >= threshold`` -> DONE rate, so thresholds are
    tuned from data, not anecdote (C6). Precision is DONE over launched
    (DONE + FAILED) — pure triage quality, independent of cooldown/cap."""
    with store_mod.SignalStore(ctx.signals_db) as st:
        rows = st.decision_rows()
    summary = decide.calibrate(rows, cfg.threshold)
    if not summary:
        print("[calibrate] no signals with score >= threshold yet")
        return 0
    for row in summary:
        precision = (
            f"{row['precision']:.0%}" if row["precision"] is not None else "no evidence"
        )
        print(
            f"[calibrate] {row['category']:<14} eligible={row['eligible']:>3}"
            f" launched={row['launched']:>3} done={row['done']:>3}"
            f" failed={row['failed']:>3} pending={row['pending']:>3}"
            f" precision={precision}"
        )
    return 0


def _num(v: object) -> str:
    """Metrics are JSON: format ints with thousands separators, show '-' for
    absent values, and never crash on an unexpected type."""
    if isinstance(v, int):
        return f"{v:,}"
    return "-" if v is None else str(v)


def cmd_hf(ctx: config.Ctx, top: int, show_all: bool) -> int:
    """List HuggingFace discovery signals — the D7 reader for the ``hf``
    collector (design §4.3): metrics JSON decoded, sorted by downloads (the
    primary metric, §4.4) so the biggest movers read first."""
    with store_mod.SignalStore(ctx.signals_db) as st:
        sigs = st.list_by_source("hf")
    if not sigs:
        print("[hf] no hf signals yet")
        return 0
    sigs.sort(key=lambda s: s.metrics.get("downloads", 0) or 0, reverse=True)
    limit = len(sigs) if show_all else max(1, min(top, len(sigs)))
    print(f"[hf] total={len(sigs)} shown={limit} (sorted by downloads desc)")
    for s in sigs[:limit]:
        m = s.metrics
        extra = ""
        if s.pre_qualify:
            extra += f" pq={s.pre_qualify}"
        if s.category:
            extra += f" cat={s.category}"
        if s.score is not None:
            extra += f" score={s.score}"
        print(
            f"[hf] {s.id:>4} {s.title} status={s.status}"
            f" dl={_num(m.get('downloads'))}"
            f" likes={_num(m.get('likes'))}"
            f" trend={_num(m.get('trending_score'))}"
            f" pipeline={_num(m.get('pipeline_tag'))}"
            f"{extra}"
        )
    return 0


def _fmt_metric(v: object, maxlen: int = 48) -> str:
    """One metrics key=value term; long values (summaries, content hashes)
    are truncated so lines stay readable."""
    s = _num(v)
    return s if len(s) <= maxlen else s[: maxlen - 1] + "\u2026"


def cmd_signals(
    ctx: config.Ctx,
    registry: dict[str, Any],
    source: Optional[str],
    top: int,
    show_all: bool,
) -> int:
    """Generic D7 reader: list any collector's signals with metrics decoded,
    sorted by the source's primary metric (rules.PRIMARY_METRIC, §4.4) or id
    order for metric-less sources (pricing's binary content_hash is not a
    sortable delta). ``--source`` narrows to one source; without it, every
    source present in the DB gets a block."""
    if source is not None and source not in registry:
        print(f"[signals] collector '{source}' is not registered")
        return 2
    with store_mod.SignalStore(ctx.signals_db) as st:
        all_sigs = st.list_all()
    if source is not None:
        groups: list[tuple[str, list[store_mod.Signal]]] = [
            (source, [s for s in all_sigs if s.source == source])
        ]
    else:
        by_source: dict[str, list[store_mod.Signal]] = {}
        for s in all_sigs:
            by_source.setdefault(s.source, []).append(s)
        groups = sorted(by_source.items())
    if not groups or all(not sigs for _, sigs in groups):
        if source is not None:
            print(f"[signals] no {source} signals yet")
        else:
            print("[signals] no signals yet")
        return 0
    for src, sigs in groups:
        if not sigs:
            continue
        order = "id order"
        primary = rules.PRIMARY_METRIC.get(src) or ""
        if primary and any(isinstance(s.metrics.get(primary), int) for s in sigs):
            metric_key = primary  # plain str: lambdas don't see narrowing
            sigs = sorted(
                sigs,
                key=lambda s: s.metrics.get(metric_key, 0) or 0,
                reverse=True,
            )
            order = f"sorted by {primary} desc"
        limit = len(sigs) if show_all else max(1, min(top, len(sigs)))
        print(f"[signals] source={src} total={len(sigs)} shown={limit} ({order})")
        for s in sigs[:limit]:
            m = s.metrics
            extra = ""
            if s.pre_qualify:
                extra += f" pq={s.pre_qualify}"
            if s.category:
                extra += f" cat={s.category}"
            if s.score is not None:
                extra += f" score={s.score}"
            metrics = " ".join(f"{k}={_fmt_metric(v)}" for k, v in m.items())
            row = f"[signals] {s.id:>4} {s.title} status={s.status}"
            if metrics:
                row += f" {metrics}"
            print(f"{row}{extra}")
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

    sub.add_parser(
        "calibrate", help="per-category decision precision (D7 consumer, §4.7)"
    )

    hf = sub.add_parser("hf", help="list HuggingFace discovery signals (§4.3)")
    hf.add_argument("--top", type=int, default=10, help="max rows to show (default 10)")
    hf.add_argument(
        "--all", action="store_true", help="show every hf signal, not just --top"
    )

    sig = sub.add_parser(
        "signals", help="list signals from any source, metrics decoded (§4.3)"
    )
    sig.add_argument(
        "--source", default=None, help="source name (default: all sources)"
    )
    sig.add_argument(
        "--top", type=int, default=10, help="max rows per source (default 10)"
    )
    sig.add_argument(
        "--all", action="store_true", help="show every row, not just --top"
    )

    args = ap.parse_args(argv)
    cfg = config.load_config()
    ctx = config.Ctx.from_env()
    registry = collectors.build_registry(cfg)

    if args.cmd == "run":
        return run_cycle(ctx, cfg, registry, once=args.once, source=args.source)
    if args.cmd == "stats":
        return cmd_stats(ctx)
    if args.cmd == "calibrate":
        return cmd_calibrate(ctx, cfg)
    if args.cmd == "hf":
        return cmd_hf(ctx, top=args.top, show_all=args.all)
    if args.cmd == "signals":
        return cmd_signals(
            ctx,
            registry,
            source=args.source,
            top=args.top,
            show_all=args.all,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
