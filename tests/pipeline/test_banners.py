"""Degrade-path banner tests (D6 — honest degrade by default).

The UNVERIFIED banner is a deterministic, script-side guarantee that the
reader always sees when figures were never machine-verified — regardless of
what the model actually did.
"""

from __future__ import annotations

from tests.pipeline.helpers import run_lib

BANNER = "> **UNVERIFIED**"


def test_mark_numbers_unverified_creates_banner_from_nothing(tmp_path):
    """The real degraded case: numbers.md never written -> banner-only file."""
    numbers = tmp_path / "numbers.md"
    proc = run_lib('mark_numbers_unverified "$1"', str(numbers))
    assert proc.returncode == 0, proc.stderr
    assert numbers.read_text(encoding="utf-8").startswith(BANNER)
    assert not (tmp_path / "numbers.md.tmp").exists()  # atomic, no orphan


def test_mark_numbers_unverified_preserves_partial_content(tmp_path):
    numbers = tmp_path / "numbers.md"
    numbers.write_text("| metric | value |\n")
    run_lib('mark_numbers_unverified "$1"', str(numbers))
    text = numbers.read_text(encoding="utf-8")
    assert text.startswith(BANNER)
    assert "| metric | value |" in text


def test_apply_unverified_banner_prepends_when_numbers_degraded(tmp_path):
    numbers = tmp_path / "numbers.md"
    numbers.write_text(f"{BANNER} — x\n")
    report = tmp_path / "report.md"
    report.write_text("# Title\n\n## Introduction\n")

    proc = run_lib('apply_unverified_banner "$1" "$2"', str(report), str(numbers))
    assert proc.returncode == 0, proc.stderr
    text = report.read_text(encoding="utf-8")
    assert text.startswith(BANNER)
    assert text.count(BANNER) == 1  # exactly once


def test_apply_unverified_banner_skips_when_report_already_bannered(tmp_path):
    numbers = tmp_path / "numbers.md"
    numbers.write_text(f"{BANNER} — x\n")
    report = tmp_path / "report.md"
    report.write_text(f"{BANNER} — y\n\n# Title\n")

    run_lib('apply_unverified_banner "$1" "$2"', str(report), str(numbers))
    text = report.read_text(encoding="utf-8")
    assert text.startswith(f"{BANNER} — y")  # untouched
    assert text.count(BANNER) == 1


def test_apply_unverified_banner_skips_when_numbers_verified(tmp_path):
    numbers = tmp_path / "numbers.md"
    numbers.write_text("| metric | value |\n")
    report = tmp_path / "report.md"
    report.write_text("# Title\n")

    run_lib('apply_unverified_banner "$1" "$2"', str(report), str(numbers))
    assert report.read_text(encoding="utf-8") == "# Title\n"
