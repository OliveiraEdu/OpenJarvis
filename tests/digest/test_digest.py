"""D8/D9 — daily digest regression tests.

Covers: prompt contract (single-line, known placeholders), the strict
per-run HOOK/NOVELTY/SPEC/SOURCE contract parser, both fidelity gates
(figures ⊆ numbers.md ∪ report.md, URLs ⊆ report.md), deterministic clean
classification, the ≤500-char budgeted social assembly (any completed run
with a passing digest; UNVERIFIED/PARTIAL marked in the footer), the
newsletter (novelty-first sections with verbatim excerpts + inline caveats +
"Also flagged" + a machine-verified figures appendix), the retry/gate/
feedback loop with an injected fake ask (including the re-ask cooldown:
figure-free artifacts make a figure-gate failure provably futile, so the
run fails after one attempt while figure-less digests can still pass on
attempt 1 and sources failures still retry), idempotent re-runs, empty
days, and the launcher through the bash seam. No model, no stack — the ask
seam is a scripted fake that writes a fixture digest file, exactly like
tests/pipeline does.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from digest import (
    DIGEST_PROMPTS_DIR,
    HOOK_MAX,
    NOVELTY_MAX,
    NO_FIGURES_WHY,
    SOCIAL_MAX,
    _figure_tokens,
    _fit,
    build_newsletter,
    build_social,
    figure_fidelity,
    groundable_figures,
    inspect_run,
    local_trigger_date,
    main,
    parse_digest,
    render_digest_prompt,
    run,
    sources_fidelity,
    validate_digest_file,
)

from tests.digest.helpers import (
    DAY_SLUGS,
    FakeDigestAsk,
    NUMBERS,
    day_rows,
    fixture_report,
    load_payload,
    make_signals_db,
    make_trace_state,
    scoped_keyword,
    setup_day,
    trace_feedback,
    valid_digest,
    write_run,
)

DATE = "2026-08-08"


# ── C2: prompt contract ──────────────────────────────────────────────────────


def test_digest_prompt_renders_single_line_with_only_known_placeholders():
    raw = (DIGEST_PROMPTS_DIR / "digest_per_run.txt").read_text(encoding="utf-8")
    template_vars = {
        "SLUG": "ollama-scope-ai",
        "TOPIC": "ollama",
        "REPORT": "/workspace/ollama-scope-ai/report.md",
        "NUMBERS": "/workspace/ollama-scope-ai/numbers.md",
        "DIGEST_FILE": "/workspace/digests/2026-08-08/ollama-scope-ai.digest.md",
    }
    assert "WRITE THE DAILY DIGEST ENTRY" in raw
    rendered = render_digest_prompt("digest_per_run", template_vars)
    # make jarvis-exec splits recipes on newlines -> a rendered prompt with an
    # internal newline breaks the live ask (same regression as the pipeline).
    assert "\n" not in rendered
    assert "WRITE THE DAILY DIGEST ENTRY FOR ollama-scope-ai" in rendered


# ── contract parser + fidelity gates (pure, deterministic) ──────────────────


def test_parse_digest_accepts_valid_contract():
    parsed = parse_digest(valid_digest("ollama-scope-ai"))
    assert parsed is not None
    assert parsed["hook"].endswith("23.69% CAGR to 2032.")
    assert parsed["novelty"] == [
        "ollama-scope-ai keeps climbing as local-first inference takes off."
    ]
    assert parsed["spec"] == ["The 23.69% CAGR comes verbatim from numbers.md."]
    assert parsed["sources"] == ["https://example.com/ollama-scope-ai/one"]


def test_parse_digest_rejects_contract_violations():
    good = valid_digest("s")
    over_hook = "HOOK: " + "x" * (HOOK_MAX + 1) + "\n" + good.split("\n", 1)[1]
    over_novelty = "HOOK: h\n" + good.split("\n", 1)[1].replace(
        "NOVELTY: s keeps", "NOVELTY: " + "x" * (NOVELTY_MAX + 1)
    )
    cases = {
        "missing hook": good.split("\n", 1)[1],
        "two hooks": "HOOK: a\n" + good,
        "no novelty": good.replace("NOVELTY: ", "SPEC: "),
        "no sources": good.replace("SOURCE: ", "SPEC: "),
        "hook over budget": over_hook,
        "novelty over budget": over_novelty,
        "too many novelty lines": good + "NOVELTY: extra one\nNOVELTY: extra two\n",
        "too many spec lines": good + "SPEC: extra\nSPEC: extra\nSPEC: extra\n",
        "unknown line": good + "EXTRA: nope\n",
        "bare url without source prefix": good.replace("SOURCE: ", ""),
        "non-url source": good.replace("https://example.com/s/one", "not-a-url"),
    }
    for name, text in cases.items():
        assert parse_digest(text) is None, name


def test_figure_tokens_skip_years_and_integers():
    assert _figure_tokens("23.69% CAGR from 2023 to 2032, size 5.45") == [
        "23.69%",
        "5.45",
    ]
    # comma-grouped integers are real spec figures (context windows, params),
    # not years — they must be grounded too
    assert _figure_tokens("a 131,072-token window and 2.6B params") == [
        "131,072",
        "2.6",
    ]


def test_figure_fidelity_grounds_in_numbers_or_report():
    parsed = parse_digest(valid_digest("s"))
    report = fixture_report("s", "s")
    assert figure_fidelity(parsed, NUMBERS, report) is True
    bad = parse_digest(valid_digest("s").replace("23.69%", "99.99%"))
    assert bad is not None
    assert figure_fidelity(bad, NUMBERS, report) is False


def test_figure_fidelity_accepts_report_only_figures():
    # a spec figure that lives ONLY in report.md (not numbers.md) still passes
    # the report-grounding contract — figures need a home in either artifact.
    report = fixture_report("s", "s")  # carries "131,072-token"
    good = (
        "HOOK: On-device models keep compounding: 23.69% CAGR to 2032.\n"
        "NOVELTY: s keeps climbing as local-first inference takes off.\n"
        "SPEC: The model ships a 131,072-token context window.\n"
        "SOURCE: https://example.com/s/one\n"
    )
    assert figure_fidelity(parse_digest(good), NUMBERS, report) is True
    # an invented spec figure still fails
    bad = good.replace("131,072", "999,999")
    assert figure_fidelity(parse_digest(bad), NUMBERS, report) is False


def test_figure_fidelity_accepts_unit_annotations_on_verbatim_values():
    # numbers.md prints the computed result BARE (no %, no $) — the model may
    # add one unit annotation; the VALUE must still appear verbatim.
    numbers_text = "13.066727193702121 201.13571874999994 75.5"
    report = fixture_report("s", "s")
    good = (
        "HOOK: CAGR of 13.066727193702121% and a $201.13571874999994 projection.\n"
        "NOVELTY: s keeps climbing as local-first inference takes off.\n"
        "SPEC: Share reaches 75.5%.\n"
        "SOURCE: https://example.com/s/one\n"
    )
    assert figure_fidelity(parse_digest(good), numbers_text, report) is True
    # a ROUNDED value still fails: no verbatim core in numbers.md
    rounded = good.replace("13.066727193702121%", "13.07%")
    assert figure_fidelity(parse_digest(rounded), numbers_text, report) is False
    # an invented value still fails
    invented = good.replace("75.5", "99.9")
    assert figure_fidelity(parse_digest(invented), numbers_text, report) is False


def test_groundable_figures_detects_figure_free_artifacts():
    # placeholder numbers.md ("| placeholder metric | 0 |") + a figure-free
    # report -> NO groundable figures anywhere (bare '0' is excluded exactly
    # like any plain integer, so it can never make a run "groundable")
    assert groundable_figures(FIGURE_FREE_NUMBERS, FIGURE_FREE_REPORT) == []
    assert groundable_figures("", "no digits anywhere") == []
    # a single figure in numbers.md OR report.md makes the run groundable
    assert groundable_figures(NUMBERS, "text") != []
    assert groundable_figures("", "131,072-token context") == ["131,072"]


def test_sources_fidelity_requires_report_urls():
    parsed = parse_digest(valid_digest("s"))
    report = "## Sources & References\n\n1. One - https://example.com/s/one - d\n"
    assert sources_fidelity(parsed, report) is True
    invented = parse_digest(
        valid_digest("s").replace(
            "https://example.com/s/one", "https://fake.invalid/x/one"
        )
    )
    assert sources_fidelity(invented, report) is False


# ── run classification (deterministic clean-ness) ───────────────────────────


def _clean_state_for(workspace: Path, slug: str):
    write_run(workspace, slug, slug)
    row = {
        "source": "hf",
        "source_key": slug,
        "signal_status": "DONE",
        "slug": slug,
        "triggered_at": f"{DATE}T15:00:00+00:00",
    }
    return inspect_run(workspace, row)


def test_inspect_run_classifies_clean_vs_degraded(tmp_path):
    ws = tmp_path / "ws"
    clean = _clean_state_for(ws, "a-clean")
    assert clean["clean"] is True
    assert clean["clean_reasons"] == []

    cases = {
        "unverified": (dict(unverified=True), "UNVERIFIED figures banner"),
        "partial": (dict(partial=True), "PARTIAL report banner"),
        "no state": (dict(no_state=True), "no state.json (pre-state run?)"),
        "failed gate": (dict(fail_gate=True), "not all phase gates pass"),
        "no report": (dict(no_report=True), "no report.md"),
        "no numbers": (dict(no_numbers=True), "no numbers.md"),
    }
    for name, (kw, reason) in cases.items():
        slug = f"run-{name.replace(' ', '-')}"
        write_run(ws, slug, slug, **kw)
        row = {
            "source": "hf",
            "source_key": slug,
            "signal_status": "DONE",
            "slug": slug,
            "triggered_at": f"{DATE}T15:00:00+00:00",
        }
        info = inspect_run(ws, row)
        assert info["clean"] is False, name
        if reason:
            assert reason in info["clean_reasons"], name

    # a soft PROVENANCE NOTE does NOT make a run unclean (soft by design)
    write_run(ws, "run-provenance-soft", "x", provenance=True)
    row = {
        "source": "hf",
        "source_key": "run-provenance-soft",
        "signal_status": "DONE",
        "slug": "run-provenance-soft",
        "triggered_at": f"{DATE}T15:00:00+00:00",
    }
    info = inspect_run(ws, row)
    assert info["clean"] is True
    assert info["provenance_note"] is True

    # a FAILED signal with a partial artifact is not clean either
    write_run(ws, "failed-run", "failed-run", no_report=True)
    row = {
        "source": "hf",
        "source_key": "failed-run",
        "signal_status": "FAILED",
        "slug": "failed-run",
        "triggered_at": f"{DATE}T15:00:00+00:00",
    }
    info = inspect_run(ws, row)
    assert info["clean"] is False
    assert "signal status FAILED" in info["clean_reasons"]
    assert "no report.md" in info["clean_reasons"]


# ── date selection ───────────────────────────────────────────────────────────


def test_local_trigger_date_converts_utc_to_local():
    # 2026-08-08T15:00Z is Aug 8 in every timezone (UTC-12..UTC+14)
    assert local_trigger_date("2026-08-08T15:00:00+00:00") == "2026-08-08"
    assert local_trigger_date("2026-08-08T15:00:00Z") == "2026-08-08"
    assert local_trigger_date("2026-08-08T15:00:00") == "2026-08-08"  # naive -> UTC
    assert local_trigger_date("garbage") is None


def test_run_selects_only_the_target_local_date(tmp_path):
    rows = day_rows("2026-08-07", ("a-old",)) + day_rows(DATE, DAY_SLUGS)
    ws, state = setup_day(
        tmp_path,
        DATE,
        rows,
        {slug: {} for slug in DAY_SLUGS},
    )
    ask = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)] for slug in DAY_SLUGS})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    slugs = [r["slug"] for r in payload["runs"]]
    assert slugs == list(DAY_SLUGS)  # the Aug 7 row and manual dirs are excluded
    assert payload["date"] == DATE


# ── social: completed runs with a passing digest, hard budget ────────────────


def test_social_includes_completed_runs_and_marks_unverified(tmp_path):
    slugs = ("ollama-scope-ai", "comfy-org-minimax-h3-scope-ai")
    rows = day_rows(DATE, slugs) + day_rows(DATE, ("unverified-run",))
    ws, state = setup_day(
        tmp_path,
        DATE,
        rows,
        {
            slugs[0]: {},
            slugs[1]: {},
            "unverified-run": dict(unverified=True),
        },
    )
    scripts = {s: [valid_digest(s)] for s in (*slugs, "unverified-run")}
    payload = run(
        str(ws), str(state), DATE, "test-agent", ask=FakeDigestAsk(ws, DATE, scripts)
    )
    social = (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8")
    assert payload["social"]["written"] is True
    assert len(social) <= SOCIAL_MAX
    # any completed run with a passing digest is shareable — clean and flagged
    assert "ollama-scope-ai keeps climbing" in social
    assert "comfy-org-minimax-h3-scope-ai keeps climbing" in social
    assert "unverified-run keeps climbing" in social
    # hook (from the first shareable run) within budget + X/LinkedIn footer
    hook = social.split("\n")[0]
    assert len(hook) <= HOOK_MAX
    assert "AI-generated · verify before acting" in social
    assert "some figures unverified" in social  # the UNVERIFIED run is marked
    assert "newsletter.md" not in social  # no local filesystem path in the footer
    assert "machine-verified" not in social  # that phrasing belongs to the newsletter


def test_social_truncates_long_bullets_to_fit_budget(tmp_path):
    slugs = ("ollama-scope-ai", "comfy-org-minimax-h3-scope-ai", "third-run")

    def long_digest(slug: str) -> str:
        return (
            "HOOK: Small language models keep compounding: 23.69% CAGR to 2032.\n"
            "NOVELTY: " + ("word " * 32).strip() + "\n"
            "SPEC: The 23.69% CAGR comes verbatim from numbers.md.\n"
            f"SOURCE: https://example.com/{slug}/one\n"
        )

    ws, state = setup_day(tmp_path, DATE, day_rows(DATE, slugs), {s: {} for s in slugs})
    payload = run(
        str(ws),
        str(state),
        DATE,
        "test-agent",
        ask=FakeDigestAsk(ws, DATE, {s: [long_digest(s)] for s in slugs}),
    )
    social = (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8")
    assert payload["social"]["written"] is True
    assert len(social) <= SOCIAL_MAX
    assert "…" in social
    bullets = [l for l in social.split("\n") if l.startswith("• ")]
    assert len(bullets) == 3  # budget shrinks per-bullet room, not the roster
    assert all(b.endswith("…") for b in bullets)  # every long bullet truncated


def test_fit_truncates_at_word_boundary():
    assert _fit("short text", 20) == "short text"
    assert _fit("one two three four", 9) == "one two…"
    assert _fit("one two three", 3) == "on…"  # no space inside the window -> hard cut
    assert len(_fit("abcdefghij", 3)) <= 3


def test_social_omitted_when_no_digest_passes(tmp_path):
    # both runs have reports, but neither has a passing digest (no scripts) —
    # there is nothing shareable, so no social.md is written
    ws, state = setup_day(
        tmp_path,
        DATE,
        day_rows(DATE, ("unverified-run", "failed-run")),
        {"unverified-run": dict(unverified=True), "failed-run": dict(no_report=True)},
    )
    payload = run(
        str(ws),
        str(state),
        DATE,
        "test-agent",
        ask=FakeDigestAsk(ws, DATE, {}),
    )
    assert payload["social"]["written"] is False
    assert not (ws / "digests" / DATE / "social.md").exists()


# ── newsletter: novelty-first sections + inline caveats + appendix ───────────


def test_newsletter_excerpts_verbatim_and_flags_cleanly(tmp_path):
    rows = (
        day_rows(DATE, ("ollama-scope-ai",))
        + day_rows(DATE, ("unverified-run",))
        + day_rows(DATE, ("failed-run",), status="FAILED")
    )
    ws, state = setup_day(
        tmp_path,
        DATE,
        rows,
        {
            "ollama-scope-ai": {},
            "unverified-run": dict(unverified=True),
            "failed-run": dict(no_report=True),
        },
    )
    payload = run(
        str(ws),
        str(state),
        DATE,
        "test-agent",
        ask=FakeDigestAsk(
            ws, DATE, {"ollama-scope-ai": [valid_digest("ollama-scope-ai")]}
        ),
    )
    newsletter = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")
    assert payload["newsletter"]["written"] is True
    lines = newsletter.splitlines()
    # completed reports get a section (heading = topic from source_key, no
    # slug) with the verbatim exec summary, the digest's novelty lines, and
    # its sources
    assert "## ollama" in lines
    assert "## ollama (ollama-scope-ai)" not in lines  # slug moved out
    assert "ollama-scope-ai summary with a verified 23.69% CAGR." in newsletter
    assert "**What's notable**" in newsletter
    assert "https://example.com/ollama-scope-ai/one" in newsletter  # digest source
    # machine-verified numbers table lives in the appendix, not the section
    assert "## Appendix — machine-verified figures (verbatim from numbers.md)" in lines
    assert "| CAGR of SLM Market |" in newsletter
    assert "### ollama (ollama-scope-ai)" in lines
    # UNVERIFIED run flagged inline, FAILED run in "Also flagged", status honest
    assert "UNVERIFIED" in newsletter
    assert "**Caveats**" in newsletter
    assert "## Also flagged" in newsletter
    assert "**failed-run**" in newsletter
    assert "signal status FAILED" in newsletter
    # footer disclaimer
    assert "AI-generated daily digest" in newsletter


def test_newsletter_caveats_for_partial_no_state_and_provenance(tmp_path):
    rows = day_rows(
        DATE,
        ("partial-run", "prestate-run", "soft-run"),
    )
    ws, state = setup_day(
        tmp_path,
        DATE,
        rows,
        {
            "partial-run": dict(partial=True),
            "prestate-run": dict(no_state=True),
            "soft-run": dict(provenance=True),
        },
    )
    payload = run(
        str(ws), str(state), DATE, "test-agent", ask=FakeDigestAsk(ws, DATE, {})
    )
    newsletter = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")
    # headings are topic-based (source_key with '-' -> '/')
    lines = newsletter.splitlines()
    assert "## partial/run" in lines
    assert "PARTIAL report banner" in newsletter
    assert "## prestate/run" in lines
    assert "no state.json" in newsletter
    assert "## soft/run" in lines
    # soft provenance note is a caveat but NOT an unclean flag
    assert "provenance note" in newsletter.lower()
    # no global caveats section anymore — caveats are inline per section
    assert "## Caveats" not in newsletter


def test_newsletter_caveats_emit_each_flagged_run_once(tmp_path):
    rows = day_rows(
        DATE,
        ("flagged-prov-run", "prestate-run"),
    )
    ws, state = setup_day(
        tmp_path,
        DATE,
        rows,
        {
            # flagged (UNVERIFIED) AND carrying a soft provenance note — the
            # case that used to be emitted twice: once bare, once annotated
            "flagged-prov-run": dict(unverified=True, provenance=True),
            "prestate-run": dict(no_state=True),
        },
    )
    payload = run(
        str(ws), str(state), DATE, "test-agent", ask=FakeDigestAsk(ws, DATE, {})
    )
    newsletter = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")
    assert "## Caveats" not in newsletter
    # the flagged run's two caveats are each emitted exactly once (never bare
    # + annotated again), and "no state.json" shows once via the status line
    assert newsletter.count("model-stated only") == 1
    assert newsletter.count("soft provenance note") == 1
    assert newsletter.count("no state.json") == 1
    # one inline caveats list per flagged run section
    assert newsletter.count("**Caveats**") == 2


# ── the retry/gate/feedback loop (fake ask) ──────────────────────────────────


def test_digest_gate_retries_on_invented_figure_then_passes(tmp_path):
    ws, state = setup_day(
        tmp_path,
        DATE,
        day_rows(DATE, (DAY_SLUGS[0],)),
        {DAY_SLUGS[0]: {}},
        trace_state=True,
    )
    slug = DAY_SLUGS[0]
    # attempt 1: figure 99.99% is not verbatim in numbers.md -> numbers gate fails
    # attempt 2: the valid digest -> passes both gates
    bad = valid_digest(slug).replace("23.69%", "99.99%")
    ask = FakeDigestAsk(ws, DATE, {slug: [bad, valid_digest(slug)]})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    entry = next(r for r in payload["runs"] if r["slug"] == slug)
    assert entry["digest_gate"] == "pass"
    assert entry["digest_attempts"] == 2
    assert ask.calls[slug] == 2
    assert trace_feedback(state, scoped_keyword(slug)) == pytest.approx(
        0.7
    )  # 2nd attempt, no size bonus


def test_digest_gate_fails_after_max_attempts_and_excludes_from_social(tmp_path):
    slugs = (DAY_SLUGS[0], DAY_SLUGS[1])
    ws, state = setup_day(
        tmp_path, DATE, day_rows(DATE, slugs), {s: {} for s in slugs}, trace_state=True
    )
    good, bad = slugs[0], slugs[1]
    bad_url = valid_digest(bad).replace("https://", "ftp://")
    ask = FakeDigestAsk(
        ws,
        DATE,
        {good: [valid_digest(good)], bad: [bad_url]},
    )
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    good_entry = next(r for r in payload["runs"] if r["slug"] == good)
    bad_entry = next(r for r in payload["runs"] if r["slug"] == bad)
    assert good_entry["digest_gate"] == "pass"
    assert bad_entry["digest_gate"] == "fail"
    assert bad_entry["digest_attempts"] == 3
    # failed digest -> not in social, but the report still ships in the newsletter
    social = (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8")
    assert good in social and bad not in social
    newsletter = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")
    assert bad in newsletter
    # low feedback recorded for the failed digest (on ITS OWN slug-scoped trace)
    assert trace_feedback(state, scoped_keyword(bad)) == pytest.approx(0.2)
    # ...and the passing run's score is untouched on its own trace
    assert trace_feedback(state, scoped_keyword(good)) == pytest.approx(0.8)


# ── re-ask cooldown: figure-free artifacts make a figure-gate failure ────────
# ── provably futile (any figure-bearing digest can never pass grounding) ─────

FIGURE_FREE_REPORT = (
    "# Title\n\n"
    "## Executive Summary\n\n"
    "A summary with no figures at all.\n\n"
    "## Detailed Analysis\n\n"
    "Analysis without any numeric claims.\n\n"
    "## Sources & References\n\n"
    "1. One - https://example.com/fig-free/one\n"
)

FIGURE_FREE_NUMBERS = (
    "## Placeholder\n\n"
    "| Metric | Value |\n"
    "|--------|-------|\n"
    "| placeholder metric | 0 |\n"
)

FIGURE_BEARING_DIGEST = (
    "HOOK: The capability jumped 9.99% in a single release.\n"
    "NOVELTY: This is a brand-new design decision for the platform.\n"
    "SOURCE: https://example.com/fig-free/one\n"
)

FIGURE_FREE_DIGEST = (
    "HOOK: Placeholder discovery with no numeric claims to verify.\n"
    "NOVELTY: The run surfaced a capability note with no figures at all.\n"
    "SOURCE: https://example.com/fig-free/one\n"
)

FIGURE_FREE_BAD_SOURCE = FIGURE_FREE_DIGEST.replace(
    "https://example.com/fig-free/one", "https://example.com/not-in-report/one"
)


def _figure_free_run(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    """A clean run whose artifacts hold NO figure tokens at all (placeholder
    numbers.md, figure-free report) — the re-ask cooldown case."""
    ws, state = setup_day(
        tmp_path,
        DATE,
        day_rows(DATE, (slug,)),
        {slug: {"no_report": True, "no_numbers": True}},
        trace_state=True,
    )
    d = ws / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(FIGURE_FREE_REPORT, encoding="utf-8")
    (d / "numbers.md").write_text(FIGURE_FREE_NUMBERS, encoding="utf-8")
    return ws, state


def test_digest_gate_skips_reruns_when_artifacts_have_no_figures(tmp_path):
    # attempt 1 writes a figure-bearing digest -> figure gate fails; the
    # artifacts hold no figures at all, so re-asking is provably futile and
    # the run fails after this ONE attempt (not the full MAX_ATTEMPTS).
    slug = DAY_SLUGS[0]
    ws, state = _figure_free_run(tmp_path, slug)
    ask = FakeDigestAsk(ws, DATE, {slug: [FIGURE_BEARING_DIGEST]})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    entry = next(r for r in payload["runs"] if r["slug"] == slug)
    assert entry["digest_gate"] == "fail"
    assert entry["digest_attempts"] == 1
    assert entry["digest_why"] == NO_FIGURES_WHY
    assert ask.calls[slug] == 1  # no re-ask
    assert trace_feedback(state, scoped_keyword(slug)) == pytest.approx(0.2)
    # failed digest -> no social post at all; the report still ships in the
    # newsletter
    assert not (ws / "digests" / DATE / "social.md").is_file()
    newsletter = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")
    assert slug in newsletter


def test_digest_gate_passes_figure_less_digest_on_first_attempt(tmp_path):
    # a figure-less digest is the ONLY passable form for figure-free
    # artifacts — attempt 1 can legitimately succeed; the cooldown only skips
    # RETRIES after a figure-gate failure, it never skips the ask itself.
    slug = DAY_SLUGS[0]
    ws, state = _figure_free_run(tmp_path, slug)
    ask = FakeDigestAsk(ws, DATE, {slug: [FIGURE_FREE_DIGEST]})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    entry = next(r for r in payload["runs"] if r["slug"] == slug)
    assert entry["digest_gate"] == "pass"
    assert entry["digest_attempts"] == 1
    assert ask.calls[slug] == 1
    social = (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8")
    assert "capability note with no figures" in social


def test_digest_gate_still_retries_sources_failure_on_figure_free_artifacts(
    tmp_path,
):
    # sources failures are NOT cooldown-eligible: a URL can be fixed by
    # re-asking, so a figure-free run whose digest fails sources still gets
    # the full MAX_ATTEMPTS (retries are not provably futile here).
    slug = DAY_SLUGS[0]
    ws, state = _figure_free_run(tmp_path, slug)
    ask = FakeDigestAsk(ws, DATE, {slug: [FIGURE_FREE_BAD_SOURCE]})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    entry = next(r for r in payload["runs"] if r["slug"] == slug)
    assert entry["digest_gate"] == "fail"
    assert entry["digest_attempts"] == 3
    assert entry["digest_why"] == "a digest URL is not in report.md"
    assert ask.calls[slug] == 3


def test_rerun_reuses_passed_digest_without_engine_calls(tmp_path):
    ws, state = setup_day(
        tmp_path, DATE, day_rows(DATE, (DAY_SLUGS[0],)), {DAY_SLUGS[0]: {}}
    )
    slug = DAY_SLUGS[0]
    ask1 = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)]})
    first = run(str(ws), str(state), DATE, "test-agent", ask=ask1)
    assert ask1.calls[slug] == 1
    social1 = (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8")
    newsletter1 = (ws / "digests" / DATE / "newsletter.md").read_text(encoding="utf-8")

    class Boom:
        def __call__(self, *args, **kwargs):
            raise AssertionError("engine must not be called on a re-run")

    second = run(str(ws), str(state), DATE, "test-agent", ask=Boom())
    assert second["runs"][0]["digest_gate"] == "pass"
    assert second["runs"][0]["digest_attempts"] == 1  # preserved from state
    # deterministic assembly -> byte-identical outputs
    assert (ws / "digests" / DATE / "social.md").read_text(encoding="utf-8") == social1
    assert (ws / "digests" / DATE / "newsletter.md").read_text(
        encoding="utf-8"
    ) == newsletter1


def test_rerun_reasks_digest_that_no_longer_matches_contract(tmp_path):
    # a digest file written under an OLDER contract (BULLET/KEY_NUMBER) with a
    # passing state must not be silently reused: parsing it under the current
    # contract fails, so the run is re-asked instead of dropping from social.
    ws, state = setup_day(
        tmp_path, DATE, day_rows(DATE, (DAY_SLUGS[0],)), {DAY_SLUGS[0]: {}}
    )
    slug = DAY_SLUGS[0]
    ask1 = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)]})
    run(str(ws), str(state), DATE, "test-agent", ask=ask1)
    digest_file = ws / "digests" / DATE / f"{slug}.digest.md"
    digest_file.write_text(
        "HOOK: h\n"
        "KEY_NUMBER: K: 1.5\n"
        f"BULLET: {slug} keeps climbing.\n"
        f"SOURCE: https://example.com/{slug}/one\n",
        encoding="utf-8",
    )  # old-format body; the state still claims digest_gate == pass

    ask2 = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)]})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask2)
    assert ask2.calls[slug] == 1  # re-asked, not trusted from state
    assert payload["runs"][0]["digest_gate"] == "pass"
    assert "NOVELTY: " in digest_file.read_text(encoding="utf-8")  # rewritten
    assert (ws / "digests" / DATE / "social.md").is_file()  # run back in social


def test_force_reruns_the_engine(tmp_path):
    ws, state = setup_day(
        tmp_path, DATE, day_rows(DATE, (DAY_SLUGS[0],)), {DAY_SLUGS[0]: {}}
    )
    slug = DAY_SLUGS[0]
    ask1 = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)]})
    run(str(ws), str(state), DATE, "test-agent", ask=ask1)
    ask2 = FakeDigestAsk(ws, DATE, {slug: [valid_digest(slug)]})
    run(str(ws), str(state), DATE, "test-agent", ask=ask2, force=True)
    assert ask2.calls[slug] == 1  # re-asked despite the passing state


def test_no_engine_call_for_runs_without_report(tmp_path):
    ws, state = setup_day(
        tmp_path,
        DATE,
        day_rows(DATE, ("failed-run",)),
        {"failed-run": dict(no_report=True)},
    )
    ask = FakeDigestAsk(ws, DATE, {})
    payload = run(str(ws), str(state), DATE, "test-agent", ask=ask)
    assert payload["runs"][0]["digest_gate"] == "skipped"
    assert ask.calls == {}


# ── empty days + missing state ───────────────────────────────────────────────


def test_empty_day_writes_state_but_no_outputs(tmp_path):
    ws, state = setup_day(tmp_path, DATE, [], {})
    payload = run(
        str(ws), str(state), DATE, "test-agent", ask=FakeDigestAsk(ws, DATE, {})
    )
    assert payload["runs"] == []
    assert payload["social"]["written"] is False
    assert payload["newsletter"]["written"] is False
    assert not (ws / "digests" / DATE / "social.md").exists()
    assert not (ws / "digests" / DATE / "newsletter.md").exists()
    assert (ws / "digests" / DATE / "digest-state.json").is_file()


def test_missing_signals_db_degrades_to_empty_day(tmp_path):
    ws, state = setup_day(tmp_path, DATE, [], {})
    (state / "signals.db").unlink()
    payload = run(
        str(ws), str(state), DATE, "test-agent", ask=FakeDigestAsk(ws, DATE, {})
    )
    assert payload["runs"] == []
    assert payload["social"]["written"] is False


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_main_usage_errors(monkeypatch, capsys):
    assert main(["--nope"]) == 2
    assert main(["--date"]) == 2
    assert main(["--date", "not-a-date"]) == 2
    assert main(["-h"]) == 0
    assert "usage: digest.py" in capsys.readouterr().err


def test_main_runs_with_env(monkeypatch, tmp_path):
    # empty day: no signals -> no engine call, so main() stays stack-free
    ws, state = setup_day(tmp_path, DATE, [], {})
    monkeypatch.setenv("OJ_WORKSPACE_HOST", str(ws))
    monkeypatch.setenv("OJ_STATE_DIR", str(state))
    monkeypatch.setenv("OJ_AGENT_NAME", "test-agent")
    assert main(["--date", DATE]) == 0
    assert (ws / "digests" / DATE / "digest-state.json").is_file()


# ── launcher (bash seam) ─────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_launcher_produces_outputs_and_releases_lock(tmp_path):
    ws, state = setup_day(
        tmp_path, DATE, day_rows(DATE, (DAY_SLUGS[0],)), {DAY_SLUGS[0]: {}}
    )
    env = {
        **os.environ,
        "OJ_STATE_DIR": str(state),
        "OJ_WORKSPACE_HOST": str(ws),
        "OJ_AGENT_NAME": "test-agent",
        "OJ_SKIP_SANITY": "1",
    }
    # a real engine call would be needed; with OJ_SKIP_SANITY the launcher
    # still delegates, and the empty-day semantics keep it stack-free, so use
    # an empty signals.db to prove the wiring end-to-end.
    (state / "signals.db").unlink()
    proc = subprocess.run(
        [str(REPO_ROOT / "scripts" / "digest.sh"), "--date", DATE],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (state / "digest.lock.d").exists()  # lock released by the EXIT trap
    assert (ws / "digests" / DATE / "digest-state.json").is_file()


def test_launcher_defers_when_lock_held(tmp_path):
    ws, state = setup_day(tmp_path, DATE, [], {})
    lock = state / "digest.lock.d"
    lock.mkdir()
    env = {
        **os.environ,
        "OJ_STATE_DIR": str(state),
        "OJ_WORKSPACE_HOST": str(ws),
        "OJ_AGENT_NAME": "test-agent",
        "OJ_SKIP_SANITY": "1",
    }
    proc = subprocess.run(
        [str(REPO_ROOT / "scripts" / "digest.sh"), "--date", DATE],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "deferring" in proc.stdout
    assert lock.exists()  # the holder keeps it; we did not stomp it
