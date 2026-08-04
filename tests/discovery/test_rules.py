"""M3: rule filters — pure, table-driven (design §4.4).

No I/O, no store: deltas are computed against a ``prior`` Signal that the
cycle passes in (the previous cycle's stored snapshot). All functions must
return None/False rather than raise when a delta cannot be computed (D6).
"""

from __future__ import annotations

from rules import (
    churn_phrases,
    contributor_spike,
    download_delta,
    engagement_ratio,
    noise_filters,
    pre_qualify,
    pricing_changed,
    star_acceleration,
)
from store import Signal

NOW = "2026-08-04T12:00:00+00:00"
_RECENT = "2026-07-15T10:00:00Z"  # 20 days before NOW
_OLD = "2026-01-01T10:00:00Z"


def _sig(source: str, title: str, **metrics) -> Signal:
    return Signal(source=source, source_key=title, title=title, metrics=metrics)


def _gh(stars: int, **extra) -> Signal:
    metrics = {"stars": stars, "created_at": _OLD, **extra}
    return _sig("github", "acme/vectordb", **metrics)


# -- noise filters ----------------------------------------------------------


def test_noise_filters_dotfiles():
    assert noise_filters(_sig("github", "user/.dotfiles", stars=100))
    assert noise_filters(_sig("github", ".vim", stars=50))


def test_noise_filters_curated_and_demo_repos():
    assert noise_filters(_sig("github", "jane/awesome-ai-storage", stars=100))
    assert noise_filters(_sig("github", "acme/demo-app", stars=100))
    assert noise_filters(_sig("github", "acme/tutorial-notes", stars=100))
    assert noise_filters(_sig("github", "acme/sample-project", stars=100))


def test_noise_filters_unengaged_posts():
    assert noise_filters(_sig("hn", "rant", points=0, num_comments=0))


def test_noise_filters_keeps_real_signals():
    assert not noise_filters(_gh(1200))
    assert not noise_filters(_sig("hn", "post", points=87, num_comments=12))
    # Reddit RSS has no scores: no evidence is not noise.
    assert not noise_filters(_sig("reddit", "post"))


# -- churn phrases ----------------------------------------------------------


def test_churn_phrases_match_design_patterns():
    assert churn_phrases("Migrating off Terraform after the license change")
    assert churn_phrases("Moving away from the old API")
    assert churn_phrases("Kubernetes is getting too expensive for small teams")
    assert churn_phrases("The legacy endpoint is deprecated")
    assert churn_phrases("Looking at alternatives to vendor X")
    assert churn_phrases("any alternatives to manage this?")


def test_churn_phrases_empty_on_plain_titles():
    assert churn_phrases("Postgres is eating the database world") == []
    assert churn_phrases("Show HN: A vector database in 500 lines") == []


def test_churn_phrases_also_searches_body():
    assert churn_phrases("plain title", "the service is too expensive now") == [
        "too expensive"
    ]


# -- engagement ratio -------------------------------------------------------


def test_engagement_ratio():
    assert engagement_ratio(321, 45) == 45 / 321
    assert engagement_ratio(0, 45) is None  # no points -> no ratio
    assert engagement_ratio(321, None) is None


# -- star acceleration ------------------------------------------------------


def test_star_acceleration_needs_a_baseline():
    assert star_acceleration(_gh(1200), None) is None  # first sighting
    prior = _gh(1000)
    assert star_acceleration(_sig("hn", "x", points=1), prior) is None  # wrong source
    no_stars = _sig("github", "acme/vectordb")
    assert star_acceleration(no_stars, prior) is None


def test_star_acceleration_rate_per_week():
    accel = star_acceleration(_gh(1400), _gh(1000), window_days=7)
    assert accel == 400 / 7


# -- contributor spike ------------------------------------------------------


def test_contributor_spike():
    assert contributor_spike(_gh(100, created_at=_RECENT, contributors=20), NOW)
    assert not contributor_spike(_gh(100, created_at=_RECENT, contributors=10), NOW)
    assert not contributor_spike(_gh(100, created_at=_OLD, contributors=20), NOW)
    # Current collectors do not expose contributors -> honest False (D6).
    assert not contributor_spike(_gh(100, created_at=_RECENT), NOW)


# -- download delta ---------------------------------------------------------


def test_download_delta():
    sig = _sig("pypi", "pgvector", downloads_last_week=8400)
    prior = _sig("pypi", "pgvector", downloads_last_week=7000)
    assert download_delta(sig, prior) == 1400
    assert download_delta(sig, None) is None  # first sighting
    # pypistats unavailable last cycle -> no baseline (D6).
    assert download_delta(sig, _sig("pypi", "pgvector")) is None


# -- pricing diff -----------------------------------------------------------


def test_pricing_changed():
    sig = _sig("pricing", "https://x", content_hash="B")
    prior = _sig("pricing", "https://x", content_hash="A")
    assert pricing_changed(sig, prior)
    assert not pricing_changed(sig, _sig("pricing", "https://x", content_hash="B"))
    assert not pricing_changed(sig, None)
    assert not pricing_changed(_sig("hn", "x"), prior)  # wrong source


# -- pre-qualify composition ------------------------------------------------


def test_pre_qualify_high_velocity_os():
    sig = _gh(1600, created_at=_RECENT)
    prior = _gh(100)
    assert pre_qualify(sig, prior, now=NOW) == ["HIGH_VELOCITY_OS"]


def test_pre_qualify_churn_signal_on_title():
    sig = _sig("reddit", "Migrating off Terraform after the license change")
    assert pre_qualify(sig, None, now=NOW) == ["CHURN_SIGNAL"]


def test_pre_qualify_pricing_diff():
    sig = _sig("pricing", "https://x", content_hash="B")
    prior = _sig("pricing", "https://x", content_hash="A")
    assert pre_qualify(sig, prior, now=NOW) == ["PRICING_DIFF"]


def test_pre_qualify_adoption_spike():
    sig = _sig("pypi", "pgvector", downloads_last_week=8400)
    prior = _sig("pypi", "pgvector", downloads_last_week=7000)
    assert pre_qualify(sig, prior, now=NOW) == ["ADOPTION_SPIKE"]
    # No growth -> no spike tag.
    same = _sig("pypi", "pgvector", downloads_last_week=7000)
    assert pre_qualify(same, prior, now=NOW) == []


def test_pre_qualify_empty_when_no_evidence():
    assert pre_qualify(_gh(1200), None, now=NOW) == []
