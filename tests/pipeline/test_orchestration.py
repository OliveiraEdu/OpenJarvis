"""C1 — typed phase orchestration tests.

The retry/gate/feedback loop and the typed phase specs now live in
scripts/research_phases.py (research.sh is a thin launcher). These tests
exercise the loop with an injected fake ``ask`` (no model, no stack) against
the SAME bash deterministic leaves — validators, the normalize hook, the
tool-usage gate, and feedback scoring — in scripts/research_lib.sh that
production runs (C4/C5: the offline harness tests that seam, not a Python
reimplementation).

Real trace-derived fixtures (HPC/ARM) are used as fake model output, so a
passing phase here means the identical bytes production would validate.
"""

from __future__ import annotations

import re
import sqlite3
import string
from pathlib import Path

import pytest

from research_phases import (
    Ctx,
    LIB_SH,
    MAX_ATTEMPTS,
    PHASES,
    PROMPTS_DIR,
    main,
    prompt_vars,
    render_prompt,
    reset_summary_memory,
    resolve_agent_uuid,
    run_phase,
    write_feedback,
)
from tests.pipeline.helpers import ARM, FIXTURES, HPC

REQUIRED_ENV = (
    "OJ_TOPIC",
    "OJ_SLUG",
    "OJ_WORKSPACE_HOST",
    "OJ_STATE_DIR",
    "OJ_AGENT_NAME",
)

# Asklog bodies — the tool-usage gate counts `  ↳ <tool>` lines (C4).
ASKLOG_GATHER = "  ↳ web_search query=a\n  ↳ web_search query=b\n"
ASKLOG_VERIFY = "  ↳ calculator expression=1+1\n"
ASKLOG_WRITE = "  ↳ file_write path=/x\n  ↳ file_write path=/y\n"

# A numbers table that check_numbers_table accepts (rows from the validator
# tests), padded past the 200-byte artifact floor.
VALID_NUMBERS = (
    "| metric | formula | result |\n"
    "| --- | --- | --- |\n"
    "| CAGR | ((87.5/60.12)^(1/5)-1)*100 | 7.79 |\n"
    "| CAGR | (60.12/55.78*100-100 = 7.78%) | 7.78 |\n"
    "| Market | (87.5/60.12) | 1.46 |\n"
    "| Market | (55.78+60.12) | 115.9 |\n"
)

# The real degraded VERIFY artifact: banner only, zero rows -> validator fails.
DEGRADED_NUMBERS = (HPC / "numbers.md").read_text(encoding="utf-8")


def make_ctx(tmp_path: Path, *, min_size: int = 200) -> Ctx:
    ws = tmp_path / "ws"
    (ws / "s").mkdir(parents=True, exist_ok=True)
    return Ctx(
        root=tmp_path,
        state_dir=str(tmp_path / "state"),
        agent_name="test-agent",
        workspace_host=str(ws),
        slug="s",
        topic="Test topic",
        min_artifact_size=min_size,
    )


def artifact_path(ctx: Ctx, spec) -> Path:
    return Path(ctx.workspace_host) / ctx.slug / spec.artifact


def make_state(state_dir: Path, *, keyword: str, uuid: str = "u-1") -> str:
    """Create agents.db + traces.db with one agent and one matching trace."""
    state_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state_dir / "agents.db")
    con.execute(
        "create table managed_agents ("
        " id text primary key, name text, status text,"
        " last_run_at text, summary_memory text)"
    )
    con.execute(
        "insert into managed_agents values (?,?,?,?,?)",
        (uuid, "test-agent", "running", "2026-08-03T00:00:00", "stale note"),
    )
    con.commit()
    con.close()
    con = sqlite3.connect(state_dir / "traces.db")
    con.execute(
        "create table traces ("
        " trace_id text primary key, agent text, query text,"
        " started_at text, feedback real)"
    )
    con.execute(
        "insert into traces values (?,?,?,?,?)",
        (uuid, uuid, f"do the work {keyword} now", "2026-08-03T00:00:00", None),
    )
    con.commit()
    con.close()
    return uuid


