"""D4 — one dialect per tool; prompt<->tool contracts are machine-checked.

Every ``calculator(expression=...)`` example the model is taught (in the
phase prompts and in the frozen trace-derived asklog) must evaluate with the
production safe_eval. This would have caught the ``**`` regression (the Rust
meval backend rejects ``**``) BEFORE the first live run, and it guards the
prompt text from silently drifting back to a dialect the tool rejects.
"""

from __future__ import annotations

import re

import pytest

from tests.pipeline.helpers import ASKSLOGS, REPO_ROOT
from openjarvis.tools.calculator import safe_eval

EXPR_RE = re.compile(r"calculator\(expression=['\"]([^'\"]+)['\"]")
ASKLOG_EXPR_RE = re.compile(r"calculator expression=([^\n]+)")

# The phase prompts are where the model is explicitly taught calculator
# notation; the template names the tool but teaches no dialect.
RESEARCH_SH = REPO_ROOT / "scripts" / "research.sh"
TEMPLATE = REPO_ROOT / "deploy" / "templates" / "it_market_analyst.toml"

# One known result to pin exact math (CAGR example from the VERIFY prompt).
CAGR_EXPR = "((60.12/55.78)^(1/1)-1)*100"


def test_calculator_examples_in_research_sh_evaluate():
    exprs = EXPR_RE.findall(RESEARCH_SH.read_text(encoding="utf-8"))
    assert exprs, "no calculator(expression=...) examples found in research.sh"
    for expr in exprs:
        assert safe_eval(expr) is not None


def test_template_mentions_calculator_without_teaching_dialect():
    """The template's system prompt names the calculator tool but teaches no
    notation at all — so it cannot drift into teaching the broken `**`."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "calculator" in text
    assert "**" not in text


def test_prompts_never_teach_double_asterisk():
    """The Rust backend rejects ``**``; the prompt dialect must use ``^``."""
    for src in (RESEARCH_SH, TEMPLATE):
        text = src.read_text(encoding="utf-8")
        for match in EXPR_RE.finditer(text):
            assert "**" not in match.group(1), (
                f"{src.name}: double-asterisk inside calculator example: "
                f"{match.group(0)[:80]}"
            )


def test_verify_prompt_cagr_example_matches_expected_value():
    assert safe_eval(CAGR_EXPR) == pytest.approx(7.78, abs=0.01)


def test_trace_asklog_calculator_expression_evaluates():
    """C3 — the exact expression that FAILED in the live run (``**``) is
    frozen in the trace-derived asklog and must now evaluate via safe_eval
    (the normalization fix). This is the regression test for dc464a66 at the
    trace-replay layer."""
    log = (ASKSLOGS / "verify-degenerate.txt").read_text(encoding="utf-8")
    exprs = ASKLOG_EXPR_RE.findall(log)
    assert exprs, "verify-degenerate asklog has no calculator expression"
    for expr in exprs:
        assert safe_eval(expr) is not None


def test_all_asklog_calculator_expressions_evaluate():
    """Every calculator call frozen in every asklog fixture must evaluate —
    a guard that production failures keep a replayable record."""
    for asklog in ASKSLOGS.glob("*.txt"):
        text = asklog.read_text(encoding="utf-8")
        for expr in ASKLOG_EXPR_RE.findall(text):
            assert safe_eval(expr) is not None, f"{asklog.name}: {expr}"
