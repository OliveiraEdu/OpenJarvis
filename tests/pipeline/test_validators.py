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
