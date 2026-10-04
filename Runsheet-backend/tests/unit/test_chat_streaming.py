"""
``/api/chat`` streams incrementally as Server-Sent Events with tool progress.

Staging finding F6: the chat endpoint awaited the orchestrator's whole
``route()`` and then sent one ``text/plain`` event, so users stared at a
spinner for the full model + tool round trip and never saw which tools ran.

These tests drive a real ``SpecialistAgent`` on a real Strands loop with a real
``@tool``. Only the LLM is replaced, by ``ScriptedModel``: its first call asks
for the tool, its second streams the answer in two deltas.

Validates: staging finding F6.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands import tool
from strands.models import Model

from Agents.llm_errors import ChatEvent, text_event
from Agents.orchestrator import AgentOrchestrator
from Agents.specialists._base import TOOL_OUTPUT_LIMIT, SpecialistAgent


@tool
def probe_tool() -> str:
    """Return a fixed marker so the test can see the tool result."""
    return "probe-ok"


@tool
def big_tool() -> str:
    """Return more output than the client is sent."""
    return "x" * (TOOL_OUTPUT_LIMIT + 500)


class ScriptedModel(Model):
    """Call 1 requests ``tool_name``; call 2 streams "Hel" + "lo"."""

    def __init__(self, tool_name: str = "probe_tool") -> None:
        self.tool_name = tool_name
        self.calls: List[list] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        yield {"messageStart": {"role": "assistant"}}
        if len(self.calls) == 1:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": self.tool_name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "Hel"}}}
        yield {"contentBlockDelta": {"delta": {"text": "lo"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class _ProbeSpecialist(SpecialistAgent):
    TOOLS = [probe_tool]
    SYSTEM_PROMPT = "probe"


class _BigSpecialist(SpecialistAgent):
    TOOLS = [big_tool]
    SYSTEM_PROMPT = "big"


def _activity_log() -> MagicMock:
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id")
    return log


async def test_specialist_stream_yields_tool_progress_then_text_deltas():
    specialist = _ProbeSpecialist(model=ScriptedModel())

    events = [e async for e in specialist.stream("go", {"tenant_id": "tenant-1"})]

    assert all(isinstance(e, ChatEvent) for e in events)
    assert [e["type"] for e in events] == ["tool", "tool_result", "text", "text"]
    assert events[0]["tool_name"] == "probe_tool"
    assert events[1]["tool_name"] == "probe_tool"
    assert "probe-ok" in events[1]["tool_output"]
    assert [e["content"] for e in events[2:]] == ["Hel", "lo"]


async def test_tool_output_sent_to_the_client_is_truncated():
    specialist = _BigSpecialist(model=ScriptedModel("big_tool"))

    events = [e async for e in specialist.stream("go", {"tenant_id": "tenant-1"})]

    (result,) = [e for e in events if e["type"] == "tool_result"]
    assert len(result["tool_output"]) == TOOL_OUTPUT_LIMIT


async def test_stream_builds_a_fresh_agent_per_call():
    """F1 invariant holds on the streaming path too."""
    model = ScriptedModel()
    specialist = _ProbeSpecialist(model=model)

    [e async for e in specialist.stream("first", {"tenant_id": "tenant-A"})]
    model.calls.clear()  # rewinds the script for the second request
    [e async for e in specialist.stream("second", {"tenant_id": "tenant-B"})]

    first_call = model.calls[0]
    assert len(first_call) == 1, "second request carried the first one's history"


class _GatedSpecialist:
    """Yields one text event, then blocks until the test opens the gate."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def stream(self, task, context=None):
        yield text_event("first")
        await self.gate.wait()
        yield text_event(" second")

    async def handle(self, task, context=None):  # pragma: no cover - unused
        raise AssertionError("route_stream must use stream()")


async def test_route_stream_delivers_events_before_the_specialist_finishes():
    specialist = _GatedSpecialist()
    orch = AgentOrchestrator(
        specialists={"fleet": specialist},
        execution_planner=MagicMock(),
        activity_log_service=_activity_log(),
    )
    stream = orch.route_stream("Show trucks", "tenant-1").__aiter__()

    routing = await asyncio.wait_for(stream.__anext__(), 1)
    start = await asyncio.wait_for(stream.__anext__(), 1)
    first = await asyncio.wait_for(stream.__anext__(), 1)

    assert routing["stage"] == "routing"
    assert start == {"type": "status", "stage": "specialist_start", "specialist": "fleet"}
    assert first == {"type": "text", "content": "first"}
    assert not specialist.gate.is_set()  # the specialist is still running

    specialist.gate.set()
    rest = [e async for e in stream]
    assert rest == [{"type": "text", "content": " second"}, {"type": "done"}]


def test_chat_endpoint_streams_server_sent_events(monkeypatch):
    import inline_endpoints
    from errors.handlers import register_exception_handlers
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    mainagent = importlib.import_module("Agents.mainagent")
    model_provider = importlib.import_module("Agents.model_provider")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setattr(model_provider, "build_agent_model", lambda settings: ScriptedModel())
    monkeypatch.setattr(mainagent, "_get_session_store", lambda: None)
    monkeypatch.setattr(
        mainagent,
        "_orchestrator",
        AgentOrchestrator(
            specialists={"fleet": _ProbeSpecialist(model=ScriptedModel())},
            execution_planner=MagicMock(),
            activity_log_service=_activity_log(),
        ),
    )

    async def _tenant() -> TenantContext:
        return TenantContext(
            tenant_id="tenant-1", user_id="user-1", has_pii_access=False, roles=["dispatcher"]
        )

    app = FastAPI()
    app.include_router(inline_endpoints.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = _tenant
    client = TestClient(app)

    with client.stream("POST", "/api/chat", json={"message": "Show trucks"}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        body = "".join(resp.iter_text())

    payloads = [
        json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")
    ]
    types = [p["type"] for p in payloads]
    assert types == ["status", "status", "tool", "tool_result", "text", "text", "done"]
    assert types.count("done") == 1
    assert "".join(p["content"] for p in payloads if p["type"] == "text") == "Hello"
