#!/usr/bin/env python3
"""Export real discovery state — signals + triage results — into fixtures.

PURPOSE (C3/C6): the offline harness in tests/discovery/ exercises the SAME
pure modules (rules, triage parse/render) on canned payloads. This script
turns real production state — the signals a live cycle stored and the triage
results it produced — into committed fixtures, so a real payload shape (or a
triage decision) becomes a permanent regression at the layer that consumed
it. It mirrors ``scripts/export_trace_fixtures.py`` exactly in structure.

WHAT IT EXPORTS (fixtures tree under tests/discovery/fixtures/):
  signals/<id>-<source>.json
      one sanitized real signal per row of signals.db (every status), in row
      order. The reconstruction contract: source + title + sanitized metrics
      are enough to re-run ``rules.pre_qualify``/``noise_filters`` and
      ``triage.render_prompt``; the recorded pre_qualify/score/category/
      triage_reason/status are the ground-truth decision for that payload.
  triage_replies/<id>-<source>-<score>-<category>.json
      the real triage payloads: the sanitized signal (input) paired with the
      machine-checked triage result the live engine produced (output). NOTE:
      the store persists only the checked result — the raw engine text is
      intentionally not kept (parse-level reply regressions stay covered by
      the canned-reply tests in test_triage.py, D6).
  README.md  origin + sanitization rules + refresh instructions (auto-written).

SANITIZATION (C7, design §10.6): every exported signal drops its identity
fields (id, source_key, url, research_slug, triggered_at, created_at,
updated_at) and keeps only a per-source metric whitelist, plus a value-level
guard that drops any remaining metric whose value contains a URL or a
secret-shaped token. Titles are kept: the rules and the triage prompt consume
them, and they are public content (repo/user names are public in the sources
this engine tracks).

USAGE (from repo root):
    python3 scripts/export_discovery_fixtures.py
    python3 scripts/export_discovery_fixtures.py --db PATH

The exporter refuses to run when the db is missing, holds no signals, or holds
no triaged signals (a partial fixture tree would silently weaken the replay
tests), so it cannot produce empty fixtures. The generated ``signals/`` and
``triage_replies/`` dirs are cleared on each run — row ids are monotonic, so
filenames shift between refreshes and stale fixtures must not linger.
Committed fixtures are the record; re-run only to refresh after a new live
run. Fixtures must contain no secrets (tvly-/sk-/ghp_ shaped tokens) — see
tests/discovery/test_fixture_hygiene.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "discovery" / "fixtures"

DEFAULT_DB = Path.home() / ".openjarvis" / "signals.db"

# Same guards as tests/pipeline/test_trace_fixtures.py: committed fixtures can
# only contain shapes that look nothing like a key.
SECRET_PATTERNS = [
    re.compile(r"tvly-[A-Za-z0-9_-]{10,}"),  # Tavily
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI/Anthropic style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"\bBearer [A-Za-z0-9._-]{20,}"),  # bearer tokens
]
_URLISH = re.compile(r"https?://", re.IGNORECASE)

# Per-source metric whitelist (design §10.6): numeric/date/structural metrics
# survive; identity-bearing or free-text values do not. Anything not listed is
# dropped even if it passes the value guard (defense in depth).
METRIC_WHITELIST: dict[str, frozenset[str]] = {
    "github": frozenset(
        {
            "stars",
            "forks",
            "open_issues",
            "created_at",
            "pushed_at",
            "language",
            "license",
            "owner_type",
            "contributors",
        }
    ),
    "hn": frozenset({"points", "num_comments", "created_at"}),
    "hf": frozenset(
        {
            "downloads",
            "likes",
            "trending_score",
            "pipeline_tag",
            "library_name",
            "last_modified",
        }
    ),
    "reddit": frozenset({"subreddit", "updated"}),
    "pypi": frozenset(
        {
            "version",
            "requires_python",
            "releases_count",
            "latest_release",
            "downloads_last_week",
            "summary",
        }
    ),
    "pricing": frozenset({"content_hash", "bytes", "normalized_len"}),
}

# Signal fields kept in fixtures (identity/timestamp fields are dropped).
_SIGNAL_FIELDS = (
    "source",
    "title",
    "metrics",
    "pre_qualify",
    "score",
    "category",
    "triage_reason",
    "status",
)

_FETCH_SQL = (
    "SELECT source, source_key, title, metrics, pre_qualify, score, category,"
    " triage_reason, status FROM signals ORDER BY id"
)


def _clean_metrics(source: str, raw: str) -> dict:
    """Whitelist metrics per source, then drop any URL/secret-shaped value."""
    try:
        metrics = json.loads(raw)
    except json.JSONDecodeError:
        metrics = {}
    allowed = METRIC_WHITELIST.get(source, frozenset())
    cleaned: dict = {}
    for key, value in metrics.items():
        if key not in allowed:
            continue
        text = str(value)
        if _URLISH.search(text) or any(p.search(text) for p in SECRET_PATTERNS):
            continue
        cleaned[key] = value
    return cleaned


def export_signals(con: sqlite3.Connection) -> list[str]:
    exported = []
    sig_dir = FIXTURES / "signals"
    sig_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute(_FETCH_SQL).fetchall()
    if not rows:
        raise SystemExit("[export] signals.db holds no signals — nothing to export")
    for i, row in enumerate(rows, start=1):
        source = row["source"]
        signal = {
            "source": source,
            "title": row["title"],
            "metrics": _clean_metrics(source, row["metrics"]),
            "pre_qualify": row["pre_qualify"] or "",
            "score": row["score"],
            "category": row["category"] or "",
            "triage_reason": row["triage_reason"] or "",
            "status": row["status"],
        }
        path = sig_dir / f"{i:03d}-{source}.json"
        path.write_text(
            json.dumps(signal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        exported.append(str(path.relative_to(REPO_ROOT)))
    return exported


def export_triage_replies(con: sqlite3.Connection) -> list[str]:
    exported = []
    reply_dir = FIXTURES / "triage_replies"
    reply_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute(_FETCH_SQL).fetchall()
    for i, row in enumerate(rows, start=1):
        if row["score"] is None:
            continue  # never triaged: no payload to record
        source = row["source"]
        signal = {
            "source": source,
            "title": row["title"],
            "metrics": _clean_metrics(source, row["metrics"]),
        }
        triage_result = {
            "score": row["score"],
            "category": row["category"] or "",
            "reason": row["triage_reason"] or "",
        }
        payload = {"signal": signal, "triage": triage_result}
        path = reply_dir / (
            f"{i:03d}-{source}-{row['score']}-{row['category'] or 'unknown'}.json"
        )
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        exported.append(str(path.relative_to(REPO_ROOT)))
    return exported


def write_readme(exported: list[str], stats: dict[str, int]) -> None:
    readme = FIXTURES / "README.md"
    readme.write_text(
        "# Discovery regression fixtures (signal replay)\n"
        "\n"
        "Real signals and triage results from live Trend Seeker discovery cycles\n"
        "(`scripts/discovery/`), exported so the offline harness in this directory\n"
        "can replay production payload shapes and decisions without a model or\n"
        "network (**C3**). The tests run the SAME pure modules (`rules.py`,\n"
        "`triage.py`) that the live engine uses.\n"
        "\n"
        "## Origin\n"
        "\n"
        "- **signals/** — one sanitized signal per row of `signals.db`, first\n"
        "  exported 2026-08-05 from the live cycle that ran after the per-repo\n"
        "  GitHub contributor capture landed (design §4.4 contributor_spike). The\n"
        "  run collected 28 real signals (github 20, hn 3, pypi 3, pricing 2;\n"
        "  reddit returned HTTP 429) and triaged 1: the Azure pricing page\n"
        "  content-hash change (PRICING_DIFF, score 8, category `cloud`). The\n"
        "  github repos that carried `contributors > 15` were 33-88 days old, so\n"
        "  none met the `< 30d` freshness leg of contributor_spike — the rule is\n"
        "  exercised end-to-end but requires a fresh repo to fire.\n"
        "- **triage_replies/** — the triaged payloads: sanitized input signal +\n"
        "  the machine-checked result the live engine produced. The store persists\n"
        "  only the checked `{score, category, reason}`; the raw engine text is\n"
        "  intentionally not kept (parse-level reply regressions stay covered by\n"
        "  the canned-reply tests in `test_triage.py`, D6).\n"
        "\n"
        "## Sanitization (C7)\n"
        "\n"
        "Every exported signal drops its identity fields (`id`, `source_key`,\n"
        "`url`, `research_slug`, `triggered_at`, `created_at`, `updated_at`) and\n"
        "keeps only the per-source metric whitelist (`METRIC_WHITELIST` in the\n"
        "exporter), plus a value-level guard that drops any metric containing a\n"
        "URL or a secret-shaped token. Titles are public content consumed by the\n"
        "rules and the triage prompt, so they are kept. See design §10.6.\n"
        "\n"
        "## Refresh\n"
        "\n"
        "```bash\n"
        "python3 scripts/export_discovery_fixtures.py   # from repo root\n"
        "```\n"
        "The exporter refuses to run when `signals.db` is missing or empty. After\n"
        "a refresh, re-run `uv run pytest tests/discovery/ -q`; the hygiene test\n"
        "(`tests/discovery/test_fixture_hygiene.py`) guards against secrets and\n"
        "keeps the fixtures self-consistent.\n",
        encoding="utf-8",
    )
    print(f"[export] wrote {readme.relative_to(REPO_ROOT)}")
    print(f"[export] {len(exported)} fixture files ({stats['total']} signals in db)")


def _clear_generated(fixtures: Path = FIXTURES) -> None:
    """Drop stale exported fixtures before writing (ids shift between runs)."""
    for sub in ("signals", "triage_replies"):
        (fixtures / sub).mkdir(parents=True, exist_ok=True)
        for old in (fixtures / sub).glob("*.json"):
            old.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    if not args.db.is_file():
        raise SystemExit(f"[export] signals db not found: {args.db}")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        (n,) = con.execute("SELECT COUNT(*) FROM signals").fetchone()
        if not int(n):
            raise SystemExit("[export] signals.db holds no signals — nothing to export")
        (triaged,) = con.execute(
            "SELECT COUNT(*) FROM signals WHERE score IS NOT NULL"
        ).fetchone()
        if not int(triaged):
            raise SystemExit(
                "[export] no triaged signals in db — run a cycle that pre-qualifies"
                " at least one signal (refusing to write a partial fixture tree)"
            )
        stats = {"total": int(n)}
        _clear_generated()
        exported = []
        exported += export_signals(con)
        exported += export_triage_replies(con)
        write_readme(exported, stats)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
