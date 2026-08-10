"""Daily digest builder for the Trend Seeker research pipeline (D8/D9).

Turns a calendar day's deep-dive runs into two ready-to-publish artifacts:

  digests/<date>/social.md      ≤500-char social post (X/LinkedIn) — every
                                completed run whose digest passed; hook ≤140
                                chars, one labeled novelty bullet per run;
                                UNVERIFIED/PARTIAL runs are included with an
                                explicit footer marker, and the footer is a
                                real post footer ("AI-generated · verify
                                before acting"), never a local path.
  digests/<date>/newsletter.md  long form, novelty-first — one section per
                                completed report (status, verbatim Executive
                                Summary, the digest's What's notable lines,
                                inline caveats, sources), an "Also flagged"
                                list for runs without a report, and an
                                appendix with the machine-verified numbers
                                tables (clean runs only, verbatim).
  digests/<date>/digest-state.json
                                per-run digest state (gate, attempts, feedback,
                                fidelity, parsed content) — re-runnable and
                                idempotent: a run whose digest already passed
                                is reused without another engine call.

Design (agreed with the user, 2026-08-10):
  - Hybrid: ONE bounded engine call per completed run (reads report.md +
    numbers.md, writes a strict per-run digest file), then DETERMINISTIC
    code-side assembly — the model never composes social.md/newsletter.md.
  - Novelty-first contract (HOOK/NOVELTY/SPEC/SOURCE): the entry captures
    what is genuinely NEW (capabilities, design/business decisions, specs) —
    market-size/CAGR projections belong in the appendix, not the entry.
  - Integrity gates mirror the pipeline (D3/D5): every figure in the digest's
    claim lines must appear verbatim in numbers.md OR report.md (report
    grounding); every URL must appear in report.md; UNVERIFIED/PARTIAL
    flagging is code-injected, never prompt-trusted — flagged runs stay
    shareable but carry an explicit footer marker + inline caveats.
  - Feedback: each digest ask is scored (retries + size via the SAME
    research_lib.sh feedback_score) and written onto its trace with the
    keyword WRITE THE DAILY DIGEST ENTRY — feeding the TDL loop.
  - Re-ask cooldown: when a run's artifacts carry no figure tokens at all
    (placeholder numbers.md, figure-free report) and the digest fails the
    figure gate, re-asking is provably futile (any figure-bearing digest can
    never pass; the only passable figure-less form just failed) — the run
    fails after that one attempt instead of burning MAX_ATTEMPTS. Sources
    failures still retry normally (a URL CAN be fixed by re-asking).
  - Scope (v1): signal-triggered runs only (they have a signals.db row with
    research_slug + triggered_at). Manual subject-* runs predate state.json
    and are excluded; the date filter uses the LOCAL trigger date so the
    00:30-next-day run cannot mis-classify the 00:00 cycle's runs.

Stdlib-only: the live pipeline invokes this under the host python3, which
cannot import openjarvis. The ask/feedback/render seams are reused from
scripts/research_phases.py (the SAME tested code the deep-dive pipeline runs).

Usage (from scripts/digest.sh):
    OJ_STATE_DIR=... OJ_WORKSPACE_HOST=... OJ_AGENT_NAME=... \
    python3 scripts/digest.py [--date YYYY-MM-DD|yesterday|today] [--force]
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import string
import sys
import tempfile
from pathlib import Path

from research_phases import (
    MAX_ATTEMPTS,
    ask_agent,
    bash_feedback_score,
    record_feedback,
    reset_summary_memory,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
DIGEST_PROMPTS_DIR = SCRIPTS_DIR / "digest_prompts"

DIGEST_SCHEMA = 2
FB_KEYWORD = "WRITE THE DAILY DIGEST ENTRY"
MIN_DIGEST_SIZE = 100
HOOK_MAX = 140
NOVELTY_MAX = 200
SPEC_MAX = 200
SOURCE_MAX = 300
NOVELTY_LINES_MIN = 1
NOVELTY_LINES_MAX = 2
SPEC_LINES_MAX = 2
SOURCES_MAX = 3
SOCIAL_MAX = 500
SOCIAL_MIN_BULLET_ROOM = 20

# Deterministic gate-failure reasons (validate_digest_file / digest_one share
# them; the re-ask cooldown keys off the figure-gate reason).
FIG_GATE_FAIL = "a digest figure is not verbatim in numbers.md or report.md"
SRC_GATE_FAIL = "a digest URL is not in report.md"
NO_FIGURES_WHY = (
    "no real figures anywhere in numbers.md or report.md; "
    "a figure-bearing digest can never pass grounding"
)

_HEADING_RE = re.compile(r"^#+\s+\S", re.M)
_URL_RE = re.compile(r"https?://\S+")
_FIGURE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


# ── run selection (signals.db) ───────────────────────────────────────────────


def load_signal_runs(state_dir: str) -> list[dict]:
    """All signal rows that triggered a deep-dive run (best-effort: a missing
    or unreadable signals.db yields [] and the day digests as empty)."""
    try:
        con = sqlite3.connect(f"{state_dir}/signals.db", timeout=15)
        rows = con.execute(
            "select source, source_key, status, research_slug, triggered_at"
            " from signals"
            " where research_slug is not null and research_slug != ''"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [
        {
            "source": r[0],
            "source_key": r[1],
            "signal_status": r[2],
            "slug": r[3],
            "triggered_at": r[4] or "",
        }
        for r in rows
    ]


def local_trigger_date(triggered_at: str) -> str | None:
    """Local calendar date (YYYY-MM-DD) of a trigger timestamp. ISO 8601 with
    an offset is converted to the machine's local timezone (UTC-3 here, where
    all pipeline times are reported); naive timestamps are treated as UTC."""
    try:
        ts = triggered_at.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone().date().isoformat()
    except (ValueError, TypeError):
        return None


# ── run inspection (workspace artifacts) ─────────────────────────────────────


def _state_note(state_data: dict | None) -> str:
    """Deterministic caveat text from a run's state.json (worst failed phase)."""
    if not state_data:
        return ""
    phases = state_data.get("phases") or []
    for p in phases:
        if p.get("gate") != "pass":
            return (
                f"phase {p.get('phase')} {p.get('status')} after "
                f"{p.get('attempts')} attempt(s)"
            )
    return ""


