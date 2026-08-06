"""Launcher seam (bash): run-lock, delegation, and the cycle scaffold's honesty.

Exercises scripts/discovery/discovery.sh through the same `bash -c`-style seam
the pipeline harness uses — no reimplementation of the launcher logic (C4).
"""

from __future__ import annotations

import subprocess

from helpers import DISCOVERY_DIR, LAUNCHER, run_launcher


def test_launcher_is_shellcheck_clean():
    """bash -n is the portable syntax gate (C7: no GNU-only constructs)."""
    for script in (LAUNCHER,):
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def test_cycle_offline_mode_is_honest(tmp_path):
    """OJ_OFFLINE=1 keeps the launcher-level cycle offline: it must say so
    (D6), create the signals.db, and report zero counts — never a network
    call and never a fake collect."""
    proc = run_launcher(
        "run", "--cycle", state_dir=tmp_path, env_extra={"OJ_OFFLINE": "1"}
    )
    assert proc.returncode == 0, proc.stderr
    assert "offline mode" in proc.stdout
    assert "total=0 NEW=0" in proc.stdout
    assert (tmp_path / "signals.db").is_file()


def test_stats_subcommand_reports_zero_counts(tmp_path):
    proc = run_launcher("stats", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "total=0" in proc.stdout


def test_calibrate_subcommand_reports_no_evidence_yet(tmp_path):
    """Empty signals.db -> the honest 'no evidence' line (D6), exit 0."""
    proc = run_launcher("calibrate", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "no signals with score >= threshold yet" in proc.stdout


def test_calibrate_subcommand_reports_per_category_precision(tmp_path):
    """Seeded db -> per-category precision through the full bash seam (D7:
    the (score, outcome) pairs get a reader, thresholds tunable from data)."""
    from store import Signal, SignalStore

    with SignalStore(tmp_path / "signals.db") as st:
        for i, (score, category, status) in enumerate(
            [
                (9, "cloud", "DONE"),
                (8, "cloud", "FAILED"),
                (7, "cloud", "TRIAGED"),  # deferred: pending, not evidence
                (7, "storage", "DONE"),
            ]
        ):
            _inserted, sid = st.upsert(
                Signal(source="pricing", source_key=f"cal{i}", title=f"t{i}")
            )
            st.set_status(sid, status, score=score, category=category)

    proc = run_launcher("calibrate", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    cloud_line = next(line for line in proc.stdout.splitlines() if "cloud" in line)
    assert "eligible=  3 launched=  2 done=  1 failed=  1 pending=  1" in cloud_line
    assert "precision=50%" in cloud_line
    storage_line = next(line for line in proc.stdout.splitlines() if "storage" in line)
    assert "precision=100%" in storage_line


def test_unknown_source_is_a_usage_error(tmp_path):
    proc = run_launcher("run", "--source", "nope", state_dir=tmp_path)
    assert proc.returncode == 2
    assert "not registered" in proc.stdout


def test_disabled_placeholder_source_is_a_usage_error(tmp_path):
    """Placeholders are registered but disabled (design §4.3): asking for one
    by --source is a usage error, not a silent no-op."""
    proc = run_launcher(
        "run",
        "--source",
        "sec_edgar",
        state_dir=tmp_path,
        env_extra={"OJ_OFFLINE": "1"},
    )
    assert proc.returncode == 2
    assert "not enabled" in proc.stdout


def test_run_lock_defers_concurrent_cycle(tmp_path):
    """A held run-lock defers honestly (exit 0 + message), not a hard error."""
    lock_dir = tmp_path / "discovery.lock.d"
    lock_dir.mkdir()
    proc = run_launcher("stats", state_dir=tmp_path)
    assert proc.returncode == 0
    assert "another cycle holds the run-lock" in proc.stdout


def test_run_lock_is_released_after_a_cycle(tmp_path):
    run_launcher("run", "--cycle", state_dir=tmp_path)
    assert not (tmp_path / "discovery.lock.d").exists()
    # And a second cycle runs normally.
    proc = run_launcher("run", "--cycle", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_discovery_scripts_are_stdlib_only():
    """C5/C7: scripts/discovery/*.py must not import openjarvis (host python3
    cannot) — grep the import lines as a cheap guard."""
    for py in DISCOVERY_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("import ", "from ")):
                assert "openjarvis" not in line, f"{py.name}: {line}"
