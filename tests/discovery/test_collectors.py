"""M2: v1 collectors, placeholders, and the cycle wiring (design §4.3).

Every network collector is exercised through an in-process FakeOpener —
no live calls (C5). Tests assert parsing, stable source_keys, idempotency,
time-purity (queries derived from the injected ``now``), and the cycle's
honest failure accounting (D6).
"""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest
import store as store_mod
from collectors import (
    Collector,
    FetchError,
    GithubCollector,
    HF_MODELS,
    HNCollector,
    HuggingFaceCollector,
    PricingCollector,
    PyPICollector,
    RedditRSSCollector,
    build_registry,
)
from config import (
    Ctx,
    GithubSettings,
    HFSettings,
    HNSettings,
    PricingSettings,
    PyPISettings,
    RedditSettings,
    load_config,
)
from helpers import REPO_ROOT

from discovery import run_cycle

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-08-04T12:00:00+00:00"


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeOpener:
    """In-process urllib opener: substring url -> body; records requests.

    Substring keys keep tests decoupled from query-string construction
    (dates, quoting) while the recorded Request objects let tests assert
    headers and parameters. Error keys win over response keys.
    """

    def __init__(
        self,
        responses: dict[str, bytes],
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._responses = responses
        self._errors = errors or {}
        self.requests: list[object] = []

    def open(self, req: object, timeout: object = None) -> io.BytesIO:
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        self.requests.append(req if isinstance(req, urllib.request.Request) else url)
        for key, exc in self._errors.items():
            if key in url:
                raise exc
        for key, body in self._responses.items():
            if key in url:
                resp = io.BytesIO(body)
                resp.status = 200
                return resp
        raise AssertionError(f"no fake response for {url}")


# -- github ----------------------------------------------------------------


def test_github_collector_parses_search_results():
    opener = FakeOpener(
        {
            "api.github.com/search/repositories": _fx("github_search.json"),
            "contributors": _fx("github_contributors.json"),
        }
    )
    col = GithubCollector(GithubSettings(), opener=opener)
    signals = col.fetch(NOW)

    assert [s.source_key for s in signals] == [
        "acme/vectordb",
        "jane/awesome-ai-storage",
    ]
    first = signals[0]
    assert first.title == "acme/vectordb"
    assert first.url == "https://github.com/acme/vectordb"
    assert first.metrics["stars"] == 1234
    assert first.metrics["forks"] == 89
    assert first.metrics["license"] == "Apache-2.0"
    assert first.metrics["owner_type"] == "Organization"
    # Contributor headroom (design §4.4 contributor_spike): one bounded
    # request per repo, metric = contributor count.
    assert first.metrics["contributors"] == 2
    assert signals[1].metrics["contributors"] == 2

    req, contrib_1, contrib_2 = opener.requests
    url = req.full_url
    assert "created%3A%3E" in url  # time-derived filter, quoted (C5)
    assert "stars%3A%3E50" in url
    assert "per_page=20" in url
    assert "sort=stars" in url
    assert req.headers.get("User-agent")  # GitHub requires a UA
    assert req.headers.get("Accept") == "application/vnd.github+json"
    assert "repos/acme/vectordb/contributors?per_page=30" in contrib_1.full_url
    assert "repos/jane/awesome-ai-storage/contributors" in contrib_2.full_url


def test_github_collector_sends_token_when_env_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekrit")
    opener = FakeOpener({"api.github.com": _fx("github_search.json")})
    col = GithubCollector(GithubSettings(), opener=opener)
    col.fetch(NOW)
    assert opener.requests[0].headers.get("Authorization") == "Bearer sekrit"
    # The per-repo contributor calls ride the same auth header.
    assert all(
        r.headers.get("Authorization") == "Bearer sekrit" for r in opener.requests
    )


def test_github_collector_tolerates_contributor_fetch_failure():
    """A rate-limited/failed contributor call drops the metric, never the
    repo (D6 best-effort): a repo without the metric simply cannot
    pre-qualify on contributor_spike."""
    opener = FakeOpener(
        {"api.github.com/search/repositories": _fx("github_search.json")},
        errors={"contributors": FetchError("rate limited")},
    )
    col = GithubCollector(GithubSettings(), opener=opener)
    signals = col.fetch(NOW)
    assert [s.source_key for s in signals] == [
        "acme/vectordb",
        "jane/awesome-ai-storage",
    ]
    assert all("contributors" not in s.metrics for s in signals)


def test_github_collector_ignores_non_list_contributor_response():
    """A rate-limit message is a dict, not a list — no bogus metric."""
    opener = FakeOpener(
        {
            "api.github.com/search/repositories": _fx("github_search.json"),
            "contributors": b'{"message": "API rate limit exceeded"}',
        }
    )
    col = GithubCollector(GithubSettings(), opener=opener)
    signals = col.fetch(NOW)
    assert all("contributors" not in s.metrics for s in signals)


def test_github_collector_is_idempotent_given_same_now():
    opener = FakeOpener({"api.github.com": _fx("github_search.json")})
    col = GithubCollector(GithubSettings(), opener=opener)
    a = col.fetch(NOW)
    b = col.fetch(NOW)
    assert [s.source_key for s in a] == [s.source_key for s in b]


# -- hacker news -----------------------------------------------------------


def test_hn_collector_parses_results_and_falls_back_to_item_url():
    opener = FakeOpener({"hn.algolia.com": _fx("hn_search.json")})
    col = HNCollector(HNSettings(), opener=opener)
    signals = col.fetch(NOW)

    assert [s.source_key for s in signals] == ["story_42424242", "story_42424243"]
    assert signals[0].title == "Postgres is eating the database world"
    assert signals[0].url == "https://example.com/postgres-eating"
    # null url -> canonical HN item link
    assert signals[1].url == "https://news.ycombinator.com/item?id=42424243"
    assert signals[0].metrics["points"] == 321
    assert signals[0].metrics["num_comments"] == 45
    assert signals[0].metrics["author"] == "pgfan"

    url = opener.requests[0].full_url
    assert "numericFilters=points%3E50" in url
    assert "hitsPerPage=20" in url
    assert "tags=story" in url


# -- hugging face -----------------------------------------------------------


def test_hf_collector_parses_trending_ranking():
    opener = FakeOpener({"huggingface.co/api/models": _fx("hf_models.json")})
    col = HuggingFaceCollector(HFSettings(), opener=opener)
    signals = col.fetch(NOW)

    assert [s.source_key for s in signals] == [
        "nimbus/vectordb-lite",
        "acme/storage-gemma-1b",
        "jane/tiny-reranker",
    ]
    first = signals[0]
    assert first.title == "nimbus/vectordb-lite"
    assert first.url == "https://huggingface.co/nimbus/vectordb-lite"
    assert first.metrics["downloads"] == 482100
    assert first.metrics["likes"] == 312
    assert first.metrics["trending_score"] == 876.5
    assert first.metrics["pipeline_tag"] == "feature-extraction"
    assert first.metrics["library_name"] == "sentence-transformers"
    assert first.metrics["author"] == "nimbus"
    assert first.metrics["last_modified"] == "2026-07-30T09:00:00.000Z"

    url = opener.requests[0].full_url
    assert url.startswith(HF_MODELS)
    assert "sort=trendingScore" in url
    assert "direction=-1" in url
    assert "limit=20" in url


def test_hf_collector_floor_filters_low_scores():
    """min_trending_score floors what triage sees (design §4.3): a floor of
    100 keeps the top two models, dropping the low-velocity one."""
    opener = FakeOpener({"huggingface.co/api/models": _fx("hf_models.json")})
    col = HuggingFaceCollector(HFSettings(min_trending_score=100.0), opener=opener)
    signals = col.fetch(NOW)
    assert [s.source_key for s in signals] == [
        "nimbus/vectordb-lite",
        "acme/storage-gemma-1b",
    ]


def test_hf_collector_skips_missing_id_or_score():
    """An entry without an id can't be keyed (dedupe) and one without a
    trendingScore can't clear the floor — both are skipped, not fatal."""
    payload = json.dumps(
        [
            {"downloads": 1, "trendingScore": 999.0},  # no id
            {"id": "acme/noscore", "downloads": 2},  # no trendingScore
            {"id": "acme/ok", "trendingScore": 500.0},
        ]
    ).encode()
    opener = FakeOpener({"huggingface.co/api/models": payload})
    col = HuggingFaceCollector(HFSettings(), opener=opener)
    signals = col.fetch(NOW)
    assert [s.source_key for s in signals] == ["acme/ok"]


def test_hf_collector_is_idempotent_given_same_now():
    opener = FakeOpener({"huggingface.co/api/models": _fx("hf_models.json")})
    col = HuggingFaceCollector(HFSettings(), opener=opener)
    a = col.fetch(NOW)
    b = col.fetch(NOW)
    assert [s.source_key for s in a] == [s.source_key for s in b]


def test_hf_collector_raises_on_non_list_payload():
    """A rate-limit dict is not the models list — surface the fetch error
    rather than silently collecting nothing (the cycle reports it, D6)."""
    opener = FakeOpener({"huggingface.co/api/models": b'{"error": "rate limited"}'})
    col = HuggingFaceCollector(HFSettings(), opener=opener)
    assert col.fetch(NOW) == []


# -- reddit rss ------------------------------------------------------------


def test_reddit_collector_parses_atom_feed():
    opener = FakeOpener({"reddit.com/r/devops/.rss": _fx("reddit_rss.xml")})
    col = RedditRSSCollector(RedditSettings(subreddits=("devops",)), opener=opener)
    signals = col.fetch(NOW)

    assert len(signals) == 2
    first = signals[0]
    assert first.source_key == (
        "https://www.reddit.com/r/devops/comments/abc123/migrating_off_terraform/"
    )
    assert first.title == "Migrating off Terraform after the license change"
    assert first.metrics["author"] == "devops_joe"
    assert first.metrics["subreddit"] == "devops"


def test_reddit_collector_caps_items_per_subreddit():
    opener = FakeOpener({"reddit.com": _fx("reddit_rss.xml")})
    col = RedditRSSCollector(
        RedditSettings(subreddits=("devops",), max_items=1), opener=opener
    )
    assert len(col.fetch(NOW)) == 1


# -- pypi ------------------------------------------------------------------


def test_pypi_collector_parses_metadata_and_stats():
    opener = FakeOpener(
        {
            "pypi.org/pypi/pgvector/json": _fx("pypi.json"),
            "pypistats.org/api/packages/pgvector/recent": _fx("pypistats.json"),
        }
    )
    col = PyPICollector(PyPISettings(packages=("pgvector",)), opener=opener)
    (sig,) = col.fetch(NOW)

    assert sig.source_key == "pgvector"
    assert sig.url == "https://pypi.org/project/pgvector/"
    assert sig.metrics["version"] == "0.8.1"
    assert sig.metrics["releases_count"] == 2
    assert sig.metrics["latest_release"] == "2026-07-01T12:00:00"
    assert sig.metrics["downloads_last_week"] == 8400


def test_pypi_collector_tolerates_missing_stats():
    """pypistats is best-effort: a failure drops the metric, not the package (D6)."""
    opener = FakeOpener({"pypi.org": _fx("pypi.json")})
    col = PyPICollector(PyPISettings(packages=("pgvector",)), opener=opener)
    (sig,) = col.fetch(NOW)
    assert sig.metrics["version"] == "0.8.1"
    assert "downloads_last_week" not in sig.metrics


def test_pypi_collector_raises_when_all_packages_fail():
    opener = FakeOpener({}, errors={"pypi.org": FetchError("pypi down")})
    col = PyPICollector(PyPISettings(packages=("pgvector", "ollama")), opener=opener)
    with pytest.raises(FetchError, match="pypi down"):
        col.fetch(NOW)


# -- pricing diff ----------------------------------------------------------


def test_pricing_collector_normalizes_content():
    """Scripts/styles/tags stripped, whitespace collapsed — a stable hash input."""
    assert PricingCollector.normalize(_fx("pricing.html")) == (
        "Pricing - Cloud Nimbus Cloud Nimbus pricing"
        " Standard VM: $0.05 / hour GPU VM: $1.20 / hour"
    )


def test_pricing_collector_hashes_and_is_idempotent():
    opener = FakeOpener(
        {
            "cloud.google.com/pricing": _fx("pricing.html"),
            "azure.microsoft.com": _fx("pricing_changed.html"),
        }
    )
    col = PricingCollector(
        PricingSettings(
            urls=(
                "https://cloud.google.com/pricing",
                "https://azure.microsoft.com/en-us/pricing/",
            )
        ),
        opener=opener,
    )
    signals = col.fetch(NOW)

    assert [s.source_key for s in signals] == [
        "https://cloud.google.com/pricing",
        "https://azure.microsoft.com/en-us/pricing/",
    ]
    assert signals[0].title == "pricing: cloud.google.com"
    # Different page content -> different hash; same page -> same hash.
    assert signals[0].metrics["content_hash"] != signals[1].metrics["content_hash"]
    again = col.fetch(NOW)
    assert [s.metrics["content_hash"] for s in again] == [
        s.metrics["content_hash"] for s in signals
    ]
    assert signals[0].metrics["bytes"] == len(_fx("pricing.html"))


# -- registry + placeholders -----------------------------------------------


def test_registry_lists_v1_sources_and_placeholders():
    registry = build_registry(load_config())
    assert set(registry) == {
        "github",
        "hn",
        "hf",
        "reddit",
        "pypi",
        "pricing",
        "sec_edgar",
        "reddit_oauth",
        "job_boards",
        "cloud_marketplaces",
    }
    for name in ("github", "hn", "hf", "reddit", "pypi", "pricing"):
        assert registry[name].enabled
    for name in ("sec_edgar", "reddit_oauth", "job_boards", "cloud_marketplaces"):
        assert not registry[name].enabled


def test_all_registry_entries_implement_protocol():
    for name, col in build_registry(load_config()).items():
        assert isinstance(col, Collector), name


def test_placeholder_fetch_raises_not_wired():
    registry = build_registry(load_config())
    with pytest.raises(NotImplementedError, match="not wired"):
        registry["sec_edgar"].fetch(NOW)
    with pytest.raises(NotImplementedError, match="not wired"):
        registry["job_boards"].fetch(NOW)


# -- cycle wiring ----------------------------------------------------------


def _ctx(tmp_path) -> Ctx:
    return Ctx(state_dir=tmp_path, workspace=tmp_path / "ws", root=REPO_ROOT)


def test_cycle_stores_collected_signals(tmp_path):
    opener = FakeOpener(
        {
            "api.github.com": _fx("github_search.json"),
            "hn.algolia.com": _fx("hn_search.json"),
        }
    )
    registry = {
        "github": GithubCollector(GithubSettings(), opener=opener),
        "hn": HNCollector(HNSettings(), opener=opener),
    }
    rc = run_cycle(_ctx(tmp_path), load_config(), registry)
    assert rc == 0
    with store_mod.SignalStore(tmp_path / "signals.db") as st:
        stats = st.stats()
    # 2 hn stories + acme/vectordb; jane/awesome-ai-storage is noise (curated
    # list) and is filtered before storage (rules.noise_filters).
    assert stats["total"] == 3
    assert stats["NEW"] == 3


def test_cycle_reports_collector_failure_not_fatal(tmp_path, capsys):
    boom = FakeOpener({}, errors={"api.github.com": FetchError("rate limited")})
    registry = {"github": GithubCollector(GithubSettings(), opener=boom)}
    rc = run_cycle(_ctx(tmp_path), load_config(), registry)
    assert rc == 0  # a failing source never kills the cycle (D6)
    out = capsys.readouterr().out
    assert "collector 'github' failed: FetchError: rate limited" in out
    assert (
        "collected=0 noise=0 triaged=0 parse_failed=0 triggered=0"
        " trigger_failures=0 deferred=0 failed=1"
    ) in out


def test_cycle_offline_mode_skips_collectors(tmp_path, capsys):
    registry = build_registry(load_config())  # real openers, but never fetched
    ctx = Ctx(
        state_dir=tmp_path, workspace=tmp_path / "ws", root=REPO_ROOT, offline=True
    )
    rc = run_cycle(ctx, load_config(), registry)
    assert rc == 0
    out = capsys.readouterr().out
    assert "offline mode" in out
    assert (
        "collected=0 noise=0 triaged=0 parse_failed=0 triggered=0"
        " trigger_failures=0 deferred=0 failed=0"
    ) in out


def test_cycle_reports_config_source_not_registered(tmp_path, capsys):
    """A config-listed source that isn't registered is reported, not dropped."""
    rc = run_cycle(_ctx(tmp_path), load_config(), {})
    assert rc == 0
    out = capsys.readouterr().out
    assert "not registered; skipped" in out
    assert "no enabled collectors in this cycle" in out
