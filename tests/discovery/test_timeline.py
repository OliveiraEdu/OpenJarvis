"""timeline subcommand: chronological artifact reference (D7 consumer, §4.7).

Exercises discovery.sh timeline through the same bash seam as the other
readers. Seeded signals.db / workspace / traces.db are hermetic: state_dir is
nested under tmp_path so run_launcher's derived workspace
(state_dir.parent / "workspace") is per-test, and OJ_SCHEDULER_RUNS is only
honored when explicitly set (C7).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import run_launcher
from store import Signal, SignalStore

LOCAL_TZ = timezone(timedelta(hours=-3))  # display frame: local UTC-3


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%H:%M:%S")


def _at_local(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).timestamp()


def _seed_signal(
    tmp_path,
    *,
    slug,
    status="DONE",
    source="hf",
    title="acme/t",
    score=9,
    category="ai",
    pre_qualify="ADOPTION_SPIKE",
    created_at="2026-08-06T15:01:15+00:00",
    triggered_at="2026-08-08T03:00:50+00:00",
):
    """One signal linked to a research slug, plus a fixed created_at so the
    first_seen render is deterministic."""
    state_dir = _state_dir(tmp_path)
    with SignalStore(state_dir / "signals.db") as st:
        _inserted, sid = st.upsert(
            Signal(
                source=source,
                source_key=slug,
                title=title,
                pre_qualify=pre_qualify,
            )
        )
        st.set_status(
            sid,
            status,
            score=score,
            category=category,
            research_slug=slug,
            triggered_at=triggered_at,
        )
    with sqlite3.connect(state_dir / "signals.db") as con:
        con.execute(
            "UPDATE signals SET created_at=? WHERE research_slug=?",
            (created_at, slug),
        )
        con.commit()


def _write_artifacts(workspace, slug, files):
    """Create a run dir with (name, size, mtime_epoch) artifacts."""
    d = workspace / slug
    d.mkdir(parents=True)
    for name, size, mtime in files:
        p = d / name
        p.write_text("x" * size)
        os.utime(p, (mtime, mtime))


def _seed_trace(tmp_path, query, started, ended, feedback=None):
    """A traces.db row in the exact columns cmd_timeline reads."""
    con = sqlite3.connect(_state_dir(tmp_path) / "traces.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS traces ("
        " query TEXT, started_at REAL, ended_at REAL, feedback REAL)"
    )
    con.execute(
        "INSERT INTO traces VALUES (?,?,?,?)", (query, started, ended, feedback)
    )
    con.commit()
    con.close()


def test_timeline_reports_no_runs_yet(tmp_path):
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "no run artifacts in workspace" in proc.stdout
    assert "no signals with research_slug" in proc.stdout


def test_timeline_lists_signal_run_chronology(tmp_path):
    """Signal-linked run: kind, status, score/cat/pq, first_seen + triggered
    rendered in local UTC-3, and artifact mtimes."""
    _seed_signal(tmp_path, slug="acme-t-scope-ai", status="DONE")
    t = _at_local(2026, 8, 8, 0, 3)
    _write_artifacts(
        _workspace(tmp_path),
        "acme-t-scope-ai",
        [
            ("findings.md", 4096, t),
            ("report.md", 2048, t + 120),
            ("state.json", 300, t + 120),
        ],
    )
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "runs=1 signals=1" in proc.stdout
    assert "== acme-t-scope-ai  kind=signal  status=DONE" in proc.stdout
    assert "source=hf title=acme/t score=9 cat=ai pq=ADOPTION_SPIKE" in proc.stdout
    assert "first_seen=2026-08-06 12:01 triggered=2026-08-08 00:00" in proc.stdout
    assert "artifacts: findings.md 4.0K 2026-08-08 00:03" in proc.stdout


def test_timeline_shows_phase_windows_from_traces(tmp_path):
    """Deep-dive phases attributed per run from traces.db prompts (topic ->
    slugify -> workspace dir), with local clock times + feedback."""
    _seed_signal(tmp_path, slug="acme-t-scope-ai", status="DONE")
    _write_artifacts(
        _workspace(tmp_path),
        "acme-t-scope-ai",
        [("report.md", 100, _at_local(2026, 8, 8, 0, 5))],
    )
    t0 = _at_local(2026, 8, 8, 0, 1, 26)
    t1 = _at_local(2026, 8, 8, 0, 3, 30)
    t2 = _at_local(2026, 8, 8, 0, 3, 50)
    t3 = _at_local(2026, 8, 8, 0, 4, 22)
    _seed_trace(
        tmp_path,
        "GATHER FACTS. Topic: acme/t | Scope: ai. Work this way: (1) Run 3-4 web_search queries",
        t0,
        t1,
        1.0,
    )
    _seed_trace(
        tmp_path,
        "VERIFY THE NUMBERS WITH THE CALCULATOR TOOL. Topic: acme/t | Scope: ai.",
        t1,
        t2,
        0.8,
    )
    _seed_trace(
        tmp_path,
        "WRITE PART 1 OF THE FINAL REPORT. Topic: acme/t | Scope: ai. Do this now",
        t2,
        t3,
        0.9,
    )
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert f"phases: gather {_clock(t0)}->{_clock(t1)} fb=1" in proc.stdout
    assert f"numbers {_clock(t1)}->{_clock(t2)} fb=0.8" in proc.stdout
    assert f"write-1 {_clock(t2)}->{_clock(t3)} fb=0.9" in proc.stdout


def test_timeline_marks_on_demand_subject_runs(tmp_path):
    _write_artifacts(
        _workspace(tmp_path),
        "subject-storage-systems-for-ai-training-",
        [("findings.md", 100, _at_local(2026, 8, 4, 12, 26))],
    )
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert "== subject-storage-systems-for-ai-training-  kind=manual" in proc.stdout
    assert "on-demand subject research (no signal linkage)" in proc.stdout


def test_timeline_keeps_failed_runs(tmp_path):
    _seed_signal(tmp_path, slug="minimax-fail", status="FAILED")
    _write_artifacts(
        _workspace(tmp_path),
        "minimax-fail",
        [("state.json", 376, _at_local(2026, 8, 7, 6, 6))],
    )
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert "== minimax-fail  kind=signal  status=FAILED" in proc.stdout
    assert "artifacts: state.json 376B 2026-08-07 06:06" in proc.stdout


def test_timeline_reports_triggered_signal_without_artifacts(tmp_path):
    """A research_slug with no workspace dir is reported honestly — never
    silently dropped (D6)."""
    _seed_signal(tmp_path, slug="ghost-run", status="DONE")
    proc = run_launcher("timeline", state_dir=_state_dir(tmp_path))
    assert "== ghost-run  kind=signal  status=DONE" in proc.stdout
    assert "no workspace artifacts found" in proc.stdout


def test_timeline_lists_cycle_ledger_when_runs_dir_given(tmp_path):
    """OJ_SCHEDULER_RUNS -> one ledger line per discovery cycle, empty cycles
    included; non-discovery job files are ignored."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "trend-seeker-discovery.jsonl").write_text(
        json.dumps({"startedAt": "2026-08-08T00:00:50-03:00", "exitCode": 0})
        + "\n"
        + json.dumps({"startedAt": "2026-08-08T06:00:05-03:00", "exitCode": 1})
        + "\n"
    )
    proc = run_launcher(
        "timeline",
        state_dir=_state_dir(tmp_path),
        env_extra={"OJ_SCHEDULER_RUNS": str(runs)},
    )
    assert "2026-08-08 00:00  cycle exit=0" in proc.stdout
    assert "2026-08-08 06:00  cycle exit=1" in proc.stdout
    (runs / "trend-seeker-outcome-check.jsonl").write_text(
        json.dumps({"startedAt": "2026-08-08T12:20:00-03:00", "exitCode": 0}) + "\n"
    )
    proc = run_launcher(
        "timeline",
        state_dir=_state_dir(tmp_path),
        env_extra={"OJ_SCHEDULER_RUNS": str(runs)},
    )
    assert "12:20" not in proc.stdout


def test_timeline_honest_when_scheduler_runs_missing(tmp_path):
    """An absent scheduler runs dir -> an explicit note, not an error (D6).
    (The launcher auto-derives the opencode runs dir when it exists; pointing
    OJ_SCHEDULER_RUNS at a nonexistent path forces the honest note.)"""
    proc = run_launcher(
        "timeline",
        state_dir=_state_dir(tmp_path),
        env_extra={"OJ_SCHEDULER_RUNS": str(tmp_path / "no-such-runs")},
    )
    assert "cycle ledger: unavailable" in proc.stdout


def test_timeline_ignores_unparseable_ledger_lines(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "trend-seeker-discovery.jsonl").write_text(
        "not json\n" + json.dumps({"startedAt": "2026-08-08T00:00:50-03:00"}) + "\n"
    )
    proc = run_launcher(
        "timeline",
        state_dir=_state_dir(tmp_path),
        env_extra={"OJ_SCHEDULER_RUNS": str(runs)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "cycle exit=None" in proc.stdout
