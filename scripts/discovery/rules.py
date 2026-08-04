#!/usr/bin/env python3
"""Rule filters — deterministic, pure (design §4.4).

The rules carry the pre-qualification burden so the LLM only scores a handful
of candidates per cycle. Every function here is pure (no I/O, no store
access): the cycle passes the *prior* cycle's stored Signal (``prior``) so
delta rules (star acceleration, download delta, pricing diff) can compare
snapshots — the stored row is the previous cycle's state until the upsert
refreshes it.

``pre_qualify(sig, prior, now)`` is the filter-stage entry point: it returns
the tags (HIGH_VELOCITY_OS, CHURN_SIGNAL, PRICING_DIFF, ADOPTION_SPIKE) that
decide whether a signal reaches LLM triage (M4). ``noise_filters`` returns
True for candidates that are dropped before storage.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from store import Signal

# Design §4.4: "migrating off/away from X", "too expensive", "deprecated",
# "alternatives to Y". v1 matches on titles only (bodies need the OAuth/HTML
# sources that are still placeholders); the body parameter exists for them.
CHURN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(migrating|migrate|migrated|migrates|moving)\s+(off|away from)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\btoo expensive\b", re.IGNORECASE),
    re.compile(r"\bdeprecated\b", re.IGNORECASE),
    re.compile(r"\balternatives? to\b", re.IGNORECASE),
)

# Pre-qualification thresholds (design §4.4): >200 stars/week, repo age < 30d
# with >15 contributors, any positive download growth vs. the prior cycle.
STAR_ACCEL_PER_WEEK = 200.0
SPIKE_REPO_AGE_DAYS = 30
SPIKE_CONTRIBUTORS = 15

# Token substrings for repo noise (design §4.4: dotfiles, demo/tutorial).
_NOISE_REPO_TOKENS = ("demo", "tutorial", "sample")


def _parse(dt: str) -> datetime:
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def churn_phrases(title: str, body: str = "") -> list[str]:
    """Matched churn phrases in a title/body, e.g. ['migrating off']."""
    text = f"{title}\n{body}"
    return [m.group(0) for pattern in CHURN_PATTERNS for m in pattern.finditer(text)]


def engagement_ratio(points: Optional[int], comments: Optional[int]) -> Optional[float]:
    """Comments per upvote; None when points are missing or zero (design §4.4)."""
    if not isinstance(points, int) or points <= 0 or not isinstance(comments, int):
        return None
    return comments / points


def star_acceleration(
    sig: Signal, prior: Optional[Signal], *, window_days: int = 7
) -> Optional[float]:
    """Stars gained per week vs. the prior cycle; None when not computable
    (first sighting, non-github, missing stars) — a delta cannot be claimed
    without a baseline (D6)."""
    if sig.source != "github" or prior is None:
        return None
    cur = sig.metrics.get("stars")
    prev = prior.metrics.get("stars")
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    return (cur - prev) / window_days


def contributor_spike(sig: Signal, now: str) -> bool:
    """Repo age < 30d with >15 contributors (design §4.4). ``now`` keeps the
    rule pure w.r.t. time (C5). Current collectors do not expose contributor
    counts (GitHub search API doesn't), so this degrades to False until a
    source provides them — the honest default (D6)."""
    created = sig.metrics.get("created_at")
    contributors = sig.metrics.get("contributors")
    if (
        not created
        or not isinstance(contributors, int)
        or contributors <= SPIKE_CONTRIBUTORS
    ):
        return False
    try:
        age = _parse(now) - _parse(created)
    except ValueError:
        return False
    return age < timedelta(days=SPIKE_REPO_AGE_DAYS)


def download_delta(sig: Signal, prior: Optional[Signal]) -> Optional[float]:
    """PyPI weekly downloads vs. the prior cycle (design §4.4); None without
    a baseline or when pypistats was unavailable (best-effort metric, D6)."""
    if sig.source != "pypi" or prior is None:
        return None
    cur = sig.metrics.get("downloads_last_week")
    prev = prior.metrics.get("downloads_last_week")
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return None
    return float(cur - prev)


def pricing_changed(sig: Signal, prior: Optional[Signal]) -> bool:
    """Normalized-content hash moved vs. the prior cycle (design §4.3/§4.4)."""
    if sig.source != "pricing" or prior is None:
        return False
    cur = sig.metrics.get("content_hash")
    prev = prior.metrics.get("content_hash")
    return bool(cur) and bool(prev) and cur != prev


def noise_filters(sig: Signal) -> bool:
    """True drops the candidate before storage (design §4.4): dotfiles,
    demo/tutorial repos, curated lists, and unengaged single-user posts."""
    if sig.source == "github":
        name = sig.title or ""
        # GitHub full_name is "owner/repo": the repo segment decides.
        repo = name.split("/")[-1]
        if repo.startswith(".") or repo.startswith("awesome-"):
            return True
        if any(token in name.lower() for token in _NOISE_REPO_TOKENS):
            return True
    if sig.source in ("hn", "reddit"):
        points = sig.metrics.get("points")
        comments = sig.metrics.get("num_comments")
        if points == 0 and comments == 0:
            # Unengaged single-user post (reddit RSS carries no scores, so it
            # can never fire there — no evidence is not noise).
            return True
    return False


def pre_qualify(sig: Signal, prior: Optional[Signal], *, now: str) -> list[str]:
    """Filter-stage entry point: tags attached to ``sig.pre_qualify``.

    Tags (design §4.2/§4.4): HIGH_VELOCITY_OS, CHURN_SIGNAL, PRICING_DIFF,
    ADOPTION_SPIKE. Empty list means the signal does not reach LLM triage.
    """
    tags: list[str] = []
    if sig.source == "github":
        accel = star_acceleration(sig, prior)
        if (accel is not None and accel > STAR_ACCEL_PER_WEEK) or contributor_spike(
            sig, now
        ):
            tags.append("HIGH_VELOCITY_OS")
    if churn_phrases(sig.title):
        tags.append("CHURN_SIGNAL")
    if pricing_changed(sig, prior):
        tags.append("PRICING_DIFF")
    if sig.source == "pypi":
        delta = download_delta(sig, prior)
        if delta is not None and delta > 0:
            tags.append("ADOPTION_SPIKE")
    return tags


__all__ = [
    "CHURN_PATTERNS",
    "STAR_ACCEL_PER_WEEK",
    "churn_phrases",
    "contributor_spike",
    "download_delta",
    "engagement_ratio",
    "noise_filters",
    "pre_qualify",
    "pricing_changed",
    "star_acceleration",
]
