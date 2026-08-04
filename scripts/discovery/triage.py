#!/usr/bin/env python3
"""LLM triage — the only model touch in Layer 1 (design §4.6).

Only pre-qualified signals (rules) reach this stage. The prompt is a
versioned ``string.Template`` rendered to a single line (C2 — make splits
recipes on newlines), and the reply is machine-checked: extract the JSON
block, ``json.loads``, clamp the score to 1-10, coerce the category to the
known enum; on ANY failure the item is scored 0 with
``triage_reason="parse_failed"`` (D6: honest degrade).

The engine call goes through the existing seam — ``make -C <root>
jarvis-exec`` — in *direct-to-engine* mode (``jarvis ask --agent ''``, no
agent, ``--json``, low temperature). ``triage_signal`` takes an injectable
``ask`` so tests never touch the engine (C5).

Stdlib-only (host python3, no openjarvis import).
"""

from __future__ import annotations

import json
import re
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from config import Ctx
from store import Signal

# Known category enum (design §4.6); extension is a calibration (M7) concern.
CATEGORIES: tuple[str, ...] = (
    "db",
    "infra",
    "security",
    "storage",
    "ai",
    "data",
    "cloud",
    "observability",
    "devops",
    "unknown",
)
MAX_REASON_CHARS = 40
PARSE_FAILED = "parse_failed"

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "triage_prompt.txt"
CONTRACT_LINE = (
    '{"relevance_score": <integer 1-10>, "category": "<kebab-case or unknown>",'
    ' "reason": "<at most 40 chars>"}'
)

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Triage:
    """Validated triage result; score 0 means parse_failed (D6)."""

    score: int
    category: str
    reason: str


def render_prompt(sig: Signal, category_hint: str = ", ".join(CATEGORIES)) -> str:
    """Render the triage prompt to a SINGLE line (C2: the make recipe splits
    on newlines). ``metrics`` is compact JSON, safe inside the prompt."""
    template = string.Template(PROMPT_FILE.read_text(encoding="utf-8"))
    rendered = template.substitute(
        source=sig.source,
        title=sig.title,
        metrics=json.dumps(sig.metrics, sort_keys=True),
        category_hint=category_hint,
    )
    return _WS_RE.sub(" ", rendered).strip()


def _extract_json(text: str) -> Optional[str]:
    """The first balanced-brace JSON block — brace-aware inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_reply(reply: str) -> Triage:
    """Extract and validate the JSON contract; any violation -> score 0."""
    block = _extract_json(reply)
    if block is None:
        return Triage(0, "", PARSE_FAILED)
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return Triage(0, "", PARSE_FAILED)

    score = data.get("relevance_score")
    category = data.get("category")
    # bool is an int subclass — reject it as a score.
    if not isinstance(score, int) or isinstance(score, bool):
        return Triage(0, "", PARSE_FAILED)
    if not isinstance(category, str) or category.strip().lower() not in CATEGORIES:
        return Triage(0, "", PARSE_FAILED)
    reason = str(data.get("reason", ""))[:MAX_REASON_CHARS]
    return Triage(max(1, min(10, score)), category.strip().lower(), reason)


def ask_engine(ctx: Ctx, prompt: str) -> str:
    """Direct-to-engine ask through the pipeline's make seam (design §4.6).

    Mirrors research_phases.ask_agent: ``jarvis ask --agent '' --json
    --no-stream --temperature 0.1 '<prompt>'``; the return code is ignored —
    validation (parse_reply) decides, which keeps engine failures honest
    (parse_failed) instead of fatal.
    """
    escaped = prompt.replace(chr(39), chr(92) + chr(39))
    cmd = f"jarvis ask --agent '' --json --no-stream --temperature 0.1 '{escaped}'"
    proc = subprocess.run(
        ["make", "-C", str(ctx.root), "jarvis-exec", f"CMD={cmd}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout + proc.stderr


def triage_signal(
    ctx: Ctx, sig: Signal, *, ask: Callable[[Ctx, str], str] = ask_engine
) -> Triage:
    """Run the engine, then machine-check the reply (design §4.6)."""
    output = ask(ctx, render_prompt(sig))
    block = _extract_json(output)
    if block is None:
        return Triage(0, "", PARSE_FAILED)
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return Triage(0, "", PARSE_FAILED)
    # The seam returns the ask CLI's --json envelope; the model reply is
    # under "content" (a str). Without the envelope, ``block`` IS the contract
    # reply. A non-str ``content`` (e.g. null) falls through to parse_reply on
    # the block -> parse_failed, never a crash (D6).
    content = data.get("content") if isinstance(data, dict) else None
    return parse_reply(content if isinstance(content, str) else block)


__all__ = [
    "CATEGORIES",
    "CONTRACT_LINE",
    "PARSE_FAILED",
    "Triage",
    "ask_engine",
    "parse_reply",
    "render_prompt",
    "triage_signal",
]
