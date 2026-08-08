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
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# -- timeline (D7 consumer, §4.7) ---------------------------------------------

# Every timestamp renders in host-local time (UTC-3 on the operator box), the
# same frame the reports use — DB timestamps are UTC ISO, traces are epoch
# floats, scheduler runs are ISO with offset; the display normalizes all three.
LOCAL_TZ = timezone(timedelta(hours=-3))

_TOPIC_PRIMARY = re.compile(r"Topic: (.+?)\. Work this way")
_TOPIC_FALLBACK = re.compile(r"Topic: (.+?)(?:\. |\n|$)")
_OFFSET_FIX = re.compile(r"([+-]\d{2})(\d{2})$")

_ARTIFACTS = (
    "findings.md",
    "numbers.md",
    "numbers.md.tmp",
    "report.md",
    "report.part1",
    "state.json",
)
_SKIP_DIRS = frozenset({"_research_logs", "research-corpus"})


def _parse_iso(text: str) -> Optional[datetime]:
    """Parse the DB/scheduler ISO timestamps (defensive: older Pythons reject
    a colon-less ``-0300`` offset, so normalize it before fromisoformat)."""
    t = (text or "").strip()
    if not t:
        return None
    m = _OFFSET_FIX.search(t)
    if m and ":" not in m.group(0):
        t = t[: m.start()] + m.group(1) + ":" + m.group(2)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # DB writes are UTC; assume so
    return dt


def _iso_local(text: str) -> str:
    dt = _parse_iso(text)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if dt else "-"


