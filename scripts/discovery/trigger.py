#!/usr/bin/env python3
"""Trigger — the decide→research.sh seam + run bookkeeping (design §4.7).

The pure pieces (C5, D3): ``subject_topic`` substitutes the committed subject
template, and ``slugify`` mirrors research.sh's slug rule EXACTLY (lowercase,
non-alnum -> '-', trim, 40-char cut, 'research' fallback) so the recorded
``research_slug`` is the workspace directory the launcher creates — the C4
seam contract, pinned by an offline test.

``launch_research`` is the only I/O: it runs ``scripts/research.sh <topic>``
synchronously via subprocess with the same env names research.sh reads
(``OJ_STATE_DIR``, ``OPENJARVIS_WORKSPACE_HOST`` — note the research launcher
does NOT read ``OJ_WORKSPACE_HOST``). The cycle owns the status transitions
(TRIGGERED with slug/triggered_at, then DONE, or FAILED on a raised runner
error — never a silently skipped trigger, D6); tests inject a fake runner.

Stdlib-only (host python3, no openjarvis import).
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Callable

from config import Ctx
from store import Signal

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def subject_topic(sig: Signal, template: str) -> str:
    """Design §4.7: ``{title} | Scope: {category}`` substituted with the
    signal's fields — the research.sh topic at trigger time."""
    return template.format(title=sig.title, category=sig.category)


def slugify(topic: str) -> str:
    """Mirror research.sh:63-65 — lowercase, runs of non-alnum to '-', trim,
    cut to 40 chars, fallback 'research'. This is the C4 contract: the slug
    recorded on the row must equal the workspace dir research.sh creates."""
    slug = _SLUG_STRIP.sub("-", topic.lower()).strip("-")[:40]
    return slug or "research"


def research_cmd(ctx: Ctx, topic: str) -> list[str]:
    """The launcher contract: ``bash scripts/research.sh <topic>``."""
    return ["bash", str(ctx.root / "scripts" / "research.sh"), topic]


def launch_research(ctx: Ctx, topic: str) -> str:
    """Run research.sh synchronously; return the workspace slug. research.sh
    reads ``OJ_STATE_DIR`` and ``OPENJARVIS_WORKSPACE_HOST`` (NOT
    ``OJ_WORKSPACE_HOST``) — mirror that env exactly. A nonzero exit raises,
    which the cycle records as FAILED (D6).
    """
    cmd = research_cmd(ctx, topic)
    env = dict(os.environ)
    env["OJ_STATE_DIR"] = str(ctx.state_dir)
    env["OPENJARVIS_WORKSPACE_HOST"] = str(ctx.workspace)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"research.sh exited {proc.returncode}: {proc.stderr[-500:]}"
        )
    return slugify(topic)


TriggerRunner = Callable[[Ctx, str], str]

__all__ = [
    "TriggerRunner",
    "launch_research",
    "research_cmd",
    "slugify",
    "subject_topic",
]