def inspect_run(workspace: Path, run: dict) -> dict:
    """Assess one run dir: artifact presence + deterministic clean-ness.

    clean == signal DONE, report.md + numbers.md present, state.json present
    with every phase gate passing, and no UNVERIFIED / PARTIAL banner in the
    report. A soft PROVENANCE NOTE does NOT make a run unclean (it is a soft
    check by design); it is reported separately for the newsletter caveats.
    """
    d = workspace / run["slug"]
    report = d / "report.md"
    numbers = d / "numbers.md"
    state = d / "state.json"
    info = {
        **run,
        "topic": run["source_key"],
        "report_path": str(report),
        "numbers_path": str(numbers),
        "report_exists": report.is_file(),
        "numbers_exists": numbers.is_file(),
        "state_exists": state.is_file(),
        "unverified": False,
        "partial": False,
        "provenance_note": False,
        "state_gates_pass": False,
        "state_note": "",
    }
    if report.is_file():
        text = report.read_text(encoding="utf-8", errors="replace")
        info["unverified"] = bool(re.search(r"^> \*\*UNVERIFIED\*\*", text, re.M))
        info["partial"] = "PARTIAL REPORT" in text
        info["provenance_note"] = "PROVENANCE NOTE" in text
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            phases = data.get("phases") or []
            info["state_gates_pass"] = len(phases) >= 4 and all(
                p.get("gate") == "pass" for p in phases
            )
            info["state_note"] = _state_note(data)
        except (json.JSONDecodeError, OSError):
            info["state_note"] = "state.json unreadable"

    reasons: list[str] = []
    if run["signal_status"] != "DONE":
        reasons.append(f"signal status {run['signal_status']}")
    if not info["report_exists"]:
        reasons.append("no report.md")
    if not info["numbers_exists"]:
        reasons.append("no numbers.md")
    if not info["state_exists"]:
        reasons.append("no state.json (pre-state run?)")
    elif not info["state_gates_pass"]:
        reasons.append("not all phase gates pass")
    if info["unverified"]:
        reasons.append("UNVERIFIED figures banner")
    if info["partial"]:
        reasons.append("PARTIAL report banner")
    info["clean_reasons"] = reasons
    info["clean"] = not reasons
    return info


