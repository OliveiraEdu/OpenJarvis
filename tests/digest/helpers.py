"""Fixtures/helpers for the digest regression harness.

Mirrors tests/pipeline/helpers.py: the digest engine (scripts/digest.py) is
stdlib-only and its deterministic leaves (contract parser, fidelity gates,
budgeted assembly, run classification) are tested directly; the per-run
engine seam is injected as a scripted fake that writes a fixture digest file,
exactly like tests/pipeline uses FakeAsk against research_phases.run_phase.
The launcher (scripts/digest.sh) is exercised through the bash seam.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# ── feedback keyword (mirrors scripts/digest.py) ─────────────────────────────

FB_KEYWORD = "WRITE THE DAILY DIGEST ENTRY"


def scoped_keyword(slug: str) -> str:
    """The slug-scoped feedback keyword the digest writes with (and the prompt
    carries): write_feedback locates the producing trace by this exact string,
    so it can never land on another run's trace."""
    return f"{FB_KEYWORD} FOR {slug}"


# ── artifact fixtures (shape mirrors a real verified run) ────────────────────

NUMBERS = (
    "## Verified Figures\n\n"
    "| Metric | Base Year | End Year | Source | Formula | Computed Result | Discrepancy Note |\n"
    "|--------|----------|---------|--------|---------|------------------|------------------|\n"
    "| CAGR of SLM Market | 2023 | 2032 | Findings.md | ((5.45/0.65)^(1/10)-1)*100 | 23.69% | None |\n"
    "| Projection of SLM Market | 2023 | 2032 | Findings.md | 100*(1+0.15)^5 | 201.14% | None |\n"
    "| Market Size 2023 | 2023 | 2023 | Findings.md | 5.45 | 5.45 | None |\n"
)

UNVERIFIED_BANNER = "> **UNVERIFIED** — figures could not be machine-verified; every figure below is model-stated only.\n\n"

REPORT = (
    "# Title\n\n"
    "## Introduction\n\n"
    "Intro paragraph with {topic} context.\n\n"
    "## Executive Summary\n\n"
    "{summary}\n\n"
    "## Detailed Analysis\n\n"
    "Analysis body paragraph with market context.\n\n"
    "## Conclusions\n\n"
    "Conclusion paragraph.\n\n"
    "## Sources & References\n\n"
    "1. Source One - https://example.com/{slug}/one - 2026-08-01\n"
    "2. Source Two - https://example.com/{slug}/two - 2026-08-01\n\n"
    "## Confidence Assessment\n\n"
    "High confidence.\n"
)


def clean_state(slug: str, topic: str) -> dict:
    return {
        "schema": 1,
        "run_id": slug,
        "topic": topic,
        "phases": [
            {"phase": "gather", "attempts": 1, "status": "OK", "gate": "pass"},
            {"phase": "verify", "attempts": 1, "status": "OK", "gate": "pass"},
            {"phase": "part1", "attempts": 1, "status": "OK", "gate": "pass"},
            {"phase": "part2", "attempts": 1, "status": "OK", "gate": "pass"},
        ],
    }


def write_run(
    workspace: Path,
    slug: str,
    topic: str,
    *,
    summary: str | None = None,
    unverified: bool = False,
    partial: bool = False,
    provenance: bool = False,
    no_state: bool = False,
    no_report: bool = False,
    no_numbers: bool = False,
    fail_gate: bool = False,
) -> None:
    """Write a run dir shaped like a real deep-dive run. Clean-ness is DERIVED
    by inspect_run from these files — nothing pre-baked."""
    d = workspace / slug
    d.mkdir(parents=True, exist_ok=True)
    if not no_report:
        text = REPORT.format(
            slug=slug,
            topic=topic,
            summary=summary or f"{topic} summary with a verified 23.69% CAGR.",
        )
        if unverified:
            text = UNVERIFIED_BANNER + text
        if partial:
            text = (
                text
                + "\n\n> **PARTIAL REPORT** — phase 3b could not complete within retries.\n"
            )
        if provenance:
            text = (
                text
                + "\n\n> **PROVENANCE NOTE** — 2 of 2 source URL(s) not found in findings.\n"
            )
        (d / "report.md").write_text(text, encoding="utf-8")
    if not no_numbers:
        nums = UNVERIFIED_BANNER + NUMBERS if unverified else NUMBERS
        (d / "numbers.md").write_text(nums, encoding="utf-8")
    if not no_state:
        state = clean_state(slug, topic)
        if fail_gate:
            state["phases"][0]["gate"] = "fail"
            state["phases"][0]["status"] = "GATE_FAIL"
        (d / "state.json").write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8"
        )


