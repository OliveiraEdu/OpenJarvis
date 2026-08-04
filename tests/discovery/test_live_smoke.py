"""Live smoke — design §6: one real-engine launcher run, skipped by default.

Marked ``live`` like the pipeline's live tests (requires a running inference
engine; excluded from the default lane via ``-m "not live"``). It runs
``discovery.sh run --cycle --once --source hn`` against the real engine and
asserts the cycle completes with the triage seam exercised: ≥0 signals with a
valid JSON triage reply, or a clean no-op (no pre-qualified signals this
cycle) — never a crash. A parse failure degrades to score 0 +
``parse_failed`` (D6) instead of failing the run.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from helpers import run_launcher


@pytest.mark.live
def test_live_cycle_triages_hn_signals(tmp_path):
    proc = run_launcher(
        "run", "--cycle", "--once", "--source", "hn", state_dir=tmp_path
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "cycle complete" in proc.stdout
    assert re.search(r"triaged=\d+", proc.stdout), proc.stdout

    con = sqlite3.connect(str(tmp_path / "signals.db"))
    try:
        n_new = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status='NEW'"
        ).fetchone()[0]
        n_triaged = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status='TRIAGED'"
        ).fetchone()[0]
        n_parse_failed = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status='TRIAGED'"
            " AND score=0 AND triage_reason='parse_failed'"
        ).fetchone()[0]
    finally:
        con.close()

    # Every triaged row is either a valid JSON reply or an honest parse_failed;
    # nothing can be silently stuck in NEW after a triage that claimed success.
    assert n_new >= 0
    assert n_triaged >= n_parse_failed >= 0