# ── digest contract parsing + fidelity gates (pure, deterministic) ───────────


def parse_digest(text: str) -> dict | None:
    """Parse a per-run digest file into {hook, novelty, spec, sources}.

    The contract is strict (structure from code, D5): any unknown line,
    a missing/over-long hook, no novelty lines, too many lines, or an
    oversized line fails the parse and forces a retry.
    """
    hooks: list[str] = []
    novelty: list[str] = []
    spec: list[str] = []
    sources: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("HOOK: "):
            hooks.append(line[len("HOOK: ") :].strip())
        elif line.startswith("NOVELTY: "):
            novelty.append(line[len("NOVELTY: ") :].strip())
        elif line.startswith("SPEC: "):
            spec.append(line[len("SPEC: ") :].strip())
        elif line.startswith("SOURCE: "):
            sources.append(line[len("SOURCE: ") :].strip())
        else:
            return None
    if len(hooks) != 1 or not hooks[0] or len(hooks[0]) > HOOK_MAX:
        return None
    if not (NOVELTY_LINES_MIN <= len(novelty) <= NOVELTY_LINES_MAX):
        return None
    if any(len(n) > NOVELTY_MAX for n in novelty):
        return None
    if len(spec) > SPEC_LINES_MAX or any(len(s) > SPEC_MAX for s in spec):
        return None
    if not sources or len(sources) > SOURCES_MAX:
        return None
    if any(
        len(s) > SOURCE_MAX or not s.startswith(("http://", "https://"))
        for s in sources
    ):
        return None
    return {
        "hook": hooks[0],
        "novelty": novelty,
        "spec": spec,
        "sources": sources,
    }


def _all_digest_text(parsed: dict) -> str:
    return " ".join(
        [parsed["hook"]] + parsed["novelty"] + parsed["spec"] + parsed["sources"]
    )


def _grounding_text(parsed: dict) -> str:
    """The digest's claim lines (hook + novelty + spec) — the text whose
    figures must be grounded. SOURCE lines are excluded: URLs carry digits
    and are already covered by sources_fidelity."""
    return " ".join([parsed["hook"]] + parsed["novelty"] + parsed["spec"])


def _figure_tokens(text: str) -> list[str]:
    """Numeric figures (decimals, percents, or comma-grouped integers) —
    bare years and plain integers are deliberately excluded so '2023' never
    gates; 23.69, 23.69% and 131,072 all do (a comma-grouped integer is a
    real spec figure, never a year). A prose comma glued to an integer
    ("2032,") is stripped so it can never masquerade as a grouped figure."""
    tokens = []
    for t in _FIGURE_RE.findall(text):
        t = t.rstrip(",")
        if t and ("." in t or "," in t or t.endswith("%")):
            tokens.append(t)
    return tokens


def _figure_core(token: str) -> str:
    """The numeric core of a figure token: one trailing '%' (unit) or one
    leading '$' (currency) is annotation on a verbatim value — the VALUE must
    still appear character-for-character in the run's artifacts.
    '13.066727193702121%' -> '13.066727193702121';
    '$201.13571874999994' -> '201.13571874999994'."""
    if token.endswith("%"):
        token = token[:-1]
    if token.startswith("$"):
        token = token[1:]
    return token


