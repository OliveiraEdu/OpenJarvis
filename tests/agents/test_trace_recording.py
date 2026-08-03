"""Tests for trace recording in AgentExecutor."""

from __future__ import annotations

from unittest.mock import patch

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.executor import AgentExecutor
from openjarvis.agents.manager import AgentManager
from openjarvis.core.events import EventBus, EventType
from openjarvis.traces.store import TraceStore


def test_executor_records_trace(tmp_path):
    """execute_tick records a trace with tool call + respond steps."""
    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus, trace_store=trace_store)

    agent = mgr.create_agent("trace-test")

    def fake_invoke(agent_dict):
        bus.publish(
            EventType.TOOL_CALL_START,
            {
                "agent": agent_dict["id"],
                "tool": "web_search",
                "arguments": {"query": "test"},
            },
        )
        bus.publish(
            EventType.TOOL_CALL_END,
            {
                "agent": agent_dict["id"],
                "tool": "web_search",
                "result": "search results...",
                "latency": 0.5,
                "success": True,
            },
        )
        return AgentResult(content="found it", metadata={"tokens_used": 100})

    with patch.object(executor, "_invoke_agent", side_effect=fake_invoke):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    assert traces[0].agent == agent["id"]
    assert traces[0].outcome == "success"
    assert len(traces[0].steps) == 2  # tool_call + respond
    assert traces[0].steps[0].step_type.value == "tool_call"
    assert traces[0].steps[0].input["tool"] == "web_search"
    assert traces[0].steps[0].input["arguments"] == {"query": "test"}
    assert traces[0].steps[0].output["success"] is True
    assert traces[0].steps[0].duration_seconds == 0.5
    assert traces[0].steps[1].step_type.value == "respond"
    assert traces[0].steps[1].output["content"] == "found it"
    # total_tokens falls back to result.metadata when no generate steps
    assert traces[0].total_tokens == 100
    assert traces[0].total_latency_seconds > 0

    mgr.close()
    trace_store.close()


def test_executor_records_generate_steps(tmp_path):
    """execute_tick records INFERENCE_START/END events as generate steps."""
    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus, trace_store=trace_store)

    agent = mgr.create_agent("gen-trace")

    def fake_invoke(agent_dict):
        bus.publish(EventType.INFERENCE_START, {"model": "qwen3:8b"})
        bus.publish(
            EventType.INFERENCE_END,
            {
                "model": "qwen3:8b",
                "usage": {
                    "prompt_tokens": 512,
                    "completion_tokens": 128,
                    "total_tokens": 640,
                },
                "content": "draft",
                "finish_reason": "stop",
            },
        )
        return AgentResult(content="final answer", metadata={"total_tokens": 640})

    with patch.object(executor, "_invoke_agent", side_effect=fake_invoke):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    steps = traces[0].steps
    assert len(steps) == 2  # generate + respond
    assert steps[0].step_type.value == "generate"
    assert steps[0].input["model"] == "qwen3:8b"
    assert steps[0].output["total_tokens"] == 640
    assert steps[0].output["tokens"] == 640
    assert steps[0].output["content"] == "draft"
    assert traces[0].total_tokens == 640
    assert steps[1].step_type.value == "respond"

    mgr.close()
    trace_store.close()


def test_executor_records_error_trace(tmp_path):
    """execute_tick records an error trace on failure."""
    from openjarvis.agents.errors import FatalError

    mgr = AgentManager(str(tmp_path / "agents.db"))
    trace_store = TraceStore(str(tmp_path / "traces.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus, trace_store=trace_store)

    agent = mgr.create_agent("error-trace")

    with patch.object(
        executor,
        "_invoke_agent",
        side_effect=FatalError("boom"),
    ):
        executor.execute_tick(agent["id"])

    traces = trace_store.list_traces(agent=agent["id"])
    assert len(traces) == 1
    assert traces[0].outcome == "error"

    mgr.close()
    trace_store.close()


def test_executor_no_trace_without_store(tmp_path):
    """Without trace_store, no error is raised."""
    mgr = AgentManager(str(tmp_path / "agents.db"))
    bus = EventBus()
    executor = AgentExecutor(mgr, bus)  # No trace_store

    agent = mgr.create_agent("no-trace")

    def fake_invoke(agent_dict):
        return AgentResult(content="done", metadata={})

    with patch.object(executor, "_invoke_agent", side_effect=fake_invoke):
        executor.execute_tick(agent["id"])

    updated = mgr.get_agent(agent["id"])
    assert updated["status"] == "idle"
    mgr.close()
