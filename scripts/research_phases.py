"""Typed phase orchestration for the research pipeline (C1, C2).

The phase *specs* (label, prompt template, artifact, validator, tool gate,
snapshot, feedback keyword, normalize hook) and the retry/gate/feedback loop
live here as typed Python. ``scripts/research.sh`` is a thin launcher: it sets
the dynamic context (topic, slug, workspace, state dir, agent) and delegates
via ``run --phase <name>``, then applies the phase-specific degrade branches.

The deterministic leaves the loop calls — validators, the normalize hook, the
tool-usage counter, and feedback scoring — are still the SAME bash functions
in ``scripts/research_lib.sh`` (C4/C5: the offline harness tests that seam),
invoked here through ``bash -c`` exactly like ``tests/pipeline/helpers.py``
does. Nothing else from this module runs the model; the ``ask`` step is
injectable so the whole loop is regression-testable offline with zero stack.

Stdlib-only on purpose: the live pipeline invokes this under the host
``python3``, which cannot import ``openjarvis``.

Usage (from the launcher):
    OJ_TOPIC=... OJ_SLUG=... OJ_WORKSPACE_HOST=... OJ_STATE_DIR=... \\
    OJ_AGENT_NAME=... [OJ_MIN_ARTIFACT_SIZE=...] \\
    python3 scripts/research_phases.py run --phase <gather|verify|part1|part2>
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPTS_DIR / "prompts"
LIB_SH = SCRIPTS_DIR / "research_lib.sh"
MAX_ATTEMPTS = 3

# ── typed phase spec ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhaseSpec:
    """Everything a phase needs, as a typed object (C1).

    ``validator`` / ``normalize`` name bash functions in research_lib.sh;
    ``prompt`` names a template file in scripts/prompts/ (C2); ``snapshot`` is
    an artifact filename within the workspace slug dir, restored before every
    attempt (used by append-phases so a failed attempt's appends cannot
    pollute the next attempt).
    """

    label: str
    prompt: str
    artifact: str
    validator: str | None
    tool_req: str | None
    snapshot: str | None
    fb_keyword: str | None
    normalize: str | None


PHASES: dict[str, PhaseSpec] = {
    "gather": PhaseSpec(
        label="phase 1 (gather)",
        prompt="phase1_gather",
        artifact="findings.md",
        validator=None,  # artifact existence + size is the gate
        tool_req="web_search:2",
        snapshot=None,
        fb_keyword="GATHER FACTS",
        normalize=None,
    ),
    "verify": PhaseSpec(
        label="phase 2 (verify)",
        prompt="phase2_verify",
        artifact="numbers.md",
        validator="check_numbers_table",
        tool_req="calculator:1",
        snapshot=None,
        fb_keyword="VERIFY THE NUMBERS",
        normalize=None,
    ),
    "part1": PhaseSpec(
        label="phase 3a (report part 1)",
        prompt="phase3a_report_part1",
        artifact="report.md",
        validator="check_report_part1",
        tool_req="file_write:2",
        snapshot=None,
        fb_keyword="WRITE PART 1 OF THE FINAL REPORT",
        normalize="fix_glued_headings",
    ),
    "part2": PhaseSpec(
        label="phase 3b (report part 2)",
        prompt="phase3b_report_part2",
        artifact="report.md",
        validator="check_report_sections",
        tool_req="file_write:2",
        snapshot="report.part1",
        fb_keyword="WRITE PART 2 OF THE FINAL REPORT",
        normalize="fix_glued_headings",
    ),
}


@dataclass(frozen=True)
class Ctx:
    """Dynamic context the launcher injects via environment (paths, topic)."""

    root: Path
    state_dir: str
    agent_name: str
    workspace_host: str
    slug: str
    topic: str
    min_artifact_size: int = 200

    @classmethod
    def from_env(cls) -> "Ctx":
        required = {
            "OJ_TOPIC",
            "OJ_SLUG",
            "OJ_WORKSPACE_HOST",
            "OJ_STATE_DIR",
            "OJ_AGENT_NAME",
        }
        missing = required - set(os.environ)
        if missing:
            raise RuntimeError(f"missing required env: {', '.join(sorted(missing))}")
        return cls(
            root=SCRIPTS_DIR.parent,
            state_dir=os.environ["OJ_STATE_DIR"],
            agent_name=os.environ["OJ_AGENT_NAME"],
            workspace_host=os.environ["OJ_WORKSPACE_HOST"],
            slug=os.environ["OJ_SLUG"],
            topic=os.environ["OJ_TOPIC"],
            min_artifact_size=int(os.environ.get("OJ_MIN_ARTIFACT_SIZE", "200")),
        )


def render_prompt(name: str, vars_: dict[str, str]) -> str:
    """Render a versioned prompt template (C2). Raises on a missing variable
    or a stray ``$``, so a template edit that breaks rendering fails loudly.

    The template file's trailing newline is trimmed: the launcher passes the
    prompt through ``make jarvis-exec CMD="... "`` and make splits recipes on
    newlines, so a prompt containing one would break the shell quoting
    ('Unterminated quoted string'). See the regression test in
    tests/pipeline/test_orchestration.py.
    """
    raw = (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return string.Template(raw).substitute(vars_).strip()


def prompt_vars(ctx: Ctx) -> dict[str, str]:
    """Container-side paths the prompts teach the model, plus the topic."""
    return {
        "TOPIC": ctx.topic,
        "FINDINGS": f"/workspace/{ctx.slug}/findings.md",
        "NUMBERS": f"/workspace/{ctx.slug}/numbers.md",
        "REPORT": f"/workspace/{ctx.slug}/report.md",
    }


# ── deterministic seams into research_lib.sh (C4/C5) ─────────────────────────


def _bash_fn(name: str, *args: str) -> subprocess.CompletedProcess:
    """Run one research_lib.sh function through the same ``bash -c`` seam the
    offline harness uses — production and tests execute the identical code."""
    script = f'set -euo pipefail\nsource {LIB_SH}\n{name} "$@"'
    return subprocess.run(
        ["bash", "-c", script, f"oj-{name}", *args],
        capture_output=True,
        text=True,
    )


def bash_validator(name: str, artifact: str) -> bool:
    return _bash_fn(name, artifact).returncode == 0


def bash_normalize(name: str, artifact: str) -> None:
    _bash_fn(name, artifact)


def bash_tool_count(asklog: str, tool: str) -> int:
    proc = _bash_fn("count_tool_calls", asklog, tool)
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return 0


def bash_feedback_score(attempts: int, size: int, passed: str) -> float:
    proc = _bash_fn("feedback_score", str(attempts), str(size), passed)
    return float(proc.stdout.strip())


# ── feedback persistence (Phase B of the TDL loop) ───────────────────────────


def reset_summary_memory(state_dir: str, agent_name: str) -> None:
    """Stale tick notes make the model answer with a status report instead of
    the task; clear the agent's summary before each attempt. Best-effort."""
    try:
        con = sqlite3.connect(f"{state_dir}/agents.db", timeout=15)
        con.execute(
            "update managed_agents set summary_memory=? where name=?",
            ("", agent_name),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def resolve_agent_uuid(state_dir: str, agent_name: str) -> str | None:
    try:
        con = sqlite3.connect(f"{state_dir}/agents.db", timeout=15)
        row = con.execute(
            "select id from managed_agents where name=? and status != 'archived'"
            " order by last_run_at desc limit 1",
            (agent_name,),
        ).fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def write_feedback(
    label: str, keyword: str, score: float, agent_uuid: str, state_dir: str
) -> None:
    """Write the score onto the trace that produced the artifact (located by
    the feedback keyword the phase prompt carried). Best-effort: a locked or
    missing traces.db must never abort the pipeline."""
    try:
        con = sqlite3.connect(f"{state_dir}/traces.db", timeout=15)
        row = con.execute(
            "select trace_id from traces where agent=? and query like ?"
            " order by started_at desc limit 1",
            (agent_uuid, f"%{keyword}%"),
        ).fetchone()
        if row:
            con.execute(
                "update traces set feedback=? where trace_id=?",
                (float(score), row[0]),
            )
            con.commit()
            print(f"[research] {label}: feedback {score} -> trace {row[0]}")
        else:
            print(
                f"[research] {label}: no trace matched keyword '{keyword}', feedback skipped"
            )
        con.close()
    except sqlite3.Error as exc:
        print(f"[research] {label}: feedback write failed ({exc})")


def record_feedback(
    label: str,
    keyword: str,
    attempts: int,
    artifact: str,
    passed: str,
    state_dir: str,
    agent_name: str,
) -> None:
    size = 0
    try:
        size = os.path.getsize(artifact)
    except OSError:
        size = 0
    score = bash_feedback_score(attempts, size, passed)
    agent_uuid = resolve_agent_uuid(state_dir, agent_name)
    if not agent_uuid:
        print(f"[research] {label}: agent uuid not found, feedback skipped")
        return
    write_feedback(label, keyword, score, agent_uuid, state_dir)


# ── the retry/gate/feedback loop ─────────────────────────────────────────────


def ask_agent(root: Path, agent_name: str, prompt: str, asklog: str) -> None:
    """Run one CLI ask, capturing the execution log to ``asklog`` (the tool
    gate greps it). Faithful port of the old bash ``make ... jarvis-exec
    CMD=... | tee``; the return code is ignored (validation decides)."""
    cmd = (
        f'jarvis agents ask {agent_name} "{prompt.replace(chr(34), chr(92) + chr(34))}"'
    )
    proc = subprocess.run(
        ["make", "-C", str(root), "jarvis-exec", f"CMD={cmd}"],
        capture_output=True,
        text=True,
    )
    with open(asklog, "w", encoding="utf-8") as fh:
        fh.write(proc.stdout + proc.stderr)


def run_phase(
    spec: PhaseSpec,
    ctx: Ctx,
    *,
    ask=ask_agent,
) -> bool:
    """Run one phase with up to MAX_ATTEMPTS tries. Returns True on success
    (and records success feedback), False after exhausting retries (recording
    a low score so the TDL loop sees the failure)."""
    artifact = os.path.join(ctx.workspace_host, ctx.slug, spec.artifact)
    snapshot = (
        os.path.join(ctx.workspace_host, ctx.slug, spec.snapshot)
        if spec.snapshot
        else None
    )
    prompt = render_prompt(spec.prompt, prompt_vars(ctx))
    req_tool = None
    req_min = 0
    if spec.tool_req:
        req_tool, req_min = spec.tool_req.split(":", 1)
        req_min = int(req_min)

    attempt = 1
    while True:
        print(f"\n[research] {spec.label} — attempt {attempt}...")
        if snapshot and os.path.exists(snapshot):
            shutil.copyfile(snapshot, artifact)
            print(f"[research] {spec.label}: restored artifact from snapshot")
        reset_summary_memory(ctx.state_dir, ctx.agent_name)

        asklog = tempfile.mktemp(prefix="oj-ask-")
        try:
            ask(ctx.root, ctx.agent_name, prompt, asklog)
            if spec.normalize:
                bash_normalize(spec.normalize, artifact)
            ok = (
                os.path.exists(artifact)
                and os.path.getsize(artifact) >= ctx.min_artifact_size
                and (spec.validator is None or bash_validator(spec.validator, artifact))
            )
            if ok and req_tool is not None:
                n = bash_tool_count(asklog, req_tool)
                if n < req_min:
                    ok = False
                    print(
                        f"[research] {spec.label}: tool-usage gate failed "
                        f"({n} {req_tool} call(s) < {req_min})"
                    )
        finally:
            try:
                os.remove(asklog)
            except OSError:
                pass

        if ok:
            print(f"[research] {spec.label} OK -> {artifact}")
            if spec.fb_keyword:
                record_feedback(
                    spec.label,
                    spec.fb_keyword,
                    attempt,
                    artifact,
                    "yes",
                    ctx.state_dir,
                    ctx.agent_name,
                )
            return True

        if attempt >= MAX_ATTEMPTS:
            print(
                f"[research] ERROR: {spec.label} did not produce a valid {artifact}"
                f" after {attempt} attempts.",
                file=sys.stderr,
            )
            if spec.fb_keyword:
                record_feedback(
                    spec.label,
                    spec.fb_keyword,
                    attempt,
                    artifact,
                    "no",
                    ctx.state_dir,
                    ctx.agent_name,
                )
            return False

        print(
            f"[research] artifact missing, too small, failing validation, or tool "
            f"gate unmet ({artifact}); retrying..."
        )
        attempt += 1


# ── CLI (the thin launcher calls this) ───────────────────────────────────────


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "run" and argv[1] == "--phase":
        name = argv[2]
        if name not in PHASES:
            print(
                f"unknown phase {name!r}; known: {', '.join(sorted(PHASES))}",
                file=sys.stderr,
            )
            return 2
        try:
            ctx = Ctx.from_env()
        except RuntimeError as exc:
            print(f"research_phases: {exc}", file=sys.stderr)
            return 2
        return 0 if run_phase(PHASES[name], ctx) else 1
    print(
        "usage: research_phases.py run --phase <gather|verify|part1|part2>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