def trace_feedback(state_dir: Path, trace_id: str) -> float | None:
    con = sqlite3.connect(state_dir / "traces.db")
    row = con.execute(
        "select feedback from traces where trace_id=?", (trace_id,)
    ).fetchone()
    con.close()
    return row[0] if row else None


class FakeAsk:
    """Scripted fake for the ask seam. Attempt i writes ``scripts[i]``; the
    last script repeats for any further attempts. ``content=None`` writes no
    artifact (a shortcutting model)."""

    def __init__(self, ctx: Ctx, spec, scripts: list[tuple[str | None, str]]):
        self.ctx = ctx
        self.spec = spec
        self.scripts = scripts
        self.calls: list[tuple[Path, str, str]] = []

    def __call__(self, root: Path, agent_name: str, prompt: str, asklog: str):
        self.calls.append((Path(asklog), agent_name, prompt))
        content, log = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        if content is not None:
            artifact_path(self.ctx, self.spec).write_text(content, encoding="utf-8")
        Path(asklog).write_text(log, encoding="utf-8")


# ── C2: versioned prompt templates ───────────────────────────────────────────


def test_prompt_templates_render_with_only_known_placeholders():
    vars_ = prompt_vars(make_ctx(Path("/tmp")))
    for f in PROMPTS_DIR.glob("*.txt"):
        template = string.Template(f.read_text(encoding="utf-8"))
        assert set(template.get_identifiers()) <= set(vars_), f"{f.name}"
        template.substitute(vars_)  # raises if a placeholder is missing


def test_render_prompt_substitutes_topic_and_container_paths(tmp_path):
    ctx = make_ctx(tmp_path)
    # the paths each template actually teaches (gather only writes findings;
    # the report phases reference findings + numbers + report)
    expected = {
        "gather": ["findings.md"],
        "verify": ["findings.md", "numbers.md"],
        "part1": ["findings.md", "numbers.md", "report.md"],
        "part2": ["numbers.md", "report.md"],  # appends to existing report
    }
    for name, spec in PHASES.items():
        text = render_prompt(spec.prompt, prompt_vars(ctx))
        assert "Test topic" in text
        for artifact in expected[name]:
            assert f"/workspace/{ctx.slug}/{artifact}" in text


def test_rendered_prompt_is_newline_free(tmp_path):
    """The launcher passes the prompt through ``make jarvis-exec CMD="…"`` and
    make splits recipes on newlines, so any newline in a rendered prompt breaks
    the shell quoting (live-run regression: 'Unterminated quoted string' from
    the template file's trailing newline). render_prompt must trim, and no
    template may introduce an internal newline."""
    ctx = make_ctx(tmp_path)
    for spec in PHASES.values():
        assert "\n" not in render_prompt(spec.prompt, prompt_vars(ctx))


# ── C1: the typed phase specs are self-consistent ────────────────────────────


def test_phase_specs_are_self_consistent():
    lib = LIB_SH.read_text(encoding="utf-8")
    wired = set()
    for name, spec in PHASES.items():
        wired.add(spec.prompt)
        assert spec.label and spec.artifact and spec.fb_keyword, f"{name}"
        prompt_file = PROMPTS_DIR / f"{spec.prompt}.txt"
        assert prompt_file.is_file(), f"{name}: prompt template missing"
        for fn in (spec.validator, spec.normalize):
            if fn:
                assert re.search(rf"^{fn}\(\)", lib, re.M), (
                    f"{name}: {fn} not a research_lib.sh function"
                )
        if spec.tool_req:
            assert re.fullmatch(r"\w+:\d+", spec.tool_req), f"{name}: tool_req"
    # every template file is wired to a phase (no orphans)
    assert wired == {f.stem for f in PROMPTS_DIR.glob("*.txt")}


