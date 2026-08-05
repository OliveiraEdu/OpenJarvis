"""M4: LLM triage — parse-and-clamp, prompt rendering, and the cycle wiring
(design §4.6). Every engine interaction goes through an injected fake ask; no
test touches the engine (C5). A triage reply that fails the JSON contract
scores 0 with ``triage_reason="parse_failed"`` instead of crashing (D6).
"""

from __future__ import annotations

import json
from typing import Callable

from config import Ctx, load_config
from helpers import REPO_ROOT
from store import Signal, SignalStore
from triage import (
    CATEGORIES,
    PARSE_FAILED,
    Triage,
    _extract_json,
    parse_reply,
    render_prompt,
    triage_signal,
)

from discovery import run_cycle

GOOD_REPLY = '{"relevance_score": 9, "category": "db", "reason": "credible momentum"}'


def _sig(**overrides) -> Signal:
    fields = dict(
        source="hn",
        source_key="story_1",
        title="Migrating off Terraform after the license change",
        metrics={"points": 200, "num_comments": 40},
    )
    fields.update(overrides)
    return Signal(**fields)


def _ctx(tmp_path) -> Ctx:
    return Ctx(state_dir=tmp_path, workspace=tmp_path / "ws", root=REPO_ROOT)


class FakeAsk:
    """Records prompts; returns a canned reply (the injectable seam, C5)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, ctx: Ctx, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


# -- prompt rendering (C2) ---------------------------------------------------


def test_render_prompt_is_single_line():
    rendered = render_prompt(_sig())
    assert "\n" not in rendered
    assert "\r" not in rendered


def test_render_prompt_substitutes_every_placeholder():
    rendered = render_prompt(_sig())
    assert "${" not in rendered  # no leftover template placeholders
    assert "hn" in rendered
    assert "Migrating off Terraform" in rendered
    assert '"points": 200' in rendered
    # The category hint enumerates the enum, so the model is taught exactly
    # the categories the code accepts (D5).
    assert all(cat in rendered for cat in CATEGORIES)


def test_render_prompt_survives_braces_and_quotes_in_metrics():
    sig = _sig(metrics={"note": 'say "hi" {x}', "n": 1})
    rendered = render_prompt(sig)
    assert "{x}" in rendered  # braces inside metric values are not placeholders
    assert "say" in rendered
    assert "\n" not in rendered


# -- JSON extraction ----------------------------------------------------------


def test_extract_json_ignores_braces_inside_strings():
    text = 'the payload {"relevance_score": 7, "reason": "use {brace}"} done'
    assert json.loads(_extract_json(text))["reason"] == "use {brace}"


def test_extract_json_takes_first_balanced_block():
    assert json.loads(_extract_json('{"a": 1} trailing {"b": 2}')) == {"a": 1}


def test_extract_json_none_when_missing_or_unbalanced():
    assert _extract_json("no braces at all") is None
    assert _extract_json("unclosed {") is None


# -- parse-and-clamp (D6) -----------------------------------------------------


def test_parse_reply_valid_contract():
    assert parse_reply(GOOD_REPLY) == Triage(9, "db", "credible momentum")


def test_parse_reply_ignores_surrounding_prose():
    tri = parse_reply(f"Sure! Here you go:\n{GOOD_REPLY}\nHope that helps.")
    assert tri == Triage(9, "db", "credible momentum")


def test_parse_reply_clamps_score_into_1_10():
    assert (
        parse_reply('{"relevance_score": 42, "category": "db", "reason": "x"}').score
        == 10
    )
    assert (
        parse_reply('{"relevance_score": 0, "category": "db", "reason": "x"}').score
        == 1
    )
    assert (
        parse_reply('{"relevance_score": -5, "category": "db", "reason": "x"}').score
        == 1
    )


def test_parse_reply_rejects_bool_score():
    tri = parse_reply('{"relevance_score": true, "category": "db", "reason": "x"}')
    assert (tri.score, tri.reason) == (0, PARSE_FAILED)


def test_parse_reply_coerces_category_case():
    assert (
        parse_reply('{"relevance_score": 8, "category": "DB", "reason": "x"}').category
        == "db"
    )
    assert (
        parse_reply(
            '{"relevance_score": 8, "category": "Unknown", "reason": "x"}'
        ).category
        == "unknown"
    )


def test_parse_reply_rejects_unknown_category():
    tri = parse_reply('{"relevance_score": 8, "category": "banana", "reason": "x"}')
    assert tri.reason == PARSE_FAILED


def test_parse_reply_truncates_reason_to_40_chars():
    tri = parse_reply(
        '{"relevance_score": 8, "category": "db", "reason": "%s"}' % ("x" * 80)
    )
    assert len(tri.reason) == 40
    assert tri.reason == "x" * 40


def test_parse_reply_missing_required_fields_is_parse_failed():
    assert parse_reply('{"category": "db", "reason": "x"}').reason == PARSE_FAILED
    assert parse_reply('{"relevance_score": 8, "reason": "x"}').reason == PARSE_FAILED


def test_parse_reply_garbage_is_parse_failed():
    assert parse_reply("no json here").reason == PARSE_FAILED
    assert parse_reply("{not valid json").reason == PARSE_FAILED


# -- triage_signal through the seam -------------------------------------------


def test_triage_signal_parses_bare_json_reply(tmp_path):
    ask = FakeAsk(GOOD_REPLY)
    verdict = triage_signal(_ctx(tmp_path), _sig(), ask=ask)
    assert verdict == Triage(9, "db", "credible momentum")
    # The rendered prompt reached the engine with the signal's context.
    assert "Migrating off Terraform" in ask.prompts[0]


def test_triage_signal_parses_engine_envelope(tmp_path):
    envelope = json.dumps({"content": GOOD_REPLY, "turns": 1, "tool_results": []})
    verdict = triage_signal(_ctx(tmp_path), _sig(), ask=FakeAsk(envelope))
    assert verdict == Triage(9, "db", "credible momentum")


def test_triage_signal_non_string_content_degrades_honestly(tmp_path):
    envelope = json.dumps({"content": None, "turns": 0, "tool_results": []})
    verdict = triage_signal(_ctx(tmp_path), _sig(), ask=FakeAsk(envelope))
    assert verdict == Triage(0, "", PARSE_FAILED)


def test_triage_signal_garbage_reply_is_parse_failed(tmp_path):
    verdict = triage_signal(_ctx(tmp_path), _sig(), ask=FakeAsk("I refuse to JSON"))
    assert verdict == Triage(0, "", PARSE_FAILED)


# -- cycle wiring (triage stage) ----------------------------------------------


class FakeCollector:
    """Minimal Collector (name/enabled/fetch) for cycle tests."""

    name = "hn"
    enabled = True

    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    def fetch(self, now: str) -> list[Signal]:
        return list(self._signals)


class FakeRunner:
    """Minimal trigger seam for cycle tests (C5): records topics, returns a
    canned slug so no research.sh is ever spawned."""

    def __init__(self, slug: str = "fake-slug") -> None:
        self.slug = slug
        self.topics: list[str] = []

    def __call__(self, ctx: Ctx, topic: str) -> str:
        self.topics.append(topic)
        return self.slug


def _cycle(
    tmp_path,
    signals,
    *,
    offline: bool = False,
    ask: Callable | None = None,
    runner: Callable | None = None,
):
    ctx = Ctx(
        state_dir=tmp_path,
        workspace=tmp_path / "ws",
        root=REPO_ROOT,
        offline=offline,
    )
    rc = run_cycle(
        ctx,
        load_config(),
        {"hn": FakeCollector(signals)},
        triage_ask=ask,
        trigger_runner=runner or FakeRunner(),
    )
    return ctx, rc


def test_cycle_triages_pre_qualified_new_signal(tmp_path):
    ask = FakeAsk(GOOD_REPLY)
    runner = FakeRunner()
    ctx, rc = _cycle(tmp_path, [_sig()], ask=ask, runner=runner)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        # Score 9 >= threshold 7 -> the decide stage triggers the deep-dive;
        # the fake runner records it and the row lands DONE.
        (row,) = st.list_by_status("DONE")
        stats = st.stats()
    assert row.score == 9
    assert row.category == "db"
    assert row.triage_reason == "credible momentum"
    assert stats["NEW"] == 0
    assert stats["TRIAGED"] == 0
    assert stats["DONE"] == 1
    assert len(ask.prompts) == 1
    assert len(runner.topics) == 1


def test_cycle_marks_parse_failed_verdict_honestly(tmp_path):
    ask = FakeAsk("not json at all")
    ctx, rc = _cycle(tmp_path, [_sig()], ask=ask)
    assert rc == 0  # a bad engine reply never kills the cycle (D6)
    with SignalStore(tmp_path / "signals.db") as st:
        (row,) = st.list_by_status("TRIAGED")
    assert row.score == 0
    assert row.triage_reason == PARSE_FAILED


def test_cycle_skips_triage_for_unqualified_signals(tmp_path):
    ask = FakeAsk(GOOD_REPLY)
    sig = _sig(title="Postgres is eating the database world")  # no rule tag
    ctx, rc = _cycle(tmp_path, [sig], ask=ask)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        assert st.count_by_status("NEW") == 1
        assert st.count_by_status("TRIAGED") == 0
    assert ask.prompts == []  # the engine is never consulted


def test_cycle_offline_skips_triage_entirely(tmp_path):
    def boom(ctx: Ctx, prompt: str) -> str:
        raise AssertionError("engine must not run in offline mode")

    sig = _sig()
    sig.pre_qualify = "CHURN_SIGNAL"
    with SignalStore(tmp_path / "signals.db") as st:
        st.upsert(sig)  # a pre-qualified NEW signal already in the store
    ctx, rc = _cycle(tmp_path, [], offline=True, ask=boom)
    assert rc == 0
    with SignalStore(tmp_path / "signals.db") as st:
        assert st.count_by_status("NEW") == 1
        assert st.count_by_status("TRIAGED") == 0
