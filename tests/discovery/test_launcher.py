"""Launcher seam (bash): run-lock, delegation, and the cycle scaffold's honesty.

Exercises scripts/discovery/discovery.sh through the same `bash -c`-style seam
the pipeline harness uses — no reimplementation of the launcher logic (C4).
"""

from __future__ import annotations

import subprocess

from helpers import DISCOVERY_DIR, LAUNCHER, REPO_ROOT, run_launcher


def test_launcher_is_shellcheck_clean():
    """bash -n is the portable syntax gate (C7: no GNU-only constructs)."""
    for script in (LAUNCHER,):
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def test_cycle_with_empty_registry_is_honest_noop(tmp_path):
    """M1 has no collectors wired: the cycle must say so (D6), create the
    signals.db, and still report counts — never pretend it collected."""
    proc = run_launcher("run", "--cycle", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "no collectors registered yet" in proc.stdout
    assert "total=0 NEW=0" in proc.stdout
    assert (tmp_path / "signals.db").is_file()


def test_stats_subcommand_reports_zero_counts(tmp_path):
    proc = run_launcher("stats", state_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "total=0" in proc.stdout


def test_unknown_source_is_a_usage_error(tmp_path):
    proc = run_launcher("run", "--source", "nope", state_dir=tmp_path)
    assert proc.returncode == 2
    assert "not registered" in proc.stdout


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
