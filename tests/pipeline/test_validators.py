"""Validators + heading-repair tests over real trace-derived fixtures.

D3 — validators verify properties, not text: these tests pin the *actual*
behavior of the grep-anchored validators against artifacts a live run
produced, including the negative cases that caused real retries (banner-only
numbers.md, glued headings in report.md).
"""

from __future__ import annotations

from tests.pipeline.helpers import ARM, HPC, run_lib


def test_numbers_table_rejects_unverified_banner_only(tmp_path):
    """Real degraded numbers.md (banner, zero rows) must fail the gate."""
    proc = run_lib('check_numbers_table "$1"', str(HPC / "numbers.md"))
    assert proc.returncode == 1


def test_numbers_table_accepts_table_with_parenthesized_formula(tmp_path):
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | base | end | formula | result |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| CAGR | 2025 | 2030 | ((60.12/55.78)^(1/1)-1)*100 | 7.78 |\n"
        "| Market | 2025 | 2030 | 55.78 | 55.78 |\n"
        "| Share | 2025 | 2030 | 60 | 60 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 0, proc.stderr


def test_numbers_table_accepts_caret_and_double_star_and_inline_claim(tmp_path):
    """D3 — formulas are re-evaluated: ^ and ** are both power (calculator.py
    semantics), and an inline claimed result inside the parens must agree
    with the computation."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | formula | result |\n"
        "| --- | --- | --- |\n"
        "| CAGR | ((87.5/60.12)**(1/5)-1)*100 | 7.79 |\n"
        "| CAGR | (60.12/55.78*100-100 = 7.78%) | 7.78 |\n"
        "| Market | (87.5/60.12) | 1.46 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 0, proc.stderr


def test_numbers_table_ignores_prose_parentheses(tmp_path):
    """Descriptive parens (CAGR notes, percent literals, year ranges in
    prose) must not fail the gate as long as real calculator evidence is
    present."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | formula | result |\n"
        "| --- | --- | --- |\n"
        "| CAGR | ((87.5/60.12)^(1/5)-1)*100 | 7.79 |\n"
        "| Note | (CAGR 7.79% (2026-2031)) | - |\n"
        "| Share | (60%) | 60 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 0, proc.stderr


def test_numbers_table_rejects_unbalanced_parentheses(tmp_path):
    """An unclosed paren means a broken formula — the figure cannot be
    verified, so the gate must fail."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | base | end | formula | result |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| CAGR | 2025 | 2031 | ((87.5/60.12)^(1/5)-1 | 7.79 |\n"
        "| Market | 2025 | 2031 | 87.5 | 87.5 |\n"
        "| Market | 2025 | 2031 | 60.12 | 60.12 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 1
    assert "unbalanced parentheses" in proc.stdout


def test_numbers_table_rejects_unverifiable_formula(tmp_path):
    """A balanced but unevaluable expression (trailing operator) must fail —
    it is evidence the calculator was not actually used on that figure."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | formula | result |\n"
        "| --- | --- | --- |\n"
        "| CAGR | ((87.5/60.12)^(1/5)-1) | 7.79 |\n"
        "| CAGR | (60.12/55.78*) | 1.08 |\n"
        "| Market | (87.5/60.12) | 1.46 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 1
    assert "unverifiable formula" in proc.stdout


def test_numbers_table_rejects_year_ranges_as_only_evidence(tmp_path):
    """(2025-2030) evaluates — to a negative number — so a table whose only
    parens are year ranges is NOT calculator evidence and must fail."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | period | result |\n"
        "| --- | --- | --- |\n"
        "| Market | (2025-2030) | 87.5 |\n"
        "| CAGR | (2025-2030) | 7.79 |\n"
        "| Share | (2025-2030) | 60 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 1
    assert "no parenthesized calculator formula" in proc.stdout


def test_numbers_table_rejects_claimed_result_mismatch(tmp_path):
    """A claimed inline result that disagrees with the computation means the
    figure is wrong — the gate must fail rather than ship it."""
    numbers = tmp_path / "numbers.md"
    numbers.write_text(
        "| metric | formula | result |\n"
        "| --- | --- | --- |\n"
        "| CAGR | (60.12/55.78*100-100 = 5.0) | 5.0 |\n"
        "| CAGR | ((87.5/60.12)^(1/5)-1) | 7.79 |\n"
        "| Market | (87.5/60.12) | 1.46 |\n"
    )
    proc = run_lib('check_numbers_table "$1"', str(numbers))
    assert proc.returncode == 1
    assert "claimed result does not match computed" in proc.stdout


def test_eval_expression_matches_calculator_semantics():
    """The bash evaluator seam: ^ and ** are both power, math funcs work —
    same semantics as calculator.safe_eval."""
    for expr, expected in [
        ("((87.5/60.12)^(1/5)-1)*100", 7.7948),
        ("((87.5/60.12)**(1/5)-1)*100", 7.7948),
        ("sqrt(16)+2^3", 12.0),
    ]:
        proc = run_lib('eval_expression "$1"', expr)
        assert proc.returncode == 0, proc.stderr
        assert abs(float(proc.stdout.strip()) - expected) < 1e-3


def test_eval_expression_rejects_broken_expression():
    proc = run_lib('eval_expression "$1"', "2+")
    assert proc.returncode == 1


def test_report_part1_accepts_real_part1_snapshot():
    proc = run_lib('check_report_part1 "$1"', str(ARM / "report.part1"))
    assert proc.returncode == 0, proc.stderr


def test_report_part1_rejects_missing_detailed_analysis(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Title\n\n## Introduction\n\n## Executive Summary\n")
    proc = run_lib('check_report_part1 "$1"', str(report))
    assert proc.returncode == 1


def test_report_sections_accepts_completed_hpc_report():
    proc = run_lib('check_report_sections "$1"', str(HPC / "report.md"))
    assert proc.returncode == 0, proc.stderr


def test_report_sections_rejects_glued_arm_report():
    """The pre-fix ARM artifact: ## Sources & References and ## Confidence
    Assessment are glued to paragraph text, so the anchored regex cannot see
    them — this is exactly the failure that made the run retry/degrade."""
    proc = run_lib('check_report_sections "$1"', str(ARM / "report.md"))
    assert proc.returncode == 1


def test_fix_glued_headings_repairs_and_is_idempotent(tmp_path):
    fixed = tmp_path / "fixed.md"
    fixed.write_text((ARM / "report.md").read_text(encoding="utf-8"))
    pristine = (ARM / "report.md").read_text(encoding="utf-8")

    proc = run_lib('fix_glued_headings "$1"', str(fixed))
    assert proc.returncode == 0, proc.stderr
    assert fixed.read_text(encoding="utf-8") != pristine  # repair did something

    # the validator now passes on the repaired copy (content was complete)
    proc = run_lib('check_report_sections "$1"', str(fixed))
    assert proc.returncode == 0, proc.stderr

    # idempotent: a second repair is a byte-identical no-op
    once = fixed.read_bytes()
    run_lib('fix_glued_headings "$1"', str(fixed))
    assert fixed.read_bytes() == once


def test_fix_glued_headings_leaves_clean_report_untouched(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Title\n\n## Section\n\ntext\n\n## Other\n")
    run_lib('fix_glued_headings "$1"', str(report))
    assert (
        report.read_text(encoding="utf-8")
        == "# Title\n\n## Section\n\ntext\n\n## Other\n"
    )


def test_fix_glued_headings_does_not_eat_leading_title(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Title\n\ntext\n")
    run_lib('fix_glued_headings "$1"', str(report))
    assert report.read_text(encoding="utf-8") == "# Title\n\ntext\n"
