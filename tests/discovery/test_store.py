"""signals.db store: schema, dedupe, status transitions, stats (design §4.5)."""

from __future__ import annotations

import pytest

from store import SCHEMA_SQL, Signal, SignalStore, VALID_STATUSES


@pytest.fixture()
def store(tmp_path):
    s = SignalStore(tmp_path / "signals.db")
    yield s
    s.close()


def make_signal(**overrides) -> Signal:
    fields = dict(
        source="github",
        source_key="owner/repo",
        title="A repo that appeared",
        url="https://github.com/owner/repo",
        metrics={"stars": 500, "forks": 30},
        pre_qualify="HIGH_VELOCITY_OS",
    )
    fields.update(overrides)
    return Signal(**fields)


def test_schema_creates_and_stats_start_zero(store):
    assert "CREATE TABLE IF NOT EXISTS signals" in SCHEMA_SQL
    assert store.stats() == {
        "NEW": 0,
        "TRIAGED": 0,
        "TRIGGERED": 0,
        "DONE": 0,
        "FAILED": 0,
        "total": 0,
    }


def test_upsert_inserts_then_dedupes(store):
    inserted, _id = store.upsert(make_signal())
    assert inserted is True
    inserted_again, same_id = store.upsert(
        make_signal(title="A repo that appeared (updated)", metrics={"stars": 600})
    )
    assert inserted_again is False
    assert same_id == _id
    assert store.stats()["total"] == 1
    got = store.get("github", "owner/repo")
    assert got is not None
    assert got.title == "A repo that appeared (updated)"
    assert got.metrics == {"stars": 600}
    assert got.pre_qualify == "HIGH_VELOCITY_OS"


def test_upsert_refresh_preserves_status(store):
    store.upsert(make_signal())
    store.set_status(
        store.get("github", "owner/repo").id,
        "TRIAGED",
        score=8,
        category="db",
        triage_reason="high velocity",
    )
    store.upsert(make_signal(metrics={"stars": 900}))
    got = store.get("github", "owner/repo")
    assert got.status == "TRIAGED"  # re-triage is a rules/decide concern (M3)
    assert got.score == 8
    assert got.metrics == {"stars": 900}


def test_distinct_source_keys_do_not_dedupe(store):
    store.upsert(make_signal())
    store.upsert(make_signal(source_key="other/repo"))
    assert store.stats()["total"] == 2


def test_status_transition_records_metadata(store):
    store.upsert(make_signal())
    sid = store.get("github", "owner/repo").id
    store.set_status(
        sid,
        "TRIGGERED",
        research_slug="subject-a-new-repo-",
        triggered_at="2026-08-04T12:00:00+00:00",
    )
    store.set_status(sid, "DONE")
    sig = store.get("github", "owner/repo")
    assert sig.status == "DONE"
    assert sig.research_slug == "subject-a-new-repo-"
    assert sig.triggered_at == "2026-08-04T12:00:00+00:00"


def test_invalid_status_rejected(store):
    store.upsert(make_signal())
    sid = store.get("github", "owner/repo").id
    with pytest.raises(ValueError, match="invalid status"):
        store.set_status(sid, "NOPE")
    with pytest.raises(ValueError, match="invalid status"):
        store.count_by_status("NOPE")
    with pytest.raises(ValueError, match="invalid status"):
        store.list_by_status("NOPE")


def test_list_and_count_by_status(store):
    store.upsert(make_signal(source_key="a"))
    store.upsert(make_signal(source_key="b"))
    store.upsert(make_signal(source_key="c"))
    done_id = store.get("github", "a").id
    store.set_status(done_id, "DONE")
    assert store.count_by_status("NEW") == 2
    assert store.count_by_status("DONE") == 1
    assert [s.source_key for s in store.list_by_status("NEW")] == ["b", "c"]
    assert store.stats()["total"] == 3
    assert store.stats()["DONE"] == 1


def test_metrics_roundtrip_nested(store):
    metrics = {"phrases": ["migrating off X"], "delta": {"stars": 12.5}}
    store.upsert(make_signal(metrics=metrics))
    got = store.get("github", "owner/repo")
    assert got.metrics == metrics


def test_valid_statuses_exhaustive():
    assert VALID_STATUSES == {"NEW", "TRIAGED", "TRIGGERED", "DONE", "FAILED"}