def make_signals_db(state_dir: Path, rows: list[tuple]) -> None:
    """rows: (source, source_key, status, research_slug, triggered_at)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state_dir / "signals.db")
    con.execute(
        "create table signals ("
        " source text, source_key text, status text,"
        " research_slug text, triggered_at text)"
    )
    con.executemany("insert into signals values (?,?,?,?,?)", rows)
    con.commit()
    con.close()


def make_trace_state(state_dir: Path, keywords: list[str]) -> None:
    """agents.db + one traces.db row per keyword (each query embeds that
    keyword, mirroring the real ask traces), so feedback writing is exercised
    exactly the way production locates the producing trace (slug-scoped)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(state_dir / "agents.db")
    con.execute(
        "create table managed_agents ("
        " id text primary key, name text, status text,"
        " last_run_at text, summary_memory text)"
    )
    con.execute(
        "insert into managed_agents values (?,?,?,?,?)",
        ("u-1", "test-agent", "running", "2026-08-03T00:00:00", ""),
    )
    con.commit()
    con.close()
    con = sqlite3.connect(state_dir / "traces.db")
    con.execute(
        "create table traces ("
        " trace_id text primary key, agent text, query text,"
        " started_at text, feedback real)"
    )
    for i, keyword in enumerate(keywords, start=1):
        con.execute(
            "insert into traces values (?,?,?,?,?)",
            (
                f"u-{i}",
                "u-1",
                f"do the work {keyword} now",
                "2026-08-03T00:00:00",
                None,
            ),
        )
    con.commit()
    con.close()


def trace_feedback(state_dir: Path, keyword: str) -> float | None:
    con = sqlite3.connect(state_dir / "traces.db")
    row = con.execute(
        "select feedback from traces where query like ? order by started_at desc limit 1",
        (f"%{keyword}%",),
    ).fetchone()
    con.close()
    return row[0] if row else None


def valid_digest(slug: str) -> str:
    """A digest that passes the contract + both fidelity gates for write_run's
    artifacts: figures verbatim from NUMBERS, URL present in REPORT."""
    return (
        "HOOK: Small language models keep compounding: 23.69% CAGR to 2032.\n"
        "KEY_NUMBER: SLM market CAGR: 23.69% (2023-2032)\n"
        f"BULLET: {slug} keeps climbing as local-first inference takes off.\n"
        f"SOURCE: https://example.com/{slug}/one\n"
    )


class FakeDigestAsk:
    """Scripted fake for the digest ask seam (signature matches
    research_phases.ask_agent). Attempt i for a slug writes scripts[slug][i];
    the last script repeats for any further attempts; None writes nothing.
    The slug is read from the rendered prompt's container digest path."""

    def __init__(
        self,
        workspace: Path,
        date: str,
        scripts: dict | None = None,
    ):
        self.workspace = Path(workspace)
        self.date = date
        self.scripts = scripts or {}
        self.calls: dict[str, int] = {}

    def __call__(self, root: Path, agent_name: str, prompt: str, asklog: str) -> None:
        m = re.search(rf"/workspace/digests/{self.date}/([^ ]+)\.digest\.md", prompt)
        slug = m.group(1) if m else "?"
        self.calls[slug] = self.calls.get(slug, 0) + 1
        content = self.scripts.get(slug)
        if content:
            idx = min(self.calls[slug] - 1, len(content) - 1)
            body = content[idx]
            if body is not None:
                path = self.workspace / "digests" / self.date / f"{slug}.digest.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
        Path(asklog).write_text(
            "  ↳ file_read path=/x\n  ↳ file_write path=/y\n", encoding="utf-8"
        )


# ── convenience builders ─────────────────────────────────────────────────────

DAY_SLUGS = ("ollama-scope-ai", "comfy-org-minimax-h3-scope-ai")


def setup_day(
    tmp_path: Path,
    date: str,
    rows: list[tuple],
    runs_spec: dict[str, dict],
    *,
    trace_state: bool = False,
) -> tuple[Path, Path]:
    """Write workspace runs + signals.db; returns (workspace, state_dir)."""
    ws = tmp_path / "ws"
    state = tmp_path / "state"
    make_signals_db(state, rows)
    for slug, kw in runs_spec.items():
        topic = kw.pop("topic", slug)
        write_run(ws, slug, topic, **kw)
    if trace_state:
        make_trace_state(state, [scoped_keyword(row[3]) for row in rows])
    return ws, state


def day_rows(date: str, slugs=DAY_SLUGS, status="DONE") -> list[tuple]:
    """Mid-day UTC triggers so the local calendar date equals the UTC date in
    any timezone (tests stay hermetic regardless of the machine's TZ)."""
    return [
        (
            "hf",
            slug.replace("-scope-ai", "").replace("-", "/") or slug,
            status,
            slug,
            f"{date}T15:00:00+00:00",
        )
        for slug in slugs
    ]


def load_payload(tmp_path: Path, date: str) -> dict:
    return json.loads(
        (tmp_path / "ws" / "digests" / date / "digest-state.json").read_text(
            encoding="utf-8"
        )
    )
