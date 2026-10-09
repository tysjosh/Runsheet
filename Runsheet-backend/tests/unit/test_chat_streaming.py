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
from Agents.specialists._base import SpecialistAgent

#: Stands in for internal detail a failing tool's exception carries.
SECRET = "psycopg.OperationalError: connection to server at 10.0.3.17 failed SECRET-7f3a"


@tool
def probe_tool() -> str:
    """Return a fixed marker so the test can see the tool result."""
    return "probe-ok"


@tool
def boom_tool() -> str:
    """Raise, so Strands turns the exception into an error tool result."""
    raise RuntimeError(SECRET)


@tool
def caught_error_tool() -> str:
    """Return its own error text, as the ``f"Error ...: {e}"`` tools do."""
    return f"Error searching fleet: {SECRET}"


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


class _BoomSpecialist(SpecialistAgent):
    TOOLS = [boom_tool]
    SYSTEM_PROMPT = "boom"


class _CaughtErrorSpecialist(SpecialistAgent):
    TOOLS = [caught_error_tool]
    SYSTEM_PROMPT = "caught"


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
    assert events[1] == {"type": "tool_result", "tool_name": "probe_tool", "status": "success"}
    assert [e["content"] for e in events[2:]] == ["Hel", "lo"]


async def test_tool_result_event_carries_status_not_output():
    """Tool output stays server-side; a raising tool reports ``error`` only."""
    specialist = _BoomSpecialist(model=ScriptedModel("boom_tool"))

    events = [e async for e in specialist.stream("go", {"tenant_id": "tenant-1"})]

    (result,) = [e for e in events if e["type"] == "tool_result"]
    assert result == {"type": "tool_result", "tool_name": "boom_tool", "status": "error"}
    assert "SECRET-7f3a" not in json.dumps(events)


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


def _chat_client(monkeypatch, specialist: SpecialistAgent) -> TestClient:
    """A TestClient for ``/api/chat`` whose orchestrator routes to ``specialist``."""
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
            specialists={"fleet": specialist},
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
    return TestClient(app)


def test_chat_endpoint_streams_server_sent_events(monkeypatch):
    client = _chat_client(monkeypatch, _ProbeSpecialist(model=ScriptedModel()))

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


def _sse_payloads(body: str) -> list:
    return [
        json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")
    ]


def test_raising_tool_exception_text_is_absent_from_the_chat_stream(monkeypatch):
    """Review R1: a raising tool's ``str(exc)`` must not reach the SSE body (F3)."""
    client = _chat_client(monkeypatch, _BoomSpecialist(model=ScriptedModel("boom_tool")))

    with client.stream("POST", "/api/chat", json={"message": "Show trucks"}) as resp:
        body = "".join(resp.iter_text())

    assert "SECRET-7f3a" not in body
    assert "10.0.3.17" not in body
    assert "RuntimeError" not in body
    (result,) = [p for p in _sse_payloads(body) if p["type"] == "tool_result"]
    assert result == {"type": "tool_result", "tool_name": "boom_tool", "status": "error"}


def test_tool_returned_error_text_is_absent_from_the_chat_stream(monkeypatch):
    """Tools that return ``f"Error ...: {e}"`` must not leak it either."""
    client = _chat_client(
        monkeypatch, _CaughtErrorSpecialist(model=ScriptedModel("caught_error_tool"))
    )

    with client.stream("POST", "/api/chat", json={"message": "Show trucks"}) as resp:
        body = "".join(resp.iter_text())

    assert "SECRET-7f3a" not in body
    assert "tool_output" not in body
    assert [p["type"] for p in _sse_payloads(body)].count("tool_result") == 1


def test_direct_path_emits_one_tool_event_per_tool_use(monkeypatch):
    """OI-39: raw ``current_tool_use`` deltas collapse to one ``tool`` event each."""
    import inline_endpoints
    from errors.handlers import register_exception_handlers
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    mainagent = importlib.import_module("Agents.mainagent")

    class _DirectAgent:
        async def chat_streaming(self, *args, **kwargs):
            for partial in ("", '{"q', '{"query": "trucks"}'):
                yield {"current_tool_use": {
                    "toolUseId": "tu-1", "name": "search_fleet_data", "input": partial,
                }}
            yield {"current_tool_result": {"name": "search_fleet_data", "status": "success"}}
            for partial in ("", '{"id": 1}'):
                yield {"current_tool_use": {
                    "toolUseId": "tu-2", "name": "search_fleet_data", "input": partial,
                }}
            yield {"data": "Done"}
            yield {"result": "ok"}

    monkeypatch.setattr(mainagent, "LogisticsAgent", _DirectAgent)

    async def _tenant() -> TenantContext:
        return TenantContext(
            tenant_id="tenant-1", user_id="user-1", has_pii_access=False, roles=["dispatcher"]
        )

    app = FastAPI()
    app.include_router(inline_endpoints.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = _tenant

    with TestClient(app).stream("POST", "/api/chat", json={"message": "Show trucks"}) as resp:
        body = "".join(resp.iter_text())

    types = [p["type"] for p in _sse_payloads(body)]
    assert types == ["tool", "tool_result", "tool", "text", "done"]
