"""D4 — the triage JSON contract is machine-checked (design §4.6).

Mirrors ``test_prompt_calculator_contract.py``: a test extracts the JSON
skeleton from the versioned template and validates it — parses as JSON, score
in 1–10, category in the known enum. The prompt cannot drift from the code
contract without failing here (one dialect, machine-checked).
"""

from __future__ import annotations

import json
import re

from store import Signal
from triage import CATEGORIES, CONTRACT_LINE, PROMPT_FILE, render_prompt

CONTRACT_RE = re.compile(r"Reply with ONLY a JSON object: (\{.*\})", re.DOTALL)

# Concrete values substituted for the skeleton placeholders so the contract
# parses as JSON (the template itself is a schema, not a literal reply).
SCORE = 7
CATEGORY = "db"
REASON = "x"


def _skeleton() -> str:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    match = CONTRACT_RE.search(text)
    assert match, "triage_prompt.txt must state the JSON contract"
    return match.group(1)


def _concrete() -> dict:
    skeleton = _skeleton()
    concrete = (
        skeleton.replace("<integer 1-10>", str(SCORE))
        .replace('"<kebab-case or unknown>"', f'"{CATEGORY}"')
        .replace('"<at most 40 chars>"', f'"{REASON}"')
    )
    return json.loads(concrete)


def test_contract_skeleton_parses_as_json_and_validates():
    data = _concrete()
    assert set(data) == {"relevance_score", "category", "reason"}
    assert isinstance(data["relevance_score"], int)
    assert 1 <= data["relevance_score"] <= 10
    assert data["category"] in CATEGORIES
    assert len(data["reason"]) <= 40


def test_prompt_contract_matches_code_constant():
    """The template's contract line and triage.CONTRACT_LINE are one string —
    the machine-checked dialect guard (D4)."""
    assert _skeleton() == CONTRACT_LINE


def test_render_hint_covers_the_whole_enum():
    """The category hint injected at render time enumerates every category the
    code accepts — the model is never asked for a category that can't parse."""
    rendered = render_prompt(Signal(source="hn", source_key="k", title="t"))
    for category in CATEGORIES:
        assert category in rendered


def test_every_category_round_trips_through_parse():
    """A reply in any taught category parses (coerced lowercase) — teaching
    and validation cannot drift apart."""
    from triage import parse_reply

    for category in CATEGORIES:
        reply = (
            f'{{"relevance_score": 8, "category": "{category.upper()}", "reason": "x"}}'
        )
        assert parse_reply(reply).category == category, category