def figure_fidelity(parsed: dict, numbers_text: str, report_text: str) -> bool:
    """Report-grounding contract: every figure in the digest's claim lines
    must exist verbatim in numbers.md (the machine-verified artifact) or in
    report.md (the researched artifact) — no invented, rounded, or
    recomputed numbers, no made-up specs.

    The VALUE must appear character-for-character; a unit annotation on top
    (one trailing '%' or one leading '$') is accepted, because the integrity
    rule is about the number, not its unit. Rounded/reformatted values still
    fail: '13.07%' vs the table's '13.066727193702121' has no verbatim core.
    """
    haystack = f"{numbers_text}\n{report_text}"
    return all(
        _figure_core(t) in haystack for t in _figure_tokens(_grounding_text(parsed))
    )


def groundable_figures(numbers_text: str, report_text: str) -> list[str]:
    """Every figure token present in the run's artifacts (numbers.md ∪
    report.md) that a digest could ground against.

    An empty list means the run holds NO real figures at all (placeholder
    numbers.md, figure-free report): any figure-bearing digest can never pass
    figure_fidelity (its core cannot appear verbatim anywhere), so re-asking a
    figure-gate failure is provably futile — the only passable form is a
    figure-less entry, which the failed attempt already showed the model does
    not produce. Report-grounding makes the common placeholder case moot
    (liquidai passes via figures copied from report.md); this is the safety
    net for artifacts that carry no figures anywhere.
    """
    return _figure_tokens(f"{numbers_text}\n{report_text}")


def sources_fidelity(parsed: dict, report_text: str) -> bool:
    """Every URL in the digest must appear in report.md (which carries the
    Sources & References section) — no invented citations."""
    return all(
        u.rstrip(".,;:)") in report_text
        for u in _URL_RE.findall(_all_digest_text(parsed))
    )