def test_gather_and_verify_spec_contracts():
    gather, verify = PHASES["gather"], PHASES["verify"]
    assert gather.validator is None and gather.tool_req == "web_search:2"
    # gather repairs glued '### Fact N' headings like the report phases (D5)
    assert gather.normalize == "fix_glued_headings"
    assert verify.validator == "check_numbers_table"
    assert verify.tool_req == "calculator:1"
    assert verify.snapshot is None


def test_report_specs_use_normalize_and_part2_snapshot():
    part1, part2 = PHASES["part1"], PHASES["part2"]
    assert part1.validator == "check_report_part1"
    assert part1.normalize == "fix_glued_headings"
    assert part2.validator == "check_report_sections"
    assert part2.normalize == "fix_glued_headings"
    assert part2.snapshot == "report.part1"


# ── Ctx: dynamic context from the launcher env ───────────────────────────────


def test_ctx_from_env_requires_all_vars(monkeypatch):
    for var in REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="missing required env"):
        Ctx.from_env()


def test_ctx_from_env_reads_all_vars(monkeypatch):
    for var, value in zip(REQUIRED_ENV, ("topic", "slug", "/ws", "/state", "agent")):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("OJ_MIN_ARTIFACT_SIZE", raising=False)
    ctx = Ctx.from_env()
    assert (ctx.topic, ctx.slug, ctx.workspace_host) == ("topic", "slug", "/ws")
    assert ctx.state_dir == "/state"
    assert ctx.agent_name == "agent"
    assert ctx.min_artifact_size == 200  # default
    monkeypatch.setenv("OJ_MIN_ARTIFACT_SIZE", "500")
    assert Ctx.from_env().min_artifact_size == 500


# ── run_phase: the retry/gate/feedback loop ──────────────────────────────────


