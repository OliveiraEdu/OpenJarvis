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
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

DEFAULT_THRESHOLD = 7
DEFAULT_MAX_TRIGGERS_PER_DAY = 3
DEFAULT_RE_TRIAGE_DELTA = 0.3
DEFAULT_SUBJECT_TEMPLATE = "{title} | Scope: {category}"
DEFAULT_COOLDOWN_SECONDS = 86400  # 24 h fallback for unlisted sources

CONFIG_FILE = Path(__file__).resolve().parent / "config.toml"


@dataclass(frozen=True)
class GithubSettings:
    """scripts/discovery config [collectors.github] (design §4.3)."""

    q: str = "ai OR llm OR storage"
    min_stars: int = 50
    created_days: int = 90
    max_repos: int = 20


@dataclass(frozen=True)
class HNSettings:
    q: str = "postgres OR kubernetes OR llm"
    min_points: int = 50
    max_items: int = 20


@dataclass(frozen=True)
class HFSettings:
    """scripts/discovery config [collectors.hf] (design §4.3).

    The Hub's trending ranking is an attention proxy for model/adoption
    velocity; ``min_trending_score`` floors what the triage stage sees.
    """

    sort: str = "trendingScore"
    min_trending_score: float = 0.0
    max_items: int = 20


@dataclass(frozen=True)
class RedditSettings:
    subreddits: tuple[str, ...] = (
        "devops",
        "sysadmin",
        "dataengineering",
        "LocalLLaMA",
    )
    max_items: int = 15


@dataclass(frozen=True)
class PyPISettings:
    packages: tuple[str, ...] = ("pgvector", "ollama", "dask")


@dataclass(frozen=True)
class PricingSettings:
    urls: tuple[str, ...] = (
        "https://cloud.google.com/pricing",
        "https://azure.microsoft.com/en-us/pricing/",
    )


@dataclass(frozen=True)
class DiscoveryConfig:
    """Committed defaults from config.toml, validated and typed."""

    threshold: int
    max_triggers_per_day: int
    subject_template: str
    re_triage_delta: float = DEFAULT_RE_TRIAGE_DELTA
    cooldown_seconds: dict[str, int] = field(default_factory=dict)
    enabled_collectors: tuple[str, ...] = ()
    github: GithubSettings = field(default_factory=GithubSettings)
    hn: HNSettings = field(default_factory=HNSettings)
    hf: HFSettings = field(default_factory=HFSettings)
    reddit: RedditSettings = field(default_factory=RedditSettings)
    pypi: PyPISettings = field(default_factory=PyPISettings)
    pricing: PricingSettings = field(default_factory=PricingSettings)

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
    offline: bool = False

    @classmethod
    def from_env(cls) -> "Ctx":
        root = Path(__file__).resolve().parents[2]  # repo root
        state_dir = Path(os.environ.get("OJ_STATE_DIR", Path.home() / ".openjarvis"))
        workspace = Path(
            os.environ.get(
                "OJ_WORKSPACE_HOST", Path.home() / "Git" / "openjarvis-workspace"
            )
        )
        # OJ_OFFLINE=1 skips network collectors — air-gapped cycles and the
        # offline test harness, mirroring OJ_SKIP_SANITY in research.sh.
        offline = os.environ.get("OJ_OFFLINE", "") == "1"
        return cls(state_dir=state_dir, workspace=workspace, root=root, offline=offline)

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
    re_triage_delta = float(d.get("re_triage_delta", DEFAULT_RE_TRIAGE_DELTA))
    if not 1 <= threshold <= 10:
        raise ValueError(f"discovery.threshold must be 1-10, got {threshold}")
    if max_triggers < 0:
        raise ValueError(
            f"discovery.max_triggers_per_day must be >= 0, got {max_triggers}"
        )
    if not 0 < re_triage_delta < 1:
        raise ValueError(
            "discovery.re_triage_delta must be a fraction in (0, 1), got"
            f" {re_triage_delta}"
        )

    def _pos_int(section: str, key: str, default: int, what: str) -> int:
        value = int((collectors.get(section, {}) or {}).get(key, default))
        if value <= 0:
            raise ValueError(
                f"collectors.{section}.{key} must be > 0 ({what}), got {value}"
            )
        return value

    def _tuple(section: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = (collectors.get(section, {}) or {}).get(key)
        if raw is None:
            return default
        return tuple(str(item) for item in raw)

    def _min_trending_score(section: dict) -> float:
        value = float(section.get("min_trending_score", 0.0))
        if value < 0:
            raise ValueError(
                f"collectors.hf.min_trending_score must be >= 0, got {value}"
            )
        return value

    return DiscoveryConfig(
        threshold=threshold,
        max_triggers_per_day=max_triggers,
        subject_template=str(d.get("subject_template", DEFAULT_SUBJECT_TEMPLATE)),
        re_triage_delta=re_triage_delta,
        cooldown_seconds={str(k): int(v) for k, v in cooldown.items()},
        enabled_collectors=tuple(str(name) for name in collectors.get("enabled", [])),
        github=GithubSettings(
            q=str(
                (collectors.get("github", {}) or {}).get("q", "ai OR llm OR storage")
            ),
            min_stars=_pos_int("github", "min_stars", 50, "minimum stars"),
            created_days=_pos_int("github", "created_days", 90, "created-window days"),
            max_repos=_pos_int("github", "max_repos", 20, "max repos per cycle"),
        ),
        hn=HNSettings(
            q=str(
                (collectors.get("hn", {}) or {}).get(
                    "q", "postgres OR kubernetes OR llm"
                )
            ),
            min_points=_pos_int("hn", "min_points", 50, "minimum points"),
            max_items=_pos_int("hn", "max_items", 20, "max items per cycle"),
        ),
        hf=HFSettings(
            sort=str((collectors.get("hf", {}) or {}).get("sort", "trendingScore")),
            min_trending_score=_min_trending_score(collectors.get("hf", {}) or {}),
            max_items=_pos_int("hf", "max_items", 20, "max items per cycle"),
        ),
        reddit=RedditSettings(
            subreddits=_tuple(
                "reddit",
                "subreddits",
                ("devops", "sysadmin", "dataengineering", "LocalLLaMA"),
            ),
            max_items=_pos_int("reddit", "max_items", 15, "max items per subreddit"),
        ),
        pypi=PyPISettings(
            packages=_tuple("pypi", "packages", ("pgvector", "ollama", "dask")),
        ),
        pricing=PricingSettings(
            urls=_tuple(
                "pricing",
                "urls",
                (
                    "https://cloud.google.com/pricing",
                    "https://azure.microsoft.com/en-us/pricing/",
                ),
            ),
        ),
    )


__all__ = [
    "CONFIG_FILE",
    "Ctx",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_RE_TRIAGE_DELTA",
    "DiscoveryConfig",
    "GithubSettings",
    "HFSettings",
    "HNSettings",
    "PricingSettings",
    "PyPISettings",
    "RedditSettings",
    "load_config",
]
