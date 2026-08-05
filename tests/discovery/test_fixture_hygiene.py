"""Fixture integrity + hygiene (C3/C6/C7).

Guards the committed discovery fixtures: exported signals and triage replies
must stay self-consistent, replay cleanly through the same pure modules the
live engine uses (`rules.py`, `triage.py`), and never carry secrets (Tavily/
OpenAI/GitHub keys — fixtures are committed, so they can only contain shapes
that look nothing like a key). Mirrors
`tests/pipeline/test_trace_fixtures.py` and extends it with discovery-specific
integrity + replay checks (design §6, §10.6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import rules
import triage
from store import Signal

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SIGNALS = FIXTURES / "signals"
TRIAGE_REPLIES = FIXTURES / "triage_replies"

SECRET_PATTERNS = [
    re.compile(r"tvly-[A-Za-z0-9_-]{10,}"),  # Tavily
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI/Anthropic style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"\bBearer [A-Za-z0-9._-]{20,}"),  # bearer tokens
]

# Allowed top-level fields in an exported signal fixture — the exporter's
# sanitization contract (design §10.6): identity/timestamp fields (id,
# source_key, url, research_slug, triggered_at, created_at, updated_at) are
# never committed.
SIGNAL_FIELDS = frozenset(
    {
        "source",
        "title",
        "metrics",
        "pre_qualify",
        "score",
        "category",
        "triage_reason",
        "status",
    }
)

# Allowed metric keys per source — must stay in lockstep with the exporter's
# METRIC_WHITELIST (design §10.6).
METRIC_WHITELIST: dict[str, frozenset[str]] = {
    "github": frozenset(
        {
            "stars",
            "forks",
            "open_issues",
            "created_at",
            "pushed_at",
            "language",
            "license",
            "owner_type",
            "contributors",
        }
    ),
    "hn": frozenset({"points", "num_comments", "created_at"}),
    "reddit": frozenset({"subreddit", "updated"}),
    "pypi": frozenset(
        {
            "version",
            "requires_python",
            "releases_count",
            "latest_release",
            "downloads_last_week",
            "summary",
        }
    ),
    "pricing": frozenset({"content_hash", "bytes", "normalized_len"}),
}

NOW = "2026-08-05T12:00:00+00:00"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_exported_signals_are_sanitized_and_well_formed():
    files = sorted(SIGNALS.glob("*.json"))
    assert files, "no exported signal fixtures"
    for path in files:
        fx = _load(path)
        assert set(fx) == SIGNAL_FIELDS, (
            f"{path.name}: unexpected field(s) — identity/timestamp fields must"
            " be dropped by the exporter"
        )
        assert fx["source"] in METRIC_WHITELIST, path.name
        assert set(fx["metrics"]).issubset(METRIC_WHITELIST[fx["source"]]), path.name
        assert fx["status"] in {"NEW", "TRIAGED", "TRIGGERED", "DONE", "FAILED"}
        if fx["score"] is None:
            assert fx["category"] == "", path.name
            assert fx["triage_reason"] == "", path.name
        else:
            assert 1 <= fx["score"] <= 10, path.name
            assert fx["category"] in triage.CATEGORIES, path.name
            assert len(fx["triage_reason"]) <= triage.MAX_REASON_CHARS, path.name


def test_triage_replies_are_self_consistent_with_signal_fixtures():
    """Every triage reply's input must match an exported signal fixture
    exactly, and its result must satisfy the triage contract (the fixture is
    the C6 record: real input -> real machine-checked output)."""
    replies = sorted(TRIAGE_REPLIES.glob("*.json"))
    assert replies, "no exported triage-reply fixtures"
    inputs = {
        json.dumps(
            {k: _load(p)[k] for k in ("source", "title", "metrics")},
            sort_keys=True,
        )
        for p in SIGNALS.glob("*.json")
    }
    for path in replies:
        fx = _load(path)
        assert set(fx) == {"signal", "triage"}, path.name
        sig = fx["signal"]
        assert set(sig) == {"source", "title", "metrics"}, path.name
        assert json.dumps(sig, sort_keys=True) in inputs, (
            f"{path.name}: input not among exported signals"
        )
        t = fx["triage"]
        assert set(t) == {"score", "category", "reason"}, path.name
        assert 1 <= t["score"] <= 10, path.name
        assert t["category"] in triage.CATEGORIES, path.name
        assert len(t["reason"]) <= triage.MAX_REASON_CHARS, path.name


def test_signal_fixtures_replay_through_rules_and_triage_render():
    """Real payload shapes must keep flowing through the pure modules without
    crashing (collector-parse/rule-edge regressions surface here, C3). No
    stored signal is noise (the cycle filters before storage), so the
    noise re-check must stay False; the rendered prompt must stay single-line
    (C2)."""
    for path in sorted(SIGNALS.glob("*.json")):
        fx = _load(path)
        sig = Signal(
            source=fx["source"],
            source_key="fixture",
            title=fx["title"],
            metrics=fx["metrics"],
        )
        tags = rules.pre_qualify(sig, None, now=NOW)  # first sighting: no prior
        assert isinstance(tags, list), path.name
        assert rules.noise_filters(sig) is False, path.name
        rendered = triage.render_prompt(sig)
        assert "\n" not in rendered, f"{path.name}: triage prompt not single-line"


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.pattern)
def test_fixtures_contain_no_secret_shaped_tokens(pattern):
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = pattern.search(text)
        assert match is None, (
            f"possible secret in {path.relative_to(FIXTURES)}: {match.group(0)}"
        )
