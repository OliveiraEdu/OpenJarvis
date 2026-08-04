"""Shared helpers for the discovery tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_DIR = REPO_ROOT / "scripts" / "discovery"
LAUNCHER = DISCOVERY_DIR / "discovery.sh"


def run_launcher(
    *args: str,
    state_dir: Path,
    skip_sanity: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run discovery.sh through the bash seam with an isolated state dir.

    OJ_SKIP_SANITY=1 keeps the harness offline (no make jarvis-health).
    """
    env = dict(os.environ)
    env["OJ_STATE_DIR"] = str(state_dir)
    env["OJ_WORKSPACE_HOST"] = str(state_dir.parent / "workspace")
    if skip_sanity:
        env["OJ_SKIP_SANITY"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        env=env,
    )
