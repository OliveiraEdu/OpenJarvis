"""M5: trigger stage — decide wiring, the research.sh seam (C4), and the
re-triage delta, end to end (design §4.7).

The pure seam pieces (subject_topic, slugify, research_cmd) are tested
directly; slugify is pinned to the launcher's ACTUAL slug derivation via the
bash seam (C4). Cycle-level tests inject a fake ask and a fake runner — no
engine, no research.sh spawn (C5). A raised runner records FAILED with a
reason, never a silent skip (D6).
"""

from __future__ import annotations

import subprocess

from config import Ctx, DiscoveryConfig
from helpers import REPO_ROOT
from store import Signal, SignalStore
from trigger import launch_research, research_cmd, slugify, subject_topic

from discovery import run_cycle

GOOD_REPLY = '{"relevance_score": 9, "category": "db", "reason": "credible momentum"}'
LOW_REPLY = '{"relevance_score": 5, "category": "db", "reason": "routine announcement"}'


def _sig(**overrides) -> Signal:
    fields = dict(
        source="github",
        source_key="acme/vectordb",
        title="Migrating off Terraform after the license change",
        metrics={"stars": 150, "forks": 20},
    )
    fields.update(overrides)
    return Signal(**fields)


class FakeAsk:
    """Canned engine replies, one per call (order matters in re-triage tests)."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, ctx: Ctx, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0)


class FakeRunner:
    """Records topics; returns a canned slug (the trigger seam, C5)."""

    def __init__(self, slug: str = "fake-slug") -> None:
        self.slug = slug
        self.topics: list[str] = []

    def __call__(self, ctx: Ctx, topic: str) -> str:
        self.topics.append(topic)
        return self.slug


class FakeCollector:
    name = "github"
    enabled = True

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    def fetch(self, now: str) -> list[Signal]:
        return list(self._signals)


def _ctx(tmp_path) -> Ctx:
    return Ctx(state_dir=tmp_path, workspace=tmp_path / "ws", root=REPO_ROOT)


def _cfg(**overrides) -> DiscoveryConfig:
    base = dict(
        threshold=7,
        max_triggers_per_day=3,
        subject_template="{title} | Scope: {category}",
        re_triage_delta=0.3,
        cooldown_seconds={"hn": 43200, "github": 86400},
        enabled_collectors=("github",),
    )
    base.update(overrides)
    return DiscoveryConfig(**base)


def _cycle(tmp_path, cfg, signals, *, ask, runner, offline: bool = False):
    ctx = Ctx(
        state_dir=tmp_path,
        workspace=tmp_path / "ws",
        root=REPO_ROOT,
        offline=offline,
    )
    rc = run_cycle(
        ctx,
        cfg,
        {"github": FakeCollector(signals)},
        triage_ask=ask,
        trigger_runner=runner,
    )
    return ctx, rc


# -- the seam: pure pieces (C4) -----------------------------------------------


def test_subject_topic_substitutes_signal_fields():
    sig = _sig(title="Postgres eats the DB world", metrics={}, category="db")
    assert subject_topic(sig, "{title} | Scope: {category}") == (
        "Postgres eats the DB world | Scope: db"
    )


def test_slugify_mirrors_research_sh_rule():
    # research.sh:63-65 — lowercase, runs of non-alnum to '-', trim, cut 40.
    assert slugify("Postgres is Eating the Database World!") == (
        "postgres-is-eating-the-database-world"
    )
    assert slugify("  --Mixed  Case--  ") == "mixed-case"


def test_slugify_fallback_and_cut():
    assert slugify("!!!") == "research"  # research.sh's empty-slug fallback
    assert len(slugify("x" * 80)) == 40


def test_slug_contract_matches_research_sh_seam():
    """C4: our python slugify == the launcher's ACTUAL slug derivation (run
    through the bash seam, like the pipeline's seam tests)."""
    topic = "Migrating off Terraform | Scope: infra"
    derived = subprocess.run(
        [
            "bash",
            "-c",
            "slug=$(printf '%s' \"$1\" | tr '[:upper:]' '[:lower:]'"
            " | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//' | cut -c1-40);"
            ' [ -n "$slug" ] || slug=research; printf \'%s\' "$slug"',
            "slug-test",
            topic,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert slugify(topic) == derived


def test_research_cmd_shape(tmp_path):
    cmd = research_cmd(_ctx(tmp_path), "a topic")
    assert cmd == [
        "bash",
        str(REPO_ROOT / "scripts" / "research.sh"),
        "a topic",
    ]


def test_launch_research_is_callable_seam():
    """The production seam exists and returns the topic's slug on success —
    signature only here; the live path is the live-marked smoke."""
    assert callable(launch_research)


# -- decide wiring in the cycle -----------------------------------------------


def test_cycle_triggers_and_records_done(tmp_path):
    ask = FakeAsk(GOOD_REPLY)
    runner = FakeRunner(slug="migrating-off-terraform")
    cfg = _cfg()
    ctx, rc = _cycle(tmp_path, cfg, [_sig()], ask=ask, runner=runner)
    assert rc == 0

    with SignalStore(tmp_path / "signals.db") as st:
        (row,) = st.list_by_status("DONE")
        stats = st.stats()
    assert row.score == 9
    assert row.category == "db"
    # research_slug is OUR derived slug (slugify of the subject topic), not the
    # runner's return value — pinned to the research.sh truncation rule (C4).
    assert row.research_slug == "migrating-off-terraform-after-the-licens"
    assert row.triggered_at  # set at trigger time (cooldown baseline)
    assert stats["DONE"] == 1
    assert stats["TRIGGERED"] == 0
    assert runner.topics == [
        "Migrating off Terraform after the license change | Scope: db"
    ]


def test_cycle_records_failed_trigger_honestly(tmp_path):
    class Boom:
        def __call__(self, ctx: Ctx, topic: str) -> str:
            raise RuntimeError("engine down")

    ask = FakeAsk(GOOD_REPLY)
    ctx, rc = _cycle(tmp_path, _cfg(), [_sig()], ask=ask, runner=Boom())
    assert rc == 0  # a failed deep-dive never kills the cycle (D6)

    with SignalStore(tmp_path / "signals.db") as st:
        (row,) = st.list_by_status("FAILED")
    assert row.research_slug  # still recorded for trace linkage
    assert row.triggered_at  # cooldown applies after a failed trigger too
    assert row.triage_reason.startswith("trigger_failed: RuntimeError: engine down")


def test_cycle_skips_below_threshold(tmp_path):
    ask = FakeAsk(LOW_REPLY)  # score 5 < threshold 7
    runner = FakeRunner()
    ctx, rc = _cycle(tmp_path, _cfg(), [_sig()], ask=ask, runner=runner)
    assert rc == 0

    with SignalStore(tmp_path / "signals.db") as st:
        (row,) = st.list_by_status("TRIAGED")
    assert row.score == 5
    assert runner.topics == []  # never triggered


def test_cycle_defers_during_cooldown(tmp_path):
    sig = _sig()
    sig.pre_qualify = "CHURN_SIGNAL"
    sig.score = 9
    sig.category = "db"
    sig.status = "TRIAGED"
    sig.triggered_at = "2099-01-01T00:00:00+00:00"  # far in the future
    with SignalStore(tmp_path / "signals.db") as st:
        st.upsert(sig)

    runner = FakeRunner()
    ctx, rc = _cycle(tmp_path, _cfg(), [], ask=FakeAsk(), runner=runner)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        (row,) = st.list_by_status("TRIAGED")
        assert st.count_by_status("DONE") == 0
    assert row.score == 9  # verdict: DEFER — retry next cycle
    assert runner.topics == []


def test_cycle_daily_cap_defers_second_trigger(tmp_path):
    for key in ("a", "b"):
        sig = _sig(source_key=key)
        sig.pre_qualify = "CHURN_SIGNAL"
        sig.score = 9
        sig.category = "db"
        sig.status = "TRIAGED"
        with SignalStore(tmp_path / "signals.db") as st:
            st.upsert(sig)

    runner = FakeRunner()
    cfg = _cfg(max_triggers_per_day=1)
    ctx, rc = _cycle(tmp_path, cfg, [], ask=FakeAsk(), runner=runner)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        assert st.count_by_status("DONE") == 1
        assert st.count_by_status("TRIAGED") == 1  # capped -> DEFER
    assert len(runner.topics) == 1


def test_cycle_offline_never_triggers(tmp_path):
    def boom(ctx: Ctx, topic: str) -> str:
        raise AssertionError("runner must not run offline")

    sig = _sig()
    sig.pre_qualify = "CHURN_SIGNAL"
    sig.score = 9
    sig.category = "db"
    sig.status = "TRIAGED"
    with SignalStore(tmp_path / "signals.db") as st:
        st.upsert(sig)

    ctx, rc = _cycle(tmp_path, _cfg(), [], ask=FakeAsk(), runner=boom, offline=True)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        assert st.count_by_status("TRIAGED") == 1  # untouched
        assert st.count_by_status("DONE") == 0


def test_re_triage_reopens_and_re_triggers_next_cycle(tmp_path):
    """Cycle 1 SKIPs (score 5). Cycle 2 sees stars +50% (> 0.3 delta) ->
    re-open -> re-triage (score 9) -> trigger -> DONE (design §4.7)."""
    ask = FakeAsk(LOW_REPLY, GOOD_REPLY)
    runner = FakeRunner()
    cfg = _cfg()

    sig1 = _sig(metrics={"stars": 100, "forks": 20})
    ctx, rc = _cycle(tmp_path, cfg, [sig1], ask=ask, runner=runner)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        assert st.count_by_status("TRIAGED") == 1  # SKIP'd, not triggered

    sig2 = _sig(metrics={"stars": 150, "forks": 20})  # +50% growth
    ctx, rc = _cycle(tmp_path, cfg, [sig2], ask=ask, runner=runner)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        stats = st.stats()
        (row,) = st.list_by_status("DONE")
    assert stats["DONE"] == 1
    assert row.score == 9  # re-scored by the second triage pass
    assert len(ask.prompts) == 2  # triaged once per cycle
    assert len(runner.topics) == 1  # triggered exactly once (cycle 2)


# -- store: daily-cap input ----------------------------------------------------


def test_count_triggered_today_counts_utc_day(tmp_path):
    with SignalStore(tmp_path / "signals.db") as st:
        for key in ("a", "b"):
            inserted, sid = st.upsert(_sig(source_key=key))
            assert inserted
            st.set_status(sid, "TRIGGERED", triggered_at="2026-08-04T09:00:00+00:00")
        inserted, sid = st.upsert(_sig(source_key="c"))
        assert inserted
        st.set_status(sid, "DONE", triggered_at="2026-08-04T22:00:00+00:00")
        inserted, sid = st.upsert(_sig(source_key="d"))
        assert inserted
        st.set_status(sid, "TRIGGERED", triggered_at="2026-08-03T23:00:00+00:00")
        # 2026-08-04: two TRIGGERED + one DONE (all triggered that UTC day).
        assert st.count_triggered_today("2026-08-04T12:00:00+00:00") == 3
        assert st.count_triggered_today("2026-08-03T12:00:00+00:00") == 1
