"""Deterministic math/provenance engine for the research pipeline.

Invoked by the bash functions in ``scripts/research_lib.sh`` so the exact
same logic runs in the live pipeline and in the offline harness
(``tests/pipeline/``, which sources the lib via ``bash -c``). Stdlib-only on
purpose: the live pipeline runs validators under the *host* ``python3``, which
cannot import ``openjarvis`` (only the uv venv at ``.venv/bin/python3`` can) —
this module must never import project code.

Keep the evaluator whitelists in sync with
``src/openjarvis/tools/calculator.py::safe_eval``: ``^`` and ``**`` both mean
power (``^`` is the Rust meval backend convention, ``**`` the LLM habit) and
the math functions/constants are the same set. Deliberate divergence: a
division by zero or a non-finite result is treated as *unverifiable* (error)
here, where the calculator returns ``inf``.

Subcommands
-----------
eval <expr>
    Print the evaluated result (exit 0) or a diagnostic (exit 1).

check-numbers-table <file>
    Property-check the numbers table: it must have at least 3 data rows, every
    parenthesized math expression must evaluate with calculator semantics, a
    claimed ``= <result>`` inside a group must match the computed value, and
    at least one expression must be a finite non-negative figure (real
    calculator evidence — a ``(2025-2030)`` year range evaluates, but
    negatively, so it can never satisfy the evidence requirement).

check-provenance <report> <findings>
    SOFT check (always exit 0): resolve every report URL against findings.md
    by normalized components (scheme, host without a leading ``www.``, path
    without a trailing slash; fragment and query dropped). Prints unmatched
    URLs and appends the PROVENANCE NOTE when any are found.
"""

from __future__ import annotations

import ast
import math
import operator
import re
import sys
from urllib.parse import urlsplit

# ── evaluator whitelists (mirror src/openjarvis/tools/calculator.py) ─────────
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MATH_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "ln": math.log,  # alias: ln(x) == log(x)
    "log10": math.log10,
    "log2": math.log2,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
    "ceil": math.ceil,
    "floor": math.floor,
}

# No whitespace, and no ")" or ">" so markdown links/HTML never bleed past
# the URL (the old grep -oE was line-scoped; excluding all whitespace keeps
# the extraction faithful line-by-line across the whole text).
_URL_RE = re.compile(r"https?://[^\s)>]+")
# After trimming whitespace and a trailing "= <number>", a parenthesized group
# is a *formula candidate* only when it is pure math punctuation (no letters —
# so "(CAGR 7.79% (2026-2031))" prose is ignored), contains a digit, and
# contains at least one real operator (+, -, *, /, ^; a bare "%" or a plain
# "(55.78)" is not a computation).
_FORMULA_RE = re.compile(r"^[0-9+\-*/^%().]+$")
_OPERATOR_RE = re.compile(r"[+\-*/^]")
_DIGIT_RE = re.compile(r"\d")
# A claimed result inside a group: "= 7.79" or "= 7.79%".
_CLAIMED_RESULT_RE = re.compile(r"\s*=\s*-?\d+(?:\.\d+)?\s*%?\s*$")


class FormulaError(ValueError):
    """Raised for anything the calculator would reject."""


def _eval_node(node: ast.AST):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise FormulaError(f"Unsupported constant: {type(node.value).__name__}")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise FormulaError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise FormulaError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormulaError("Only simple function calls are allowed")
        name = node.func.id
        if name not in _MATH_FUNCS:
            raise FormulaError(f"Unknown function: {name}")
        return _MATH_FUNCS[name](*[_eval_node(a) for a in node.args])
    if isinstance(node, ast.Name):
        val = _MATH_FUNCS.get(node.id)
        if isinstance(val, (int, float)):
            return val
        raise FormulaError(f"Unknown variable: {node.id}")
    raise FormulaError(f"Unsupported expression type: {type(node).__name__}")


def eval_expr(expression: str) -> float:
    """Evaluate like calculator.safe_eval (``^`` and ``**`` are both power)."""
    expression = expression.replace("^", "**")
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise FormulaError(str(exc)) from exc
    if isinstance(result, complex) or not math.isfinite(result):
        raise FormulaError("non-finite result")
    return float(result)


# ── check-numbers-table ───────────────────────────────────────────────────────


def _formula_groups(text: str):
    """Yield every balanced parenthesized group in ``text``, in scan order
    (outer groups before their inners).

    Raises FormulaError on unbalanced parentheses: an unclosed ``(`` or a
    stray ``)`` means a broken formula — the figure cannot be verified.
    """
    if text.count(")") != text.count("("):
        raise FormulaError("unbalanced parentheses in numbers table")
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "(":
            i += 1
            continue
        depth = 0
        j = i
        while j < n:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    yield text[i : j + 1]
                    break
            j += 1
        if depth != 0:  # reached EOF with an open paren
            raise FormulaError(
                f"unbalanced parentheses in numbers table (near {text[i : i + 40]!r})"
            )
        i += 1  # continue from inside the group to also scan inner groups