def _epoch_local(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def _clock_local(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%H:%M:%S")


def _trace_phase(query: str) -> str:
    """Classify a deep-dive trace by its prompt prefix (research_phases.py
    phase prompts; unknown prompts stay unattributed, e.g. continuation
    echoes)."""
    q = (query or "").upper()
    if q.startswith("GATHER FACTS"):
        return "gather"
    if q.startswith("VERIFY THE NUMBERS"):
        return "numbers"
    if q.startswith("WRITE PART 2"):
        return "write-2"
    if q.startswith("WRITE PART 1"):
        return "write-1"
    if q.startswith("WRITE"):
        return "write"
    return ""


def _trace_topic(query: str) -> str:
    """Extract the research.sh topic from a trace prompt — the same parse the
    fixture exporter uses (export_trace_fixtures.py)."""
    m = _TOPIC_PRIMARY.search(query or "")
    if m:
        return m.group(1).strip()
    m = _TOPIC_FALLBACK.search(query or "")
    return m.group(1).strip() if m else ""


def _traces_by_slug(
    traces_db: Path,
) -> dict[str, list[tuple[str, float, float, float]]]:
    """traces.db -> {research_slug: [(phase, started_at, ended_at, feedback)]}.

    The topic in each trace prompt slugifies to the workspace dir name — the
    C4 contract — so a run's phase windows come straight from its traces. A
    missing/locked DB degrades to no phase info, never an abort (D6)."""
    if not traces_db.is_file():
        return {}
    by_slug: dict[str, list[tuple[str, float, float, float]]] = {}
    try:
        con = sqlite3.connect(str(traces_db))
        rows = con.execute(
            "SELECT query, started_at, ended_at, feedback FROM traces"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    for query, started, ended, feedback in rows:
        phase = _trace_phase(query)
        topic = _trace_topic(query)
        if not phase or not topic:
            continue
        by_slug.setdefault(trigger.slugify(topic), []).append(
            (
                phase,
                float(started),
                float(ended),
                float(feedback) if feedback is not None else -1.0,
            )
        )
    return by_slug


def _scan_runs(workspace: Path) -> list[tuple[str, list[tuple[str, float, int]]]]:
    """Workspace -> [(dir_name, [(artifact, mtime, size), ...])]: every run dir
    that actually produced artifacts, infra dirs and dotdirs excluded."""
    runs: list[tuple[str, list[tuple[str, float, int]]]] = []
    if not workspace.is_dir():
        return runs
    for entry in sorted(workspace.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in _SKIP_DIRS:
            continue
        artifacts = [
            (f.name, f.stat().st_mtime, f.stat().st_size)
            for f in sorted(entry.iterdir())
            if f.is_file() and f.name in _ARTIFACTS
        ]
        if artifacts:
            runs.append((entry.name, artifacts))
    return runs


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def _artifact_text(artifacts: list[tuple[str, float, int]]) -> str:
    return " · ".join(
        f"{name} {_fmt_size(size)} {_epoch_local(mtime)}"
        for name, mtime, size in sorted(artifacts)
    )


def _signal_section(
    name: str,
    sig: store_mod.Signal,
    artifacts: list[tuple[str, float, int]],
    phases: list[tuple[str, float, float, float]],
) -> list[str]:
    lines = [f"[timeline] == {name}  kind=signal  status={sig.status}"]
    extras = []
    if sig.source:
        extras.append(f"source={sig.source}")
    if sig.title:
        extras.append(f"title={sig.title}")
    if sig.score is not None:
        extras.append(f"score={sig.score}")
    if sig.category:
        extras.append(f"cat={sig.category}")
    if sig.pre_qualify:
        extras.append(f"pq={sig.pre_qualify}")
    if extras:
        lines.append(f"[timeline]    {' '.join(extras)}")
    lines.append(
        f"[timeline]    first_seen={_iso_local(sig.created_at)}"
        f" triggered={_iso_local(sig.triggered_at)}"
    )
    if phases:
        parts = []
        for phase, started, ended, feedback in sorted(phases, key=lambda p: p[1]):
            fb = f" fb={feedback:g}" if feedback >= 0.0 else ""
            parts.append(f"{phase} {_clock_local(started)}->{_clock_local(ended)}{fb}")
        lines.append(f"[timeline]    phases: {' · '.join(parts)}")
    lines.append(f"[timeline]    artifacts: {_artifact_text(artifacts)}")
    return lines


def _manual_section(name: str, artifacts: list[tuple[str, float, int]]) -> list[str]:
    return [
        f"[timeline] == {name}  kind=manual",
        "[timeline]    on-demand subject research (no signal linkage)",
        f"[timeline]    artifacts: {_artifact_text(artifacts)}",
    ]


def _other_section(name: str, artifacts: list[tuple[str, float, int]]) -> list[str]:
    return [
        f"[timeline] == {name}  kind=other",
        "[timeline]    no signal linkage (seam or manual run)",
        f"[timeline]    artifacts: {_artifact_text(artifacts)}",
    ]


def _run_anchor(
    artifacts: list[tuple[str, float, int]],
    sig: store_mod.Signal,
    phases: list[tuple[str, float, float, float]],
) -> float:
    """Chronological anchor for a run: its earliest *execution* event (phase
    start, artifact write, trigger). first_seen deliberately excluded — a
    signal can sit in the DB for days before its run, and the timeline must
    read in run order, not discovery order."""
    candidates = [mtime for _, mtime, _ in artifacts]
    candidates += [started for _, started, _, _ in phases]
    dt = _parse_iso(sig.triggered_at)
    if dt:
        candidates.append(dt.timestamp())
    return min(candidates) if candidates else 0.0


def _cycles_lines(runs_dir: Optional[Path]) -> list[str]:
    """Scheduler cycle ledger: one line per discovery-cycle fire, empty cycles
    included (they are evidence of quiet, healthy periods). Opt-in via
    OJ_SCHEDULER_RUNS (C7) — absent, the note is honest, not an error."""
    if runs_dir is None or not runs_dir.is_dir():
        return [
            "[timeline] cycle ledger: unavailable (set OJ_SCHEDULER_RUNS to the"
            " scheduler runs dir)"
        ]
    events: list[tuple[str, Any]] = []
    for f in sorted(runs_dir.glob("*discovery*.jsonl")):
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("startedAt"):
                events.append((ev["startedAt"], ev.get("exitCode")))
    if not events:
        return [f"[timeline] cycle ledger: no discovery cycles recorded in {runs_dir}"]
    lines = ["[timeline] -- discovery cycles (scheduler runs/*.jsonl) --"]
    for started, code in sorted(events):
        lines.append(f"[timeline] {_iso_local(started)}  cycle exit={code}")
    return lines


def cmd_timeline(ctx: config.Ctx) -> int:
    """Chronological reference of every research artifact the engine produced
    (D7, design §4.7): signal-triggered deep-dives AND on-demand subject runs,
    each with its signal chain (first_seen -> pre_qualify -> triage ->
    trigger), phase windows from traces.db, artifact mtimes, and outcome.
    FAILED runs and empty discovery cycles are kept — they are evidence the
    guards work. All times render in local UTC-3."""
    with store_mod.SignalStore(ctx.signals_db) as st:
        sigs = st.list_all()
    by_slug: dict[str, store_mod.Signal] = {}
    for s in sigs:
        if s.research_slug and s.research_slug not in by_slug:
            by_slug[s.research_slug] = s
    phases_by_slug = _traces_by_slug(ctx.traces_db)
    runs = _scan_runs(ctx.workspace)

    print("[timeline] Trend Seeker artifact timeline (times local, UTC-3)")
    print(f"[timeline] runs={len(runs)} signals={len(sigs)}")

    sections: list[tuple[float, list[str]]] = []
    seen = set()
    for name, artifacts in runs:
        seen.add(name)
        sig = by_slug.get(name)
        phases = phases_by_slug.get(name, [])
        if sig is not None:
            sections.append(
                (
                    _run_anchor(artifacts, sig, phases),
                    _signal_section(name, sig, artifacts, phases),
                )
            )
        elif name.startswith("subject-"):
            sections.append(
                (min(m for _, m, _ in artifacts), _manual_section(name, artifacts))
            )
        else:
            sections.append(
                (min(m for _, m, _ in artifacts), _other_section(name, artifacts))
            )
    for slug, sig in sorted(by_slug.items()):
        if slug in seen:
            continue
        sections.append(
            (
                _run_anchor([], sig, []),
                [
                    f"[timeline] == {slug}  kind=signal  status={sig.status}",
                    "[timeline]    no workspace artifacts found",
                ],
            )
        )

    for _anchor, lines in sorted(sections, key=lambda item: item[0]):
        for ln in lines:
            print(ln)
    if not sections:
        print(f"[timeline] no run artifacts in workspace ({ctx.workspace})")
        print(f"[timeline] no signals with research_slug in {ctx.signals_db}")
    for ln in _cycles_lines(ctx.scheduler_runs):
        print(ln)
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

    sub.add_parser(
        "timeline",
        help="chronological reference of every research artifact (§4.7, D7)",
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
    if args.cmd == "timeline":
        return cmd_timeline(ctx)
    return 2


if __name__ == "__main__":
    sys.exit(main())
