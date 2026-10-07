"""Orchestrator chat remembers the conversation per (tenant, user, session) (OI-17).

The F1 fix gave every specialist call a fresh Strands ``Agent`` with no
history, so ``/api/chat`` forgot the previous turn. ``LogisticsAgent`` now
keeps a text-only transcript under ``orch:{tenant}:{user}:{session}`` and the
orchestrator hands it to the specialist as ``context["history"]``; the
specialist copies it into a new ``Agent(messages=...)`` per call.

These tests go through the real ``/api/chat`` and ``/api/chat/clear``
handlers, the real ``LogisticsAgent`` and the real ``AgentOrchestrator``. The
session store is ``FakeSessionStore`` (a dict that serializes like Redis) and
the specialist records the context it was given.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands.models import Model

import inline_endpoints
from Agents.llm_errors import text_event, tool_event, tool_result_event
from Agents.orchestrator import AgentOrchestrator
from Agents.specialists._base import SpecialistAgent
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit.test_chat_session_scoping import FakeSessionStore


class RecordingSpecialist:
    """Records ``context["history"]`` per call and answers with a tool use."""

    def __init__(self) -> None:
        self.histories: List[Optional[list]] = []

    async def stream(self, task, context=None):
        self.histories.append(copy.deepcopy((context or {}).get("history")))
        yield tool_event("probe_tool", {"q": "TOOL-INPUT-SECRET"})
        yield tool_result_event("probe_tool", "success")
        yield text_event(f"answer to {task}")

    async def handle(self, task, context=None):  # pragma: no cover - unused
        raise AssertionError("route_stream uses stream()")


def _activity_log() -> MagicMock:
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id")
    return log


@pytest.fixture
def chat(monkeypatch):
    store = FakeSessionStore()
    specialist = RecordingSpecialist()
    current = {"tenant": "tenant-A", "user": "user-1"}

    mainagent = importlib.import_module("Agents.mainagent")
    model_provider = importlib.import_module("Agents.model_provider")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setattr(model_provider, "build_agent_model", lambda settings: _RecordingModel())
    monkeypatch.setattr(mainagent, "_get_session_store", lambda: store)
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
            tenant_id=current["tenant"],
            user_id=current["user"],
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()
    app.include_router(inline_endpoints.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = _tenant
    client = TestClient(app)

    def turn(message: str, session_id: str = "s1", tenant: str = "tenant-A",
             user: str = "user-1") -> Optional[list]:
        """One /api/chat turn; returns the history the specialist received."""
        current["tenant"], current["user"] = tenant, user
        with client.stream(
            "POST", "/api/chat", json={"message": message, "session_id": session_id}
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert '"type": "done"' in body
        return specialist.histories[-1]

    def clear(session_id: str = "s1", tenant: str = "tenant-A", user: str = "user-1"):
        current["tenant"], current["user"] = tenant, user
        return client.post("/api/chat/clear", json={"session_id": session_id})

    return turn, clear, store


# Every message mentions a truck so keyword routing picks "fleet" (no LLM).

def test_second_turn_sees_the_first(chat):
    turn, _, _ = chat
    assert turn("find truck QA-TRUCK-17") is None
    history = turn("which truck was that?")
    assert history == [
        {"role": "user", "content": "find truck QA-TRUCK-17"},
        {"role": "assistant", "content": "answer to find truck QA-TRUCK-17"},
    ]


def test_other_user_same_tenant_and_session_sees_nothing(chat):
    turn, _, store = chat
    turn("user one truck secret", user="user-1")
    assert turn("my trucks?", user="user-2") is None
    assert "orch:tenant-A:user-1:s1" in store.data
    assert "orch:tenant-A:user-2:s1" in store.data


def test_other_tenant_same_session_sees_nothing(chat):
    turn, _, _ = chat
    turn("tenant A truck secret", tenant="tenant-A")
    assert turn("trucks?", tenant="tenant-B") is None


def test_clear_removes_the_transcript(chat):
    turn, clear, store = chat
    turn("truck alpha")
    resp = clear()
    assert resp.status_code == 200, resp.text
    assert "orch:tenant-A:user-1:s1" not in store.data
    assert turn("truck again?") is None


def test_transcript_is_capped_at_twenty_messages(chat):
    turn, _, store = chat
    for i in range(12):
        turn(f"truck {i}")
    messages = store.data["orch:tenant-A:user-1:s1"]["messages"]
    assert len(messages) == 20
    assert messages[0] == {"role": "user", "content": "truck 2"}
    assert messages[-1] == {"role": "assistant", "content": "answer to truck 11"}


def test_long_messages_are_cut_to_4000_characters(chat):
    turn, _, store = chat
    turn("truck " + "x" * 5000)
    for m in store.data["orch:tenant-A:user-1:s1"]["messages"]:
        assert len(m["content"]) <= 4000


def test_tool_input_and_output_are_never_stored(chat):
    turn, _, store = chat
    turn("truck status")
    stored = repr(store.data)
    assert "TOOL-INPUT-SECRET" not in stored
    assert "probe_tool" not in stored
    assert all(set(m) == {"role", "content"} for m in store.data["orch:tenant-A:user-1:s1"]["messages"])


def test_no_session_id_never_touches_the_transcript(chat):
    turn, _, store = chat
    turn("truck one", session_id="")
    assert not any(k.startswith("orch:") for k in store.data)


def test_store_failure_still_answers(chat, monkeypatch):
    turn, _, _ = chat

    async def _boom(self, *args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(FakeSessionStore, "get", _boom)
    monkeypatch.setattr(FakeSessionStore, "set", _boom)
    assert turn("truck while redis is down") is None
    assert turn("truck again") is None


# ---------------------------------------------------------------------------
# SpecialistAgent: the transcript becomes the fresh Agent's messages
# ---------------------------------------------------------------------------


class _RecordingModel(Model):
    def __init__(self) -> None:
        self.calls: List[list] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        await asyncio.sleep(0)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class _PlainSpecialist(SpecialistAgent):
    TOOLS: list = []
    SYSTEM_PROMPT = "plain"


async def test_history_becomes_agent_messages_and_is_never_shared():
    model = _RecordingModel()
    specialist = _PlainSpecialist(model=model)
    history: List[Dict[str, str]] = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    ctx = {"tenant_id": "tenant-A", "history": history}

    await specialist.handle("now", ctx)
    await specialist.handle("again", ctx)

    first, second = model.calls
    assert [m["role"] for m in first] == ["user", "assistant", "user"]
    assert first[0]["content"] == [{"text": "earlier question"}]
    assert first[1]["content"] == [{"text": "earlier answer"}]
    # The second call starts from the same transcript, not from the first
    # call's grown messages list.
    assert len(second) == 3
    assert second[-1]["content"] == [{"text": "again"}]
    # The caller's transcript is untouched.
    assert len(history) == 2

    a1 = specialist._new_agent(history)
    a2 = specialist._new_agent(history)
    assert a1.messages is not a2.messages
    assert a1.messages[0] is not a2.messages[0]