def _candidate_core(group: str) -> tuple[str | None, float | None]:
    """Return ``(evaluable_core, claimed_result_or_None)`` for a group, or
    ``(None, None)`` when the group is not a formula candidate (prose, a bare
    parenthesized figure, a percent literal without an operator)."""
    inner = group[1:-1]
    claimed = None
    m = _CLAIMED_RESULT_RE.search(inner)
    if m:
        claimed_text = m.group(0).strip().lstrip("=").strip().rstrip("%").strip()
        claimed = float(claimed_text)
        inner = _CLAIMED_RESULT_RE.sub("", inner)
    core = inner.strip()
    if not _DIGIT_RE.search(core):
        return None, None
    if not _FORMULA_RE.search(core):
        return None, None
    if not _OPERATOR_RE.search(core):
        return None, None
    return core, claimed


def check_numbers_table(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"check_numbers_table: cannot read {path} ({exc})")
        return 1

    rows = sum(1 for line in text.splitlines() if line.startswith("|"))
    if rows < 3:
        print(f"check_numbers_table: fewer than 3 table rows (found {rows})")
        return 1

    try:
        groups = list(_formula_groups(text))
    except FormulaError as exc:
        print(f"check_numbers_table: {exc}")
        return 1

    evidence = False
    for group in groups:
        core, claimed = _candidate_core(group)
        if core is None:
            continue
        try:
            value = eval_expr(core)
        except FormulaError as exc:
            print(f"check_numbers_table: unverifiable formula -> {group} ({exc})")
            return 1
        if claimed is not None:
            tolerance = max(0.01, abs(claimed) * 0.01)
            if abs(value - claimed) > tolerance:
                print(
                    "check_numbers_table: claimed result does not match computed "
                    f"-> {group} (computed {value:.6g}, claimed {claimed})"
                )
                return 1
        if value >= 0:
            evidence = True

    if not evidence:
        print(
            "check_numbers_table: no parenthesized calculator formula found "
            "(expected e.g. ((87.5/60.12)^(1/5)-1)*100)"
        )
        return 1
    return 0


# ── check-provenance ──────────────────────────────────────────────────────────


def _extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:)") for u in _URL_RE.findall(text)]


def _norm_url(url: str) -> tuple[str, str, str]:
    """Normalize a URL for resolution: lowercase scheme/host, drop a leading
    ``www.``, drop the trailing slash on the path, and ignore fragment/query
    (query strings are frequently tracking tokens that legitimately differ)."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return (parts.scheme.lower(), host, parts.path.rstrip("/"))


def check_provenance(report_path: str, findings_path: str) -> int:
    try:
        with open(report_path, encoding="utf-8") as fh:
            report_text = fh.read()
    except OSError:
        report_text = ""
    try:
        with open(findings_path, encoding="utf-8") as fh:
            findings_text = fh.read()
    except OSError:
        findings_text = ""

    report_urls = _extract_urls(report_text)
    if not report_urls:
        print("[research] provenance: no URLs found in report")
        return 0

    findings_norm = {_norm_url(u) for u in _extract_urls(findings_text)}

    total = 0
    unmatched = 0
    for url in report_urls:
        total += 1
        if _norm_url(url) not in findings_norm:
            unmatched += 1
            print(f"[research] provenance: UNMATCHED source URL -> {url}")

    print(
        f"[research] provenance: {unmatched}/{total} report URL(s) not found in findings.md"
    )
    if unmatched > 0:
        note = (
            f"\n> **PROVENANCE NOTE** — {unmatched} of {total} source URL(s) in this "
            "report were not found in the gathered findings; they may be "
            "fabricated — verify every URL before citing.\n"
        )
        with open(report_path, "a", encoding="utf-8") as fh:
            fh.write(note)
        print(f"[research] appended provenance caveat to {report_path.split('/')[-1]}")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: research_eval.py <eval|check-numbers-table|check-provenance> ...")
        return 2
    cmd = argv[0]
    if cmd == "eval" and len(argv) == 2:
        try:
            print(eval_expr(argv[1]))
            return 0
        except FormulaError as exc:
            print(f"eval_expression: invalid expression -> {argv[1]} ({exc})")
            return 1
    if cmd == "check-numbers-table" and len(argv) == 2:
        return check_numbers_table(argv[1])
    if cmd == "check-provenance" and len(argv) == 3:
        return check_provenance(argv[1], argv[2])
    print(f"usage: research_eval.py {cmd}: bad arguments")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
