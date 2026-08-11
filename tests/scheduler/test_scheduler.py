"""Tests for TaskScheduler — scheduling logic, lifecycle, and execution."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

from typing import Optional

import pytest

from openjarvis.scheduler.scheduler import ScheduledTask, TaskScheduler
from openjarvis.scheduler.store import SchedulerStore


@pytest.fixture()
def store(tmp_path):
    s = SchedulerStore(tmp_path / "scheduler_test.db")
    yield s
    s.close()


@pytest.fixture()
def scheduler(store):
    sched = TaskScheduler(store, poll_interval=1)
    yield sched
    sched.stop()


# -- ScheduledTask dataclass -------------------------------------------------


class TestScheduledTask:
    def test_round_trip(self):
        task = ScheduledTask(
            id="abc123",
            prompt="hello",
            schedule_type="interval",
            schedule_value="60",
            agent="orchestrator",
            tools="calculator,think",
            metadata={"key": "value"},
        )
        d = task.to_dict()
        restored = ScheduledTask.from_dict(d)
        assert restored.id == "abc123"
        assert restored.prompt == "hello"
        assert restored.schedule_type == "interval"
        assert restored.agent == "orchestrator"
        assert restored.tools == "calculator,think"
        assert restored.metadata == {"key": "value"}

    def test_defaults(self):
        task = ScheduledTask(
            id="x",
            prompt="p",
            schedule_type="once",
            schedule_value="2026-01-01T00:00:00",
        )
        assert task.context_mode == "isolated"
        assert task.status == "active"
        assert task.agent == "simple"
        assert task.tools == ""
        assert task.metadata == {}


# -- TaskScheduler create/list -----------------------------------------------


class TestCreateAndList:
    def test_create_task(self, scheduler):
        task = scheduler.create_task(
            prompt="hello world",
            schedule_type="interval",
            schedule_value="3600",
        )
        assert task.id
        assert task.prompt == "hello world"
        assert task.schedule_type == "interval"
        assert task.next_run is not None
        assert task.status == "active"

    def test_create_task_with_agent_and_tools(self, scheduler):
        task = scheduler.create_task(
            prompt="hello",
            schedule_type="once",
            schedule_value="2099-01-01T00:00:00+00:00",
            agent="orchestrator",
            tools="calculator,think",
        )
        assert task.agent == "orchestrator"
        assert task.tools == "calculator,think"

    def test_list_tasks_empty(self, scheduler):
        assert scheduler.list_tasks() == []

    def test_list_tasks(self, scheduler):
        scheduler.create_task("a", "interval", "60")
        scheduler.create_task("b", "interval", "120")
        assert len(scheduler.list_tasks()) == 2

    def test_list_tasks_filter_status(self, scheduler):
        t1 = scheduler.create_task("a", "interval", "60")
        scheduler.create_task("b", "interval", "120")
        scheduler.pause_task(t1.id)
        active = scheduler.list_tasks(status="active")
        paused = scheduler.list_tasks(status="paused")
        assert len(active) == 1
        assert len(paused) == 1


# -- Pause / resume / cancel -------------------------------------------------


class TestPauseResumeCancel:
    def test_pause_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.pause_task(task.id)
        tasks = scheduler.list_tasks(status="paused")
        assert len(tasks) == 1
        assert tasks[0].status == "paused"

    def test_resume_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.pause_task(task.id)
        scheduler.resume_task(task.id)
        tasks = scheduler.list_tasks(status="active")
        assert len(tasks) == 1

    def test_cancel_task(self, scheduler):
        task = scheduler.create_task("test", "interval", "60")
        scheduler.cancel_task(task.id)
        tasks = scheduler.list_tasks(status="cancelled")
        assert len(tasks) == 1
        assert tasks[0].next_run is None

    def test_pause_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.pause_task("nonexistent")

    def test_resume_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.resume_task("nonexistent")

    def test_cancel_nonexistent(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.cancel_task("nonexistent")


# -- _compute_next_run -------------------------------------------------------


class TestComputeNextRun:
    def test_interval(self, scheduler):
        task = ScheduledTask(
            id="t", prompt="p", schedule_type="interval", schedule_value="300"
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is not None
        # Should be roughly 300 seconds from now
        parsed = datetime.fromisoformat(next_run)
        diff = (parsed - datetime.now(timezone.utc)).total_seconds()
        assert 295 <= diff <= 310

    def test_once_not_yet_run(self, scheduler):
        target = "2099-06-15T12:00:00+00:00"
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="once",
            schedule_value=target,
            last_run=None,
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run == target

    def test_once_already_run(self, scheduler):
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="once",
            schedule_value="2099-06-15T12:00:00+00:00",
            last_run="2099-06-15T12:01:00+00:00",
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is None

    def test_cron_fallback(self, scheduler):
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="cron",
            schedule_value="30 2 * * *",
        )
        next_run = scheduler._compute_next_run(task)
        assert next_run is not None

    def test_unknown_type(self, scheduler):
        task = ScheduledTask(
            id="t", prompt="p", schedule_type="unknown", schedule_value="x"
        )
        assert scheduler._compute_next_run(task) is None


# -- cron fallback parser (no croniter) -------------------------------------


class TestComputeNextCron:
    def _next(self, expr: str, iso: str) -> Optional[str]:
        now = datetime.fromisoformat(iso)
        return TaskScheduler._compute_next_cron(expr, now)

    def test_step_hours_same_day(self):
        # "0 */6 * * *" from 16:36 → next tick 18:00 today
        assert self._next("0 */6 * * *", "2026-08-11T16:36:00+00:00") == (
            "2026-08-11T18:00:00+00:00"
        )

    def test_step_hours_rollover(self):
        # from 19:01 → next tick 00:00 tomorrow (6-hourly: 0,6,12,18)
        assert self._next("0 */6 * * *", "2026-08-11T19:01:00+00:00") == (
            "2026-08-12T00:00:00+00:00"
        )

    def test_hour_list_same_day(self):
        # "20 12,18 * * *" from 16:36 → next tick 18:20 today
        assert self._next("20 12,18 * * *", "2026-08-11T16:36:00+00:00") == (
            "2026-08-11T18:20:00+00:00"
        )

    def test_hour_list_rollover(self):
        # from 19:00 → next tick 12:20 tomorrow
        assert self._next("20 12,18 * * *", "2026-08-11T19:00:00+00:00") == (
            "2026-08-12T12:20:00+00:00"
        )

    def test_minute_hour_single(self):
        # "30 0 * * *" from 16:36 → next tick 00:30 tomorrow
        assert self._next("30 0 * * *", "2026-08-11T16:36:00+00:00") == (
            "2026-08-12T00:30:00+00:00"
        )

    def test_wildcard_every_minute(self):
        # "* * * * *" from 16:36:45 → next tick 16:37:00
        assert self._next("* * * * *", "2026-08-11T16:36:45+00:00") == (
            "2026-08-11T16:37:00+00:00"
        )

    def test_minute_step(self):
        # "*/15 * * * *" from 16:37 → next tick 16:45
        assert self._next("*/15 * * * *", "2026-08-11T16:37:00+00:00") == (
            "2026-08-11T16:45:00+00:00"
        )

    def test_invalid_field_degrades_to_one_hour(self):
        # Unparseable fields must not crash or produce a broken hourly loop;
        # documented degraded behavior is "+1 hour".
        assert self._next("bogus 2 * * *", "2026-08-11T16:36:00+00:00") == (
            "2026-08-11T17:36:00+00:00"
        )

    def test_short_expression_degrades_to_one_hour(self):
        assert self._next("0 * *", "2026-08-11T16:36:00+00:00") == (
            "2026-08-11T17:36:00+00:00"
        )


# -- _execute_task -----------------------------------------------------------


class TestExecuteTask:
    def test_execute_with_system(self, store):
        mock_system = MagicMock()
        mock_system.ask.return_value = "result text"
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task("what is 2+2?", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        mock_system.ask.assert_called_once()
        call_args = mock_system.ask.call_args
        assert call_args[0][0] == "what is 2+2?"

        # Check that a run log was recorded
        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 1
        assert logs[0]["result"] == "result text"

    def test_execute_with_dict_result_advances_next_run(self, store):
        # Regression: system.ask returns a dict; if the run log rejects it
        # (SQLite ProgrammingError) the task's next_run never advances, so the
        # daemon refires a due cron task every poll. The run must persist and
        # next_run must move past "now" so the task is no longer due.
        from datetime import datetime, timezone

        mock_system = MagicMock()
        mock_system.ask.return_value = {
            "content": "collected=48 triaged=6",
            "usage": {"tokens": 12},
        }
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task("run cycle", "cron", "0 * * * *")
        # Force the task to be due right now (the refire scenario).
        d = store.get_task(task.id)
        d["next_run"] = "2020-01-01T00:00:00+00:00"
        store.update_task(d)

        sched._execute_task(ScheduledTask.from_dict(d))

        # Run log persisted the dict as JSON text.
        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 1
        assert '"collected=48 triaged=6"' in logs[0]["result"]

        # Task state advanced: last_run set, next_run in the future.
        updated = store.get_task(task.id)
        assert updated["last_run"] is not None
        next_run = datetime.fromisoformat(updated["next_run"])
        assert next_run > datetime.now(timezone.utc)

        # Not due anymore → the daemon will not refire on the next poll.
        due = store.get_due_tasks(datetime.now(timezone.utc).isoformat())
        assert task.id not in [t["id"] for t in due]

    def test_error_cooldown_skips_repeat_attempts(self, store):
        # Hardening: if a task keeps raising (e.g. store write failure), the
        # daemon must not refire it every poll. After the first failure the
        # task is skipped until the cooldown elapses.
        mock_system = MagicMock()
        mock_system.ask.side_effect = RuntimeError("boom")
        sched = TaskScheduler(
            store, system=mock_system, poll_interval=1, error_cooldown=3600
        )

        task = sched.create_task("fail", "cron", "0 * * * *")
        sched._execute_task(task)
        sched._execute_task(task)

        assert mock_system.ask.call_count == 1

    def test_execute_without_system(self, store):
        sched = TaskScheduler(store, poll_interval=1)
        task = sched.create_task("dry run", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 1
        assert "dry-run" in logs[0]["result"]

    def test_execute_with_error(self, store):
        mock_system = MagicMock()
        mock_system.ask.side_effect = RuntimeError("engine down")
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task("fail", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        logs = store.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["success"] == 0
        assert "engine down" in logs[0]["error"]

    def test_execute_publishes_events(self, store):
        mock_bus = MagicMock()
        sched = TaskScheduler(store, bus=mock_bus, poll_interval=1)
        task = sched.create_task("test", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        assert mock_bus.publish.call_count == 2
        start_call = mock_bus.publish.call_args_list[0]
        end_call = mock_bus.publish.call_args_list[1]
        assert start_call[0][0] == "scheduler_task_start"
        assert end_call[0][0] == "scheduler_task_end"

    def test_execute_once_task_completed_after_run(self, store):
        sched = TaskScheduler(store, poll_interval=1)
        task = sched.create_task("one-shot", "once", "2026-01-01T00:00:00+00:00")
        sched._execute_task(task)

        updated = store.get_task(task.id)
        assert updated["status"] == "completed"
        assert updated["next_run"] is None

    def test_execute_with_tools(self, store):
        mock_system = MagicMock()
        mock_system.ask.return_value = "4"
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task(
            "what is 2+2?",
            "once",
            "2026-01-01T00:00:00+00:00",
            tools="calculator,think",
        )
        sched._execute_task(task)

        call_kwargs = mock_system.ask.call_args[1]
        assert call_kwargs["tools"] == ["calculator", "think"]

    def test_execute_headless_auto_approves_sensitive_tools(self, store):
        """The daemon runs unattended (design §4.8): system.ask must receive
        interactive=True + an auto-approving confirm_callback so sensitive
        tools (shell_exec) execute without a TTY."""
        mock_system = MagicMock()
        mock_system.ask.return_value = "ran"
        sched = TaskScheduler(store, system=mock_system, poll_interval=1)

        task = sched.create_task(
            "run cycle", "once", "2026-01-01T00:00:00+00:00", tools="shell_exec"
        )
        sched._execute_task(task)

        call_kwargs = mock_system.ask.call_args[1]
        assert call_kwargs["interactive"] is True
        cb = call_kwargs["confirm_callback"]
        assert callable(cb)
        assert cb("Allow execution of tool 'shell_exec' with args {...}?") is True


# -- Start / stop lifecycle ---------------------------------------------------


class TestLifecycle:
    def test_start_stop(self, scheduler):
        scheduler.start()
        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()
        scheduler.stop()
        assert not scheduler._thread

    def test_double_start(self, scheduler):
        scheduler.start()
        t1 = scheduler._thread
        scheduler.start()  # Should not create a second thread
        assert scheduler._thread is t1
        scheduler.stop()

    def test_poll_loop_finds_due_tasks(self, store):
        sched = TaskScheduler(store, poll_interval=1)
        # Create a task that is already due
        task = sched.create_task("immediate", "once", "2020-01-01T00:00:00+00:00")
        # Manually set next_run to the past
        d = store.get_task(task.id)
        d["next_run"] = "2020-01-01T00:00:00+00:00"
        store.update_task(d)

        sched.start()
        # Give the poll loop time to execute
        time.sleep(2.5)
        sched.stop()

        logs = store.get_run_logs(task.id)
        assert len(logs) >= 1
