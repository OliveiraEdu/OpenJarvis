#!/usr/bin/env python3
"""Typed discovery configuration (C1).

``config.toml`` is parsed into frozen dataclasses — the same discipline as
``PhaseSpec``/``Ctx.from_env`` in the research pipeline: no
positional-argument plumbing, no dict-of-strings config access in application
code. Values that must hold (threshold range, non-negative caps) are validated
at load time, because validators verify properties, not text (D3).

Stdlib-only (tomllib): the discovery engine runs under the host ``python3``,
which cannot import ``openjarvis``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_THRESHOLD = 7
DEFAULT_MAX_TRIGGERS_PER_DAY = 3
DEFAULT_SUBJECT_TEMPLATE = "{title} | Scope: {category}"
DEFAULT_COOLDOWN_SECONDS = 86400  # 24 h fallback for unlisted sources

CONFIG_FILE = Path(__file__).resolve().parent / "config.toml"


@dataclass(frozen=True)
class DiscoveryConfig:
    """Committed defaults from config.toml, validated and typed."""

    threshold: int
    max_triggers_per_day: int
    subject_template: str
    cooldown_seconds: dict[str, int] = field(default_factory=dict)
    enabled_collectors: tuple[str, ...] = ()

    def cooldown_for(self, source: str) -> int:
        return self.cooldown_seconds.get(source, DEFAULT_COOLDOWN_SECONDS)


@dataclass(frozen=True)
class Ctx:
    """Launcher-injected environment, mirroring research_phases.Ctx.from_env.

    No machine-specific paths in committed code (C7): everything is derived
    from env vars with the same defaults research.sh uses, or from the repo
    root (this file's location).
    """

    state_dir: Path
    workspace: Path
    root: Path

    @classmethod
    def from_env(cls) -> "Ctx":
        root = Path(__file__).resolve().parents[2]  # repo root
        state_dir = Path(os.environ.get("OJ_STATE_DIR", Path.home() / ".openjarvis"))
        workspace = Path(
            os.environ.get(
                "OJ_WORKSPACE_HOST", Path.home() / "Git" / "openjarvis-workspace"
            )
        )
        return cls(state_dir=state_dir, workspace=workspace, root=root)

    @property
    def signals_db(self) -> Path:
        return self.state_dir / "signals.db"


def load_config(path: Path | None = None) -> DiscoveryConfig:
    """Parse config.toml into a validated DiscoveryConfig.

    Raises FileNotFoundError if the file is missing and ValueError if a
    property that must hold does not (D3).
    """
    cfg_path = path or CONFIG_FILE
    with open(cfg_path, "rb") as fh:
        data = tomllib.load(fh)

    d = data.get("discovery", {})
    cooldown = data.get("cooldown", {})
    collectors = data.get("collectors", {})

    threshold = int(d.get("threshold", DEFAULT_THRESHOLD))
    max_triggers = int(d.get("max_triggers_per_day", DEFAULT_MAX_TRIGGERS_PER_DAY))
    if not 1 <= threshold <= 10:
        raise ValueError(f"discovery.threshold must be 1-10, got {threshold}")
    if max_triggers < 0:
        raise ValueError(
            f"discovery.max_triggers_per_day must be >= 0, got {max_triggers}"
        )

    return DiscoveryConfig(
        threshold=threshold,
        max_triggers_per_day=max_triggers,
        subject_template=str(d.get("subject_template", DEFAULT_SUBJECT_TEMPLATE)),
        cooldown_seconds={str(k): int(v) for k, v in cooldown.items()},
        enabled_collectors=tuple(str(name) for name in collectors.get("enabled", [])),
    )


__all__ = [
    "CONFIG_FILE",
    "Ctx",
    "DEFAULT_COOLDOWN_SECONDS",
    "DiscoveryConfig",
    "load_config",
]
