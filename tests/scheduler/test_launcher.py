"""Host scheduler wrapper (bash seam): syntax gate + env/config wiring.

Exercises scripts/scheduler/jarvis-host with a stub jarvis binary so the
wrapper's env derivation (host config, poll interval) is verified without
touching the real engine or the live scheduler.db (design §4.8 cutover prep).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "scheduler" / "jarvis-host"


def run_wrapper(tmp_path: Path, *args: str, env_extra: dict | None = None):
    env = dict(os.environ)
    env["OJ_STATE_DIR"] = str(tmp_path)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_wrapper_is_shellcheck_clean():
    """bash -n is the portable syntax gate (C7: no GNU-only constructs)."""
    proc = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_wrapper_errors_clearly_without_host_config(tmp_path):
    """No host config -> an actionable error, never a confusing daemon crash
    (D6: honest, explicit)."""
    proc = run_wrapper(tmp_path)
    assert proc.returncode == 1
    assert "host config not found" in proc.stderr
    assert "config.host.toml.example" in proc.stderr


def test_wrapper_execs_jarvis_scheduler_with_host_config(tmp_path):
    """With a host config + a stub jarvis, the wrapper must exec
    `scheduler start --poll-interval N` with OPENJARVIS_CONFIG set (the daemon
    path that replaces the opencode timers)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "jarvis"
    stub.write_text(
        "#!/bin/sh\n"
        "printf 'CFG=%s\\n' \"$OPENJARVIS_CONFIG\"\n"
        "printf 'ARGS=%s\\n' \"$*\"\n"
    )
    stub.chmod(0o755)
    cfg = tmp_path / "config.host.toml"
    cfg.write_text("[engine]\n")
    proc = run_wrapper(
        tmp_path,
        env_extra={
            "OJ_JARVIS_BIN": str(stub),
            "OJ_HOST_CONFIG": str(cfg),
            "OJ_POLL_INTERVAL": "30",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert f"CFG={cfg}" in proc.stdout
    assert "ARGS=scheduler start --poll-interval 30" in proc.stdout