def test_run_phase_succeeds_first_attempt_and_cleans_asklog(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    spec = PHASES["gather"]
    ask = FakeAsk(ctx, spec, [("facts" * 200, ASKLOG_GATHER)])
    assert run_phase(spec, ctx, ask=ask) is True
    assert artifact_path(ctx, spec).read_text(encoding="utf-8") == "facts" * 200
    assert len(ask.calls) == 1
    # asklog is a temp file the loop always removes
    assert not ask.calls[0][0].exists()
    # prompt passed to the model is the RENDERED template (C2)
    assert "Test topic" in ask.calls[0][2]
    assert f"/workspace/{ctx.slug}/findings.md" in ask.calls[0][2]
    # feedback skipped gracefully: no agents.db in the tmp state dir
    out = capsys.readouterr().out
    assert "agent uuid not found, feedback skipped" in out


def test_run_phase_retries_after_validator_failure_then_succeeds(tmp_path):
    ctx = make_ctx(tmp_path)
    state = Path(ctx.state_dir)
    make_state(state, keyword="VERIFY THE NUMBERS")
    spec = PHASES["verify"]
    # attempt 1: real degraded artifact (banner only) -> validator fails
    # attempt 2: valid numbers table -> passes size + validator + tool gate
    ask = FakeAsk(
        ctx, spec, [(DEGRADED_NUMBERS, ASKLOG_VERIFY), (VALID_NUMBERS, ASKLOG_VERIFY)]
    )
    assert run_phase(spec, ctx, ask=ask) is True
    assert len(ask.calls) == 2
    # attempt 2 + small artifact -> 0.6 + 0.1 (attempt bonus) + 0 size bonus
    assert trace_feedback(state, "u-1") == pytest.approx(0.7)


def test_run_phase_writes_success_feedback_first_attempt(tmp_path):
    ctx = make_ctx(tmp_path)
    state = Path(ctx.state_dir)
    make_state(state, keyword="WRITE PART 2 OF THE FINAL REPORT")
    spec = PHASES["part2"]
    report = (HPC / "report.md").read_text(encoding="utf-8")
    ask = FakeAsk(ctx, spec, [(report, ASKLOG_WRITE)])
    assert run_phase(spec, ctx, ask=ask) is True
    # HPC report: 5418 bytes, first attempt -> 0.6 + 0.2 + 0.2 = 1.0
    assert trace_feedback(state, "u-1") == 1.0
    # the stale summary_memory note was reset before the attempt
    con = sqlite3.connect(state / "agents.db")
    row = con.execute(
        "select summary_memory from managed_agents where id='u-1'"
    ).fetchone()
    con.close()
    assert row[0] == ""


def test_run_phase_fails_after_max_attempts_and_records_low_score(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    state = Path(ctx.state_dir)
    make_state(state, keyword="VERIFY THE NUMBERS")
    spec = PHASES["verify"]
    # the real degraded artifact on every attempt: validator never passes
    ask = FakeAsk(ctx, spec, [(DEGRADED_NUMBERS, ASKLOG_VERIFY)])
    assert run_phase(spec, ctx, ask=ask) is False
    assert len(ask.calls) == MAX_ATTEMPTS == 3
    # 184-byte degraded artifact, failed -> min(0.3, 0.2) = 0.2
    assert trace_feedback(state, "u-1") == pytest.approx(0.2)
    err = capsys.readouterr().err
    assert "did not produce a valid" in err


def test_run_phase_missing_artifact_never_written_retries(tmp_path):
    ctx = make_ctx(tmp_path)
    spec = PHASES["gather"]
    # the model short-circuits: two empty turns (no artifact, tool gate unmet)
    ask = FakeAsk(ctx, spec, [(None, ""), ("facts" * 200, ASKLOG_GATHER)])
    assert run_phase(spec, ctx, ask=ask) is True
    assert len(ask.calls) == 2


def test_run_phase_tool_gate_forces_retry(tmp_path):
    ctx = make_ctx(tmp_path)
    spec = PHASES["gather"]
    # artifact fine on attempt 1 but only one web_search call (gate needs 2)
    ask = FakeAsk(
        ctx,
        spec,
        [("facts" * 200, "  ↳ web_search query=a\n"), ("facts" * 200, ASKLOG_GATHER)],
    )
    assert run_phase(spec, ctx, ask=ask) is True
    assert len(ask.calls) == 2


def test_run_phase_normalize_hook_repairs_glued_headings(tmp_path):
    ctx = make_ctx(tmp_path)
    spec = PHASES["part2"]
    # the pre-fix ARM report has two headings glued to paragraph text; the
    # normalize hook (fix_glued_headings) repairs it before validation, so a
    # content-complete report passes the sections gate on attempt 1.
    glued = (ARM / "report.md").read_text(encoding="utf-8")
    ask = FakeAsk(ctx, spec, [(glued, ASKLOG_WRITE)])
    assert run_phase(spec, ctx, ask=ask) is True
    assert len(ask.calls) == 1
    assert artifact_path(ctx, spec).read_text(encoding="utf-8") != glued


def test_gather_normalize_repairs_glued_findings(tmp_path):
    """The edgeai live run (2026-08-03) glued '### Fact N' headings to the
    previous URL line in findings.md. Gather now repairs them with the same
    idempotent hook as the report phases (D5 — structure from code), using
    the real degraded fixture as the fake model output."""
    ctx = make_ctx(tmp_path)
    spec = PHASES["gather"]
    glued = (FIXTURES / "artifacts" / "edgeai" / "findings.md").read_text(
        encoding="utf-8"
    )
    assert "market### Fact 3" in glued  # fixture really is glued
    ask = FakeAsk(ctx, spec, [(glued, ASKLOG_GATHER)])
    assert run_phase(spec, ctx, ask=ask) is True
    fixed = artifact_path(ctx, spec).read_text(encoding="utf-8")
    assert "\n\n### Fact 3" in fixed
    assert "\n\n### Fact 4" in fixed
    assert fixed != glued  # repair changed something (idempotent hook)


def test_run_phase_restores_snapshot_before_each_attempt(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    spec = PHASES["part2"]
    artifact = artifact_path(ctx, spec)
    snapshot = Path(ctx.workspace_host) / ctx.slug / "report.part1"
    snapshot.write_text((ARM / "report.part1").read_text(encoding="utf-8"))
    # attempt 1: missing Confidence Assessment -> validator fails
    # attempt 2: complete report -> passes
    bad = "## Introduction\n\n## Executive Summary\n\n## Detailed Analysis\n\n## Conclusions\n\n## Sources\n"
    good = (HPC / "report.md").read_text(encoding="utf-8")
    ask = FakeAsk(ctx, spec, [(bad, ASKLOG_WRITE), (good, ASKLOG_WRITE)])
    assert run_phase(spec, ctx, ask=ask) is True
    assert len(ask.calls) == 2
    out = capsys.readouterr().out
    assert out.count("restored artifact from snapshot") == 2  # before every attempt


# ── feedback persistence: best-effort, never aborts the pipeline ─────────────


def test_feedback_skipped_when_no_trace_matches(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    state = Path(ctx.state_dir)
    make_state(state, keyword="WRITE PART 2 OF THE FINAL REPORT")
    spec = PHASES["part1"]
    part1 = (ARM / "report.part1").read_text(encoding="utf-8")
    ask = FakeAsk(ctx, spec, [(part1, ASKLOG_WRITE)])
    # the phase's keyword ("WRITE PART 1 ...") does not match the only trace
    # (which carries the part-2 keyword) -> feedback skipped, phase still OK
    assert run_phase(spec, ctx, ask=ask) is True
    assert trace_feedback(state, "u-1") is None
    out = capsys.readouterr().out
    assert "no trace matched keyword" in out


def test_reset_summary_memory_clears_stale_note(tmp_path):
    state = Path(tmp_path) / "state"
    make_state(state, keyword="GATHER FACTS")
    reset_summary_memory(str(state), "test-agent")
    con = sqlite3.connect(state / "agents.db")
    row = con.execute(
        "select summary_memory from managed_agents where id='u-1'"
    ).fetchone()
    con.close()
    assert row[0] == ""


def test_reset_summary_memory_best_effort_without_agents_db(tmp_path):
    # no agents.db at all -> must not raise (pipeline continues)
    reset_summary_memory(str(tmp_path / "nope"), "test-agent")


def test_resolve_agent_uuid_picks_latest_non_archived(tmp_path):
    state = Path(tmp_path) / "state"
    state.mkdir()
    con = sqlite3.connect(state / "agents.db")
    con.execute(
        "create table managed_agents ("
        " id text primary key, name text, status text,"
        " last_run_at text, summary_memory text)"
    )
    for row in (
        ("old", "test-agent", "running", "2026-01-01", ""),
        ("latest", "test-agent", "running", "2026-08-03", ""),
        ("archived", "test-agent", "archived", "2026-12-31", ""),
    ):
        con.execute("insert into managed_agents values (?,?,?,?,?)", row)
    con.commit()
    con.close()
    assert resolve_agent_uuid(str(state), "test-agent") == "latest"
    assert resolve_agent_uuid(str(state), "missing-agent") is None
    assert resolve_agent_uuid(str(state / "nonexistent"), "test-agent") is None


def test_write_feedback_best_effort_without_traces_db(tmp_path, capsys):
    state = Path(tmp_path) / "state"
    state.mkdir()
    # only agents.db exists; traces.db missing -> caught, pipeline continues
    write_feedback(
        "phase 3b", "WRITE PART 2 OF THE FINAL REPORT", 1.0, "u-1", str(state)
    )
    out = capsys.readouterr().out
    assert "feedback write failed" in out


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_main_usage_and_unknown_phase(monkeypatch, capsys):
    assert main([]) == 2
    assert main(["run"]) == 2
    assert main(["run", "--phase", "nope"]) == 2
    err = capsys.readouterr().err
    assert "unknown phase" in err


def test_main_missing_env_exits_2(monkeypatch, capsys):
    for var in REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    assert main(["run", "--phase", "gather"]) == 2
    assert "missing required env" in capsys.readouterr().err
