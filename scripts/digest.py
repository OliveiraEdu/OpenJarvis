"""Daily digest builder for the Trend Seeker research pipeline (D8/D9).

Turns a calendar day's deep-dive runs into two ready-to-publish artifacts:

  digests/<date>/social.md      ≤500-char social post — ONLY clean runs
                                (status DONE, all phases gated, no UNVERIFIED
                                /PARTIAL banners); hook ≤140 chars, footer with
                                the newsletter path + verification caveat.
  digests/<date>/newsletter.md  long form — every completed report, verbatim
                                Executive Summary + machine-checked numbers
                                table + takeaways + sources, plus a Caveats
                                section that flags UNVERIFIED / PARTIAL /
                                no-state / FAILED runs deterministically.
  digests/<date>/digest-state.json
                                per-run digest state (gate, attempts, feedback,
                                fidelity, parsed content) — re-runnable and
                                idempotent: a run whose digest already passed
                                is reused without another engine call.

Design (agreed with the user, 2026-08-09):
  - Hybrid: ONE bounded engine call per completed run (reads report.md +
    numbers.md, writes a strict per-run digest file), then DETERMINISTIC
    code-side assembly — the model never composes social.md/newsletter.md.
  - Integrity gates mirror the pipeline (D3/D5): every figure in the digest
    must appear verbatim in numbers.md; every URL must appear in report.md;
    UNVERIFIED/PARTIAL flagging is code-injected, never prompt-trusted.
  - Feedback: each digest ask is scored (retries + size via the SAME
    research_lib.sh feedback_score) and written onto its trace with the
    keyword WRITE THE DAILY DIGEST ENTRY — feeding the TDL loop.
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

DIGEST_SCHEMA = 1
FB_KEYWORD = "WRITE THE DAILY DIGEST ENTRY"
MIN_DIGEST_SIZE = 100
HOOK_MAX = 140
BULLET_MAX = 200
KEY_NUMBER_MAX = 300
SOURCE_MAX = 300
BULLETS_MIN = 1
BULLETS_MAX = 3
KEY_NUMBERS_MAX = 3
SOURCES_MAX = 3
SOCIAL_MAX = 500
SOCIAL_MIN_BULLET_ROOM = 20

_HEADING_RE = re.compile(r"^#+\s+\S", re.M)
_URL_RE = re.compile(r"https?://\S+")
_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?%?")


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
    """Parse a per-run digest file into {hook, key_numbers, bullets, sources}.

    The contract is strict (structure from code, D5): any unknown line,
    a missing/over-long hook, no bullets, too many lines, or an oversized
    line fails the parse and forces a retry.
    """
    hooks: list[str] = []
    key_numbers: list[str] = []
    bullets: list[str] = []
    sources: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("HOOK: "):
            hooks.append(line[len("HOOK: ") :].strip())
        elif line.startswith("KEY_NUMBER: "):
            key_numbers.append(line[len("KEY_NUMBER: ") :].strip())
        elif line.startswith("BULLET: "):
            bullets.append(line[len("BULLET: ") :].strip())
        elif line.startswith("SOURCE: "):
            sources.append(line[len("SOURCE: ") :].strip())
        else:
            return None
    if len(hooks) != 1 or not hooks[0] or len(hooks[0]) > HOOK_MAX:
        return None
    if not (BULLETS_MIN <= len(bullets) <= BULLETS_MAX):
        return None
    if any(len(b) > BULLET_MAX for b in bullets):
        return None
    if len(key_numbers) > KEY_NUMBERS_MAX or len(sources) > SOURCES_MAX:
        return None
    if not key_numbers:
        return None
    if any(len(k) > KEY_NUMBER_MAX for k in key_numbers):
        return None
    if any(
        len(s) > SOURCE_MAX or not s.startswith(("http://", "https://"))
        for s in sources
    ):
        return None
    return {
        "hook": hooks[0],
        "key_numbers": key_numbers,
        "bullets": bullets,
        "sources": sources,
    }


def _all_digest_text(parsed: dict) -> str:
    return " ".join(
        [parsed["hook"]] + parsed["key_numbers"] + parsed["bullets"] + parsed["sources"]
    )


def _figure_tokens(text: str) -> list[str]:
    """Numeric figures (decimals or percents) — years and plain integers are
    deliberately excluded so '2023' never gates; 23.69 / 23.69% do."""
    return [t for t in _FIGURE_RE.findall(text) if "." in t or t.endswith("%")]


def numbers_fidelity(parsed: dict, numbers_text: str) -> bool:
    """Every figure in the digest must appear verbatim in numbers.md (the
    machine-verified artifact) — no invented, rounded, or recomputed numbers."""
    return all(t in numbers_text for t in _figure_tokens(_all_digest_text(parsed)))


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
        return False, "digest does not match the HOOK/KEY_NUMBER/BULLET/SOURCE contract"
    try:
        report = Path(run["report_path"]).read_text(encoding="utf-8", errors="replace")
        numbers = Path(run["numbers_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False, "run artifacts unreadable"
    if not numbers_fidelity(parsed, numbers):
        return False, "a digest figure is not verbatim in numbers.md"
    if not sources_fidelity(parsed, report):
        return False, "a digest URL is not in report.md"
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
            "TOPIC": run["topic"],
            "REPORT": f"/workspace/{slug}/report.md",
            "NUMBERS": f"/workspace/{slug}/numbers.md",
            "DIGEST_FILE": f"/workspace/digests/{date}/{slug}.digest.md",
        },
    )
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
            record_feedback("digest", FB_KEYWORD, score, state_dir, agent_name)
            return {
                "digest_gate": "pass",
                "digest_attempts": attempt,
                "digest_feedback": score,
                "digest_bytes": size,
                "digest_why": "",
            }
        if attempt >= MAX_ATTEMPTS:
            score = bash_feedback_score(attempt, size, "no")
            record_feedback("digest", FB_KEYWORD, score, state_dir, agent_name)
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
    """Long form: one section per completed report (verbatim Executive Summary,
    verbatim numbers table, takeaways, sources) + Caveats for every non-clean
    run. The body is excerpted, never engine-synthesized."""
    parts: list[str] = [f"# AI Infrastructure Digest — {date}", ""]
    done = [r for r in runs if r["report_exists"]]
    parts.append(
        f"*Daily digest of the Trend Seeker research pipeline: {len(done)} report(s) "
        f"completed on {date}. Figures below are taken verbatim from each run's "
        f"machine-checked numbers table; sources come from each run's report. "
        f"AI-generated — verify before acting.*"
    )
    for r in done:
        report_text = Path(r["report_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
        parts.append("")
        parts.append(f"## {r['topic']} ({r['slug']})")
        parts.append("")
        parts.append(
            f"**Status:** "
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
        if r["numbers_exists"]:
            numbers_text = Path(r["numbers_path"]).read_text(
                encoding="utf-8", errors="replace"
            )
            parts.append("")
            if r["unverified"]:
                parts.append("**Key figures** (UNVERIFIED — not machine-verified)")
            else:
                parts.append("**Key figures** (verbatim from numbers.md)")
            parts.append("")
            parts.append(numbers_text.strip())
        digest = r.get("parsed_digest")
        if digest:
            parts.append("")
            parts.append("**Key takeaways**")
            parts.append("")
            parts.extend(f"- {b}" for b in digest["bullets"])
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
    flagged = [r for r in runs if not r["clean"]]
    soft_notes = [r for r in runs if r["clean"] and r["provenance_note"]]
    if flagged or soft_notes:
        parts.append("## Caveats")
        parts.append("")
        for r in flagged:
            why = "; ".join(r["clean_reasons"]) or "not clean"
            note = f" ({r['state_note']})" if r["state_note"] else ""
            parts.append(f"- **{r['slug']}** — {why}.{note}")
        for r in soft_notes:
            parts.append(
                f"- **{r['slug']}** — clean, but carries a soft provenance note "
                f"(some source URLs were not found in the gathered findings)."
            )
        parts.append("")
        for r in flagged:
            why = "; ".join(r["clean_reasons"]) or "not clean"
            note = f" ({r['state_note']})" if r["state_note"] else ""
            prov = (
                " — carries a soft provenance note (some source URLs were not found in the gathered findings)"
                if r["provenance_note"]
                else ""
            )
            parts.append(f"- **{r['slug']}** — {why}.{note}{prov}")
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
    """≤500-char social post from the clean runs' digests (hook from the first
    clean run, one bullet per clean run). None when there is nothing to share
    (no clean run with a passing digest)."""
    clean = [r for r in runs if r["clean"] and r.get("parsed_digest")]
    if not clean:
        return None
    footer = f"Full digest → {workspace}/digests/{date}/newsletter.md · figures machine-verified"
    hook = clean[0]["parsed_digest"]["hook"]
    bullets = [r["parsed_digest"]["bullets"][0] for r in clean]

    def render(bullet_list: list[str], per: int) -> list[str]:
        out = [hook, ""]
        out.extend(f"• {_fit(b, per)}" for b in bullet_list)
        out.extend(["", footer])
        return out

    n = len(bullets)
    parts: list[str] = []
    while n > 0:
        # exact budget: total = hook + footer + 4 newlines + n*2 ("• ") + Σbullets
        per = (SOCIAL_MAX - len(hook) - len(footer) - 4 - 2 * n) // n
        if per >= SOCIAL_MIN_BULLET_ROOM:
            candidate = "\n".join(render(bullets[:n], per))
            if len(candidate) <= SOCIAL_MAX:
                parts = candidate.split("\n")
                break
        n -= 1
        bullets = bullets[:n]
    if not parts:
        return None
    return "\n".join(parts)


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