def validate_digest_file(path: Path, run: dict) -> tuple[bool, str]:
    """Gate the per-run digest artifact: structure + both fidelity checks.
    Returns (ok, why)."""
    try:
        size = path.stat().st_size
    except OSError:
        return False, "digest file missing"
    if size < MIN_DIGEST_SIZE:
        return False, f"digest too small ({size}B < {MIN_DIGEST_SIZE}B)"
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_digest(text)
    if parsed is None:
        return False, "digest does not match the HOOK/NOVELTY/SPEC/SOURCE contract"
    try:
        report = Path(run["report_path"]).read_text(encoding="utf-8", errors="replace")
        numbers = Path(run["numbers_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False, "run artifacts unreadable"
    if not figure_fidelity(parsed, numbers, report):
        return False, FIG_GATE_FAIL
    if not sources_fidelity(parsed, report):
        return False, SRC_GATE_FAIL
    return True, ""


# ── the per-run engine call (retry/gate/feedback, mirrors run_phase) ─────────


def render_digest_prompt(name: str, vars_: dict[str, str]) -> str:
    """Render a digest prompt template (C2, same rules as research_phases.

    render_prompt). The templates live in scripts/digest_prompts/ — a separate
    directory from the pipeline's scripts/prompts/, whose templates are all
    wired to PHASES and asserted so by tests/pipeline (no orphans allowed).
    The trailing newline is trimmed: the launcher passes the prompt through
    ``make jarvis-exec CMD="... "`` and make splits recipes on newlines.
    """
    raw = (DIGEST_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return string.Template(raw).substitute(vars_).strip()


def digest_one(
    run: dict,
    date: str,
    digest_dir: Path,
    root: Path,
    agent_name: str,
    state_dir: str,
    *,
    ask=ask_agent,
) -> dict:
    """Ask the engine to write this run's digest file, up to MAX_ATTEMPTS.

    Returns the per-run digest state entry (gate, attempts, feedback, size,
    why). On success the feedback score is written onto the trace via the
    same record_feedback seam the deep-dive phases use (TDL loop).
    """
    slug = run["slug"]
    path = digest_dir / f"{slug}.digest.md"
    prompt = render_digest_prompt(
        "digest_per_run",
        {
            "SLUG": slug,
            "TOPIC": run["topic"],
            "REPORT": f"/workspace/{slug}/report.md",
            "NUMBERS": f"/workspace/{slug}/numbers.md",
            "DIGEST_FILE": f"/workspace/digests/{date}/{slug}.digest.md",
        },
    )
    # Slug-scoped feedback keyword: write_feedback locates the producing trace
    # by "query like %keyword%" — with a shared keyword, a failed ask whose own
    # trace never carried the prompt (the agent resumed a hot thread and the
    # trace reads "Continue your assigned task") would OVERWRITE the previous
    # run's score. Scoping to this run's slug keeps feedback on its own trace;
    # when the prompt never arrived there is no matching trace and feedback is
    # skipped honestly (best-effort by design).
    feedback_keyword = f"{FB_KEYWORD} FOR {slug}"
    attempt = 1
    while True:
        print(f"[digest] {slug}: digest attempt {attempt}...")
        reset_summary_memory(state_dir, agent_name)
        asklog = tempfile.mktemp(prefix="oj-digest-")
        try:
            ask(root, agent_name, prompt, asklog)
        finally:
            try:
                os.remove(asklog)
            except OSError:
                pass
        ok, why = validate_digest_file(path, run)
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass
        if ok:
            print(f"[digest] {slug}: digest OK -> {path}")
            score = bash_feedback_score(attempt, size, "yes")
            record_feedback("digest", feedback_keyword, score, state_dir, agent_name)
            return {
                "digest_gate": "pass",
                "digest_attempts": attempt,
                "digest_feedback": score,
                "digest_bytes": size,
                "digest_why": "",
            }
        if attempt >= MAX_ATTEMPTS:
            score = bash_feedback_score(attempt, size, "no")
            record_feedback("digest", feedback_keyword, score, state_dir, agent_name)
            print(
                f"[digest] ERROR: {slug}: digest failed ({why}) after {attempt} attempts.",
                file=sys.stderr,
            )
            return {
                "digest_gate": "fail",
                "digest_attempts": attempt,
                "digest_feedback": score,
                "digest_bytes": size,
                "digest_why": why,
            }
        if why == FIG_GATE_FAIL:
            # Re-ask cooldown: the run's artifacts carry no figure tokens at
            # all (placeholder numbers.md, figure-free report) — any
            # figure-bearing digest can NEVER pass the grounding gate, and
            # the only passable form (a figure-less entry) just failed. All
            # further re-asks are provably futile, so fail after this attempt
            # instead of burning the full MAX_ATTEMPTS.
            try:
                numbers_text = Path(run["numbers_path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
                report_text = Path(run["report_path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                numbers_text = report_text = ""
            if not groundable_figures(numbers_text, report_text):
                score = bash_feedback_score(attempt, size, "no")
                record_feedback(
                    "digest", feedback_keyword, score, state_dir, agent_name
                )
                print(
                    f"[digest] {slug}: no real figures anywhere in the run's "
                    "artifacts; a figure-bearing digest can never pass "
                    "grounding — skipping re-ask",
                    file=sys.stderr,
                )
                return {
                    "digest_gate": "fail",
                    "digest_attempts": attempt,
                    "digest_feedback": score,
                    "digest_bytes": size,
                    "digest_why": NO_FIGURES_WHY,
                }
        print(f"[digest] {slug}: digest gate unmet ({why}); retrying...")
        attempt += 1


# ── deterministic assembly (no engine) ───────────────────────────────────────


def extract_section(report_text: str, heading: str) -> str:
    """Verbatim text of a '## <heading>' section (heading through the next
    heading of any level). Empty string when the section is absent."""
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$", report_text, re.M)
    if not m:
        return ""
    rest = report_text[m.end() :]
    nxt = re.search(r"^#+\s+\S", rest, re.M)
    end = nxt.start() if nxt else len(rest)
    return rest[:end].strip()


def first_paragraphs(report_text: str, n: int = 3) -> str:
    """Fallback body for degraded reports: the first n non-banner paragraphs."""
    paras = [p.strip() for p in report_text.split("\n\n") if p.strip()]
    paras = [p for p in paras if not p.startswith(">")]
    return "\n\n".join(paras[:n])


def report_source_urls(report_text: str, limit: int = 3) -> list[str]:
    urls: list[str] = []
    for u in _URL_RE.findall(report_text):
        u = u.rstrip(".,;:)")
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def build_newsletter(date: str, runs: list[dict], workspace: Path) -> str:
    """Long form, novelty-first: one section per completed report (status,
    verbatim Executive Summary, the digest's What's notable lines, inline
    caveats, sources) + an 'Also flagged' list for runs without a report +
    an appendix with the machine-verified numbers tables. The body is
    excerpted, never engine-synthesized; the numbers live in the appendix,
    not the headline flow."""
    parts: list[str] = [f"# AI Infrastructure Digest — {date}", ""]
    done = [r for r in runs if r["report_exists"]]
    verified = sum(1 for r in done if r["clean"])
    parts.append(
        f"*What Trend Seeker surfaced on {date}: {len(done)} report(s) — "
        f"{verified} machine-verified. AI-generated summary of machine-run "
        f"research; figures are taken verbatim from each run's machine-checked "
        f"numbers table (appendix) or its report. Verify before acting.*"
    )
    for r in done:
        report_text = Path(r["report_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
        parts.append("")
        parts.append(f"## {r['topic']}")
        parts.append("")
        parts.append(
            "**Status:** "
            + (
                "machine-verified (all phases gated)."
                if r["clean"]
                else "; ".join(r["clean_reasons"])
            )
        )
        parts.append("")
        body = extract_section(report_text, "Executive Summary") or first_paragraphs(
            report_text
        )
        parts.append(body)
        digest = r.get("parsed_digest")
        if digest:
            parts.append("")
            parts.append("**What's notable**")
            parts.append("")
            parts.extend(f"- {n}" for n in digest["novelty"])
            parts.extend(f"- {s}" for s in digest["spec"])
        caveats: list[str] = []
        if r["unverified"]:
            caveats.append(
                "UNVERIFIED — figures are model-stated only; re-run verification "
                "before relying on any number."
            )
        if r["partial"]:
            caveats.append(
                "PARTIAL report banner — the report could not be fully completed "
                "within retries."
            )
        if r["provenance_note"]:
            caveats.append(
                "Carries a soft provenance note (some source URLs were not found "
                "in the gathered findings)."
            )
        if digest is None and r["digest_gate"] == "fail":
            caveats.append("Digest excerpt not produced (quality gate not satisfied).")
        if caveats:
            parts.append("")
            parts.append("**Caveats**")
            parts.append("")
            parts.extend(f"- {c}" for c in caveats)
        sources = (
            digest["sources"]
            if digest and digest["sources"]
            else report_source_urls(report_text)
        )
        if sources:
            parts.append("")
            parts.append("**Sources**")
            parts.append("")
            parts.extend(f"- {s}" for s in sources)
        parts.append("")

    no_report = [r for r in runs if not r["report_exists"]]
    if no_report:
        parts.append("## Also flagged")
        parts.append("")
        for r in no_report:
            why = "; ".join(r["clean_reasons"]) or "not clean"
            note = f" ({r['state_note']})" if r["state_note"] else ""
            parts.append(f"- **{r['slug']}** — {why}.{note}")
        parts.append("")

    appendix = [r for r in done if r["clean"] and r["numbers_exists"]]
    if appendix:
        parts.append(
            "## Appendix — machine-verified figures (verbatim from numbers.md)"
        )
        parts.append("")
        for r in appendix:
            numbers_text = Path(r["numbers_path"]).read_text(
                encoding="utf-8", errors="replace"
            )
            parts.append(f"### {r['topic']} ({r['slug']})")
            parts.append("")
            parts.append(numbers_text.strip())
            parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(
        f"*AI-generated daily digest of Trend Seeker research. Figures as "
        f"machine-verified in numbers.md on {date}; unverified figures are "
        f"flagged. Verify before acting. Not financial advice.*"
    )
    return "\n".join(parts)


def _fit(text: str, limit: int) -> str:
    """Word-boundary truncation with an ellipsis (deterministic)."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    cut = text[: limit - 1]
    last = cut.rfind(" ")
    if last > 0:
        cut = cut[:last]
    return cut + "…"


def build_social(date: str, runs: list[dict], workspace: Path) -> str | None:
    """≤500-char social post from the runs whose digest passed (hook from the
    first shareable run, one labeled novelty line per shareable run). Any
    completed run is shareable — UNVERIFIED/PARTIAL runs are included with an
    explicit marker in the footer instead of being hidden. None when there is
    nothing to share (no run with a passing digest)."""
    shareable = [r for r in runs if r.get("parsed_digest")]
    if not shareable:
        return None
    any_flag = any(r["unverified"] or r["partial"] for r in shareable)
    footer = "AI-generated · verify before acting" + (
        " · some figures unverified" if any_flag else ""
    )
    hook = shareable[0]["parsed_digest"]["hook"]
    items = [(r["topic"], r["parsed_digest"]["novelty"][0]) for r in shareable]
    labels = [f"• **{topic}**: " for topic, _ in items]

    def render(bullets: list[str]) -> list[str]:
        out = [hook, ""]
        out.extend(labels[i] + bullets[i] for i in range(len(bullets)))
        out.extend(["", footer])
        return out

    n = len(items)
    while n > 0:
        # per-bullet room: budget minus hook, footer, newlines and labels
        per = (
            SOCIAL_MAX
            - len(hook)
            - len(footer)
            - (n + 2)  # newlines between the parts
            - sum(len(labels[i]) for i in range(n))
        ) // n
        if per >= SOCIAL_MIN_BULLET_ROOM:
            bullets = [_fit(item[1], per) for item in items[:n]]
            candidate = "\n".join(render(bullets))
            if len(candidate) <= SOCIAL_MAX:
                return candidate
        n -= 1
    return None


# ── orchestration ────────────────────────────────────────────────────────────


def _digest_entry(run: dict) -> dict:
    """The run entry as persisted in digest-state.json (digest fields default
    for runs that never got an engine call)."""
    entry = {
        key: run[key]
        for key in (
            "slug",
            "topic",
            "signal_status",
            "triggered_at",
            "clean",
            "clean_reasons",
            "unverified",
            "partial",
            "provenance_note",
            "state_note",
        )
    }
    entry.update(
        digest_gate=run.get("digest_gate", "skipped"),
        digest_attempts=run.get("digest_attempts", 0),
        digest_feedback=run.get("digest_feedback"),
        digest_bytes=run.get("digest_bytes", 0),
        digest_why=run.get("digest_why", ""),
    )
    parsed = run.get("parsed_digest")
    entry["parsed_digest"] = parsed if parsed else None
    return entry


def write_state(path: Path, payload: dict) -> None:
    """Best-effort: a read-only/locked workspace must never abort the run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[digest] state write failed ({exc})")


def run(
    workspace_host: str,
    state_dir: str,
    date: str,
    agent_name: str,
    *,
    ask=ask_agent,
    force: bool = False,
) -> dict:
    """Build the digest for one calendar day. Returns the digest-state payload
    (also persisted); tests assert on both the return value and the files."""
    workspace = Path(workspace_host)
    digest_dir = workspace / "digests" / date
    digest_dir.mkdir(parents=True, exist_ok=True)  # writes below assume it exists
    state_path = digest_dir / "digest-state.json"

    prev: dict = {}
    if state_path.is_file():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
    prev_runs = {
        r["slug"]: r
        for r in (prev.get("runs") or [])
        if isinstance(r, dict) and r.get("slug")
    }

    runs = [
        inspect_run(workspace, row)
        for row in load_signal_runs(state_dir)
        if local_trigger_date(row["triggered_at"]) == date
    ]
    runs.sort(key=lambda r: r["triggered_at"])

    root = Path(__file__).resolve().parents[1]
    for r in runs:
        r.update(
            digest_gate="skipped",
            digest_attempts=0,
            digest_feedback=None,
            digest_bytes=0,
            digest_why="",
        )
        if not r["report_exists"]:
            continue
        prev_entry = prev_runs.get(r["slug"])
        if not force and prev_entry and prev_entry.get("digest_gate") == "pass":
            digest_path = digest_dir / f"{r['slug']}.digest.md"
            try:
                still_valid = (
                    parse_digest(
                        digest_path.read_text(encoding="utf-8", errors="replace")
                    )
                    is not None
                )
            except OSError:
                still_valid = False
            if not still_valid:
                # the digest file no longer satisfies the current contract
                # (e.g. it was written under an older format) — reusing it
                # would silently drop the run from social + What's notable,
                # so re-ask instead.
                print(
                    f"[digest] {r['slug']}: prior digest no longer matches the"
                    " contract; re-asking"
                )
                r.update(
                    digest_one(
                        r, date, digest_dir, root, agent_name, state_dir, ask=ask
                    )
                )
                continue
            for key in (
                "digest_gate",
                "digest_attempts",
                "digest_feedback",
                "digest_bytes",
                "digest_why",
            ):
                if key in prev_entry:
                    r[key] = prev_entry[key]
            print(f"[digest] {r['slug']}: digest already passed, reusing")
        else:
            r.update(
                digest_one(r, date, digest_dir, root, agent_name, state_dir, ask=ask)
            )

    for r in runs:
        if r["digest_gate"] == "pass":
            try:
                r["parsed_digest"] = parse_digest(
                    (digest_dir / f"{r['slug']}.digest.md").read_text(
                        encoding="utf-8", errors="replace"
                    )
                )
            except OSError:
                r["parsed_digest"] = None
        else:
            r["parsed_digest"] = None

    social = build_social(date, runs, workspace)
    newsletter = (
        build_newsletter(date, runs, workspace)
        if any(r["report_exists"] for r in runs)
        else None
    )
    if social is not None:
        (digest_dir / "social.md").write_text(social, encoding="utf-8")
        print(f"[digest] wrote {digest_dir / 'social.md'} ({len(social)} chars)")
    if newsletter is not None:
        (digest_dir / "newsletter.md").write_text(newsletter, encoding="utf-8")
        print(
            f"[digest] wrote {digest_dir / 'newsletter.md'} ({len(newsletter)} chars)"
        )

    payload = {
        "schema": DIGEST_SCHEMA,
        "date": date,
        "produced_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "runs": [_digest_entry(r) for r in runs],
        "clean_count": sum(1 for r in runs if r["clean"]),
        "flagged_count": sum(1 for r in runs if not r["clean"]),
        "social": {
            "written": social is not None,
            "chars": len(social) if social is not None else 0,
        },
        "newsletter": {
            "written": newsletter is not None,
            "chars": len(newsletter) if newsletter is not None else 0,
        },
    }
    write_state(state_path, payload)
    print(
        f"[digest] {date}: {len(runs)} run(s), {payload['clean_count']} clean, "
        f"{payload['flagged_count']} flagged; state -> {state_path}"
    )
    return payload


# ── CLI ──────────────────────────────────────────────────────────────────────


def usage() -> None:
    print(
        "usage: digest.py [--date YYYY-MM-DD|yesterday|today] [--force]",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    date_arg: str | None = None
    force = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--date":
            i += 1
            if i >= len(argv):
                usage()
                return 2
            date_arg = argv[i]
        elif arg == "--force":
            force = True
        elif arg in ("--help", "-h"):
            usage()
            return 0
        else:
            usage()
            return 2
        i += 1

    if date_arg in (None, "yesterday"):
        date_arg = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    elif date_arg == "today":
        date_arg = datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(date_arg)
    except ValueError:
        print(
            f"digest: invalid date {date_arg!r} (expected YYYY-MM-DD)", file=sys.stderr
        )
        return 2

    workspace = os.environ.get(
        "OJ_WORKSPACE_HOST", os.path.expanduser("~/Git/openjarvis-workspace")
    )
    state_dir = os.environ.get("OJ_STATE_DIR", os.path.expanduser("~/.openjarvis"))
    agent_name = os.environ.get("OJ_AGENT_NAME", "it-market-analyst")
    run(workspace, state_dir, date_arg, agent_name, force=force)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
