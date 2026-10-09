"""
Specialist agents must never carry conversation history between requests.

Staging finding F1: bootstrap builds the five specialists once per process and
each used to hold one Strands ``Agent`` for its whole life. Strands appends every
user, assistant and tool message to ``agent.messages``, so tenant B's request
reached the LLM with tenant A's prompt and answer in front of it, and two
concurrent requests mutated the same list.

These tests drive the real specialists (and the real ``AgentOrchestrator``) on a
real Strands ``Agent`` loop. Only the LLM is replaced, by ``RecordingModel``,
which records exactly what Strands sends it. Nothing about the history is mocked,
so a shared ``messages`` list shows up as extra messages in a later call.

Validates: Requirement 7.9; staging finding F1.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from strands.models import Model

from Agents.orchestrator import AgentOrchestrator
from Agents.specialists import (
    FleetAgent,
    FuelAgent,
    OpsIntelligenceAgent,
    ReportingAgent,
    SchedulingAgent,
)


class RecordingModel(Model):
    """Fake Strands model: records the messages of every call, answers "ok".

    Sticks to the stable ``strands.models.Model`` ABC so it runs on both the
    pinned Strands and newer local installs.
    """

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
        # Yield control so concurrent requests interleave under gather().
        await asyncio.sleep(0.01)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _texts(call: list) -> List[str]:
    return [block["text"] for m in call for block in m["content"] if "text" in block]


SPECIALISTS = [FleetAgent, SchedulingAgent, FuelAgent, OpsIntelligenceAgent, ReportingAgent]


@pytest.mark.parametrize("cls", SPECIALISTS, ids=lambda c: c.__name__)
async def test_tenant_b_sees_no_tenant_a_history(cls):
    model = RecordingModel()
    specialist = cls(model=model)  # one instance, as bootstrap builds it

    await specialist.handle("Find truck QA-TRUCK-AG-01", {"tenant_id": "tenant-A"})
    await specialist.handle("Show me delayed trucks", {"tenant_id": "tenant-B"})

    assert len(model.calls) == 2
    second = model.calls[1]
    assert len(second) == 1, f"tenant B's call carried {len(second)} messages"
    for text in _texts(second):
        assert "QA-TRUCK-AG-01" not in text
        assert "tenant-A" not in text


@pytest.mark.parametrize("cls", SPECIALISTS, ids=lambda c: c.__name__)
async def test_sessions_in_one_tenant_do_not_share_history(cls):
    model = RecordingModel()
    specialist = cls(model=model)

    await specialist.handle(
        "session one secret", {"tenant_id": "tenant-A", "session_id": "s1"}
    )
    await specialist.handle(
        "session two question", {"tenant_id": "tenant-A", "session_id": "s2"}
    )

    second = model.calls[1]
    assert len(second) == 1
    assert not any("session one secret" in t for t in _texts(second))


@pytest.mark.parametrize("cls", SPECIALISTS, ids=lambda c: c.__name__)
async def test_concurrent_requests_never_share_a_messages_list(cls):
    model = RecordingModel()
    specialist = cls(model=model)

    # A shared Agent either accumulates both prompts in one list or refuses
    # the second concurrent invocation; both are the F1 bug and fail here.
    await asyncio.gather(
        specialist.handle("alpha", {"tenant_id": "tenant-A"}),
        specialist.handle("bravo", {"tenant_id": "tenant-B"}),
    )

    assert len(model.calls) == 2
    for call in model.calls:
        assert len(call) == 1
        joined = "\n".join(_texts(call))
        assert ("alpha" in joined) != ("bravo" in joined), joined


async def test_orchestrator_route_does_not_leak_across_tenants():
    model = RecordingModel()
    activity_log = MagicMock()
    activity_log.log = AsyncMock(return_value="log-id-1")
    planner = MagicMock()
    planner.create_plan = AsyncMock()
    planner.execute_plan = AsyncMock()
    orch = AgentOrchestrator(
        specialists={
            "fleet": FleetAgent(model=model),
            "scheduling": SchedulingAgent(model=model),
        },
        execution_planner=planner,
        activity_log_service=activity_log,
    )

    # Keyword routing: "truck" -> fleet; "truck jobs" -> fleet + scheduling
    # (two entity keywords; "delayed trucks" narrows to fleet since N3).
    # No LLM classification runs.
    await orch.route("Find truck QA-TRUCK-AG-01", "tenant-A")
    tenant_a_calls = len(model.calls)
    await orch.route("Show truck jobs", "tenant-B")

    assert tenant_a_calls == 1
    assert len(model.calls) == 3  # fleet + scheduling for tenant B
    for call in model.calls[1:]:
        assert len(call) == 1
        assert not any("QA-TRUCK-AG-01" in t for t in _texts(call))


async def test_clear_leaves_no_specialist_history():
    """Specialists hold nothing between requests, so ``/api/chat/clear`` has no
    specialist state it could miss: the very next call starts empty."""
    model = RecordingModel()
    specialist = FleetAgent(model=model)

    await specialist.handle("alpha-secret", {"tenant_id": "tenant-A", "session_id": "s1"})
    await specialist.handle("next", {"tenant_id": "tenant-A", "session_id": "s1"})

    assert len(model.calls[1]) == 1
    assert not any("alpha-secret" in t for t in _texts(model.calls[1]))
