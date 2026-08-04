"""Typed config parsing (C1): defaults, overrides, and property validation (D3)."""

from __future__ import annotations

import pytest

from config import (
    CONFIG_FILE,
    DEFAULT_COOLDOWN_SECONDS,
    DiscoveryConfig,
    load_config,
)


def test_default_config_has_committed_defaults():
    cfg = load_config()
    assert isinstance(cfg, DiscoveryConfig)
    assert cfg.threshold == 7
    assert cfg.max_triggers_per_day == 3
    assert cfg.subject_template == "{title} | Scope: {category}"
    assert cfg.enabled_collectors == (
        "github",
        "hn",
        "reddit",
        "pypi",
        "pricing",
    )
    # Design §4.7 cooldown defaults.
    assert cfg.cooldown_for("github") == 86400
    assert cfg.cooldown_for("hn") == 43200
    assert cfg.cooldown_for("reddit") == 86400
    assert cfg.cooldown_for("pypi") == 604800
    assert cfg.cooldown_for("pricing") == 604800
    # Unlisted sources fall back.
    assert cfg.cooldown_for("edgar") == DEFAULT_COOLDOWN_SECONDS


def test_override_file_is_honored(tmp_path):
    overrides = tmp_path / "config.toml"
    overrides.write_text(
        "[discovery]\n"
        "threshold = 9\n"
        "max_triggers_per_day = 1\n"
        'subject_template = "{title} only"\n'
        "[cooldown]\n"
        "github = 3600\n"
        "[collectors]\n"
        'enabled = ["github"]\n'
    )
    cfg = load_config(overrides)
    assert cfg.threshold == 9
    assert cfg.max_triggers_per_day == 1
    assert cfg.subject_template == "{title} only"
    assert cfg.cooldown_for("github") == 3600
    assert cfg.enabled_collectors == ("github",)


@pytest.mark.parametrize("bad", [0, 11, -3])
def test_threshold_out_of_range_is_rejected(tmp_path, bad):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f"[discovery]\nthreshold = {bad}\n")
    with pytest.raises(ValueError, match="threshold"):
        load_config(cfg_file)


def test_negative_trigger_cap_is_rejected(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[discovery]\nmax_triggers_per_day = -1\n")
    with pytest.raises(ValueError, match="max_triggers_per_day"):
        load_config(cfg_file)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_committed_config_file_exists():
    assert CONFIG_FILE.is_file()
