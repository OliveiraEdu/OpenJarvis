"""Provenance check tests (D2/D6 — honest degrade: fabricated URLs are
flagged and made visible, never silently shipped as citations).

All tests mutate a temp copy, never the committed fixtures.
"""

from __future__ import annotations

import re

from tests.pipeline.helpers import HPC, run_lib

FINDINGS = HPC / "findings.md"
KNOWN_URL = "https://www.mordorintelligence.com/industry-reports/high-performance-computing-market"


def test_hpc_report_urls_all_unmatched_append_note(tmp_path):
    """The real HPC report's 5 URLs are all fabricated (none in findings):
    the check must report 5/5 and append the PROVENANCE NOTE exactly once."""
    committed = (HPC / "report.md").read_text(encoding="utf-8")
    report = tmp_path / "report.md"
    # the committed fixture already carries the note from the live run — strip
    # it from the tmp copy so we test the append behavior from a clean state
    report.write_text(
        re.sub(r"\n*> \*\*PROVENANCE NOTE\*\*.*$", "", committed, flags=re.S),
        encoding="utf-8",
    )
    assert "PROVENANCE NOTE" not in report.read_text(encoding="utf-8")

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "5/5 report URL(s) not found" in proc.stdout
    text = report.read_text(encoding="utf-8")
    assert text.count("PROVENANCE NOTE") == 1  # exactly one note, at the end
    assert text.rstrip().endswith("verify every URL before citing.")

    # committed fixture is left exactly as exported (mutation only on tmp copy)
    assert (HPC / "report.md").read_text(encoding="utf-8") == committed


def test_provenance_accepts_url_present_in_findings(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(f"# R\n\n## Sources\n\n{KNOWN_URL}\n")

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "0/1 report URL(s) not found" in proc.stdout
    assert "PROVENANCE NOTE" not in report.read_text(encoding="utf-8")


def test_provenance_no_urls_is_soft(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# R\n\nno links here\n")

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "no URLs found" in proc.stdout
    assert "PROVENANCE NOTE" not in report.read_text(encoding="utf-8")


def test_provenance_mixed_urls_counts_correctly(tmp_path):
    """One known + one fabricated URL -> 1/2 unmatched, note still appended."""
    report = tmp_path / "report.md"
    report.write_text(
        f"# R\n\n## Sources\n\n{KNOWN_URL}\nhttps://example.com/fake-report-123\n"
    )

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "1/2 report URL(s) not found" in proc.stdout
    assert "PROVENANCE NOTE" in report.read_text(encoding="utf-8")


def test_provenance_resolves_trailing_slash_and_fragment(tmp_path):
    """URL resolution is component-based (D3): a trailing slash or a fragment
    must not make a legitimate citation look fabricated."""
    report = tmp_path / "report.md"
    report.write_text(f"# R\n\n## Sources\n\n{KNOWN_URL}/\n{KNOWN_URL}#overview\n")

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "0/2 report URL(s) not found" in proc.stdout
    assert "PROVENANCE NOTE" not in report.read_text(encoding="utf-8")


def test_provenance_resolves_without_www(tmp_path):
    """A bare host resolves against the findings' www. variant."""
    report = tmp_path / "report.md"
    bare = KNOWN_URL.replace("https://www.", "https://")
    report.write_text(f"# R\n\n## Sources\n\n{bare}\n")

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "0/1 report URL(s) not found" in proc.stdout
    assert "PROVENANCE NOTE" not in report.read_text(encoding="utf-8")


def test_provenance_same_host_different_path_still_unmatched(tmp_path):
    """Normalization must not over-match: same host, different path is the
    fabricated-URL failure mode in miniature and must stay flagged."""
    report = tmp_path / "report.md"
    report.write_text(
        "# R\n\n## Sources\n\n"
        "https://www.mordorintelligence.com/industry-reports/something-else\n"
    )

    proc = run_lib('check_sources_provenance "$1" "$2"', str(report), str(FINDINGS))
    assert proc.returncode == 0, proc.stderr
    assert "1/1 report URL(s) not found" in proc.stdout
    assert "PROVENANCE NOTE" in report.read_text(encoding="utf-8")
