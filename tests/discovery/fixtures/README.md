# Discovery regression fixtures (signal replay)

Real signals and triage results from live Trend Seeker discovery cycles
(`scripts/discovery/`), exported so the offline harness in this directory
can replay production payload shapes and decisions without a model or
network (**C3**). The tests run the SAME pure modules (`rules.py`,
`triage.py`) that the live engine uses.

## Origin

- **signals/** — one sanitized signal per row of `signals.db`, first
  exported 2026-08-05 from the live cycle that ran after the per-repo
  GitHub contributor capture landed (design §4.4 contributor_spike). The
  run collected 28 real signals (github 20, hn 3, pypi 3, pricing 2;
  reddit returned HTTP 429) and triaged 1: the Azure pricing page
  content-hash change (PRICING_DIFF, score 8, category `cloud`). The
  github repos that carried `contributors > 15` were 33-88 days old, so
  none met the `< 30d` freshness leg of contributor_spike — the rule is
  exercised end-to-end but requires a fresh repo to fire.
- **triage_replies/** — the triaged payloads: sanitized input signal +
  the machine-checked result the live engine produced. The store persists
  only the checked `{score, category, reason}`; the raw engine text is
  intentionally not kept (parse-level reply regressions stay covered by
  the canned-reply tests in `test_triage.py`, D6).
- **Raw payload fixtures** (top level) — collector `fetch()` inputs replayed
  through the fake opener: `github_search.json`, `github_contributors.json`,
  `hn_search.json`, `pypi.json`, `pypistats.json`, `pricing.html`,
  `pricing_changed.html`, `reddit_rss.xml`, and `hf_models.json` (added
  2026-08-06, mirrors the Hub `sort=trendingScore` response shape).

## Sanitization (C7)

Every exported signal drops its identity fields (`id`, `source_key`,
`url`, `research_slug`, `triggered_at`, `created_at`, `updated_at`) and
keeps only the per-source metric whitelist (`METRIC_WHITELIST` in the
exporter), plus a value-level guard that drops any metric containing a
URL or a secret-shaped token. Titles are public content consumed by the
rules and the triage prompt, so they are kept. See design §10.6.

## Refresh

```bash
python3 scripts/export_discovery_fixtures.py   # from repo root
```
The exporter refuses to run when `signals.db` is missing or empty. After
a refresh, re-run `uv run pytest tests/discovery/ -q`; the hygiene test
(`tests/discovery/test_fixture_hygiene.py`) guards against secrets and
keeps the fixtures self-consistent.
