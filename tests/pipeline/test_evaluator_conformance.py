"""D4 — the numbers-validator evaluator must stay in sync with the calculator.

``scripts/research_eval.py::eval_expr`` is a hand-ported copy of
``calculator.safe_eval`` semantics (same operators/functions, ``^`` and ``**``
both mean power). It exists because the validator runs under the host
``python3`` where ``openjarvis`` is not importable. This test pins the two
implementations together on a shared corpus so they cannot silently drift —
the exact failure class D4 exists to catch.

Known, deliberate divergences (documented in both files):
- division by zero: safe_eval returns ``inf`` (meval behavior); eval_expr
  raises FormulaError because an unverifiable figure must fail the gate, not
  pass it.
- ``log10``/``log2``: the Rust meval backend rejects them, so safe_eval
  errors on them; eval_expr (and the calculator's Python fallback) accept
  them. The validator is a deliberate superset.
"""

from __future__ import annotations

import math

import pytest

from research_eval import FormulaError, eval_expr
from openjarvis.tools.calculator import safe_eval

# Expressions both implementations must agree on (verified against the Rust
# meval backend; log10/log2 excluded per the docstring above).
CONFORMING = [
    "2+3",
    "10-3",
    "4*5",
    "10/4",
    "floor(10/3)",
    "10%3",
    "2^10",
    "2**10",
    "((60.12/55.78)^(1/1)-1)*100",
    "((87.5/60.12)^(1/5)-1)*100",
    "((87.5/60.12)**(1/5)-1)*100",
    "100*(1+0.15)^5",
    "sqrt(16)",
    "ln(e)",
    "abs(-3)",
    "sin(0)",
    "cos(0)",
    "tan(0)",
    "ceil(2.1)",
    "floor(2.9)",
    "round(2.4)",
    "round(3.6)",
    "min(1,2)",
    "max(1,2)",
    "pi",
    "e",
    "-5+3",
    "(2+3)*(4-1)",
]

# Expression both implementations must reject as unparseable/unsafe.
BOTH_REJECT = ["2+", "'hello'", "exec(1)", "x+1"]


@pytest.mark.parametrize("expr", CONFORMING)
def test_conforming_expressions_agree(expr):
    assert abs(eval_expr(expr) - safe_eval(expr)) < 1e-9, expr


@pytest.mark.parametrize("expr", BOTH_REJECT)
def test_garbage_rejected_by_both(expr):
    with pytest.raises((FormulaError, ValueError)):
        eval_expr(expr)
    with pytest.raises(ValueError):
        safe_eval(expr)


def test_div_zero_divergence_is_documented_and_intentional():
    """safe_eval says inf; the validator must say "cannot verify" so a
    division-by-zero figure fails the numbers gate instead of passing it."""
    assert safe_eval("1/0") == math.inf
    with pytest.raises(FormulaError):
        eval_expr("1/0")


def test_log10_log2_validator_is_superset_of_rust_backend():
    """eval_expr accepts log10/log2; the Rust backend rejects them — the
    validator must never reject a figure the calculator's Python fallback
    can verify."""
    assert eval_expr("log10(1000)") == pytest.approx(3.0)
    assert eval_expr("log2(8)") == pytest.approx(3.0)
    with pytest.raises(ValueError):
        safe_eval("log10(1000)")
