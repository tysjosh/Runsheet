"""
A specialist that fails while the rest of the answer stands is reported as a
typed, non-terminal error event, and is retried while its text has not yet
reached the client.

Staging finding N4: when the fuel specialist hit a 429 after text had
streamed, the client got ``_The fuel assistant couldn't answer this part
right now._`` as ordinary ``text`` (no ``error`` event, no code), and the
orchestrator did not retry it (log: ``Specialist 'fuel' failed (attempt 1/3,
code=AI_RATE_LIMITED…)`` and no attempt 2).

The orchestrator, ``LogisticsAgent`` and the ``/api/chat`` handler are real;
specialists are fakes. No LLM is called.

Validates: staging finding N4.
"""
from __future__ import annotations

import json
from typing import List
from unittest.mock import MagicMock

from Agents.execution_planner import ExecutionPlanner
from Agents.llm_errors import ChatEvent, done_event, status_event, text_event
from tests.unit.test_orchestrator_errors import (  # noqa: F401  (mainagent is a fixture)
    FakeSpecialist,
    _activity_log,
    _assert_no_leak,
    _client,
    _events,
    _orchestrator,
    _rate_limit,
    _routing_completed,
    _sse_payloads,
    _Unavailable,
    mainagent,
)


class StreamingSpecialist:
    """``stream`` (an async generator on the class) yields each attempt's text
    deltas, then raises that attempt's error, if any."""

    def __init__(self, *attempts) -> None:
        # Each attempt: (list of text deltas, exception or None).
        self.attempts = list(attempts)
        self.calls = 0

    async def stream(self, task: str, context: dict = None):
        deltas, error = self.attempts[min(self.calls, len(self.attempts) - 1)]
        self.calls += 1
        for delta in deltas:
            yield text_event(delta)
        if error is not None:
            raise error

    async def handle(self, task: str, context: dict = None) -> str:  # pragma: no cover
        raise AssertionError("streaming specialists are not called via handle")


def _text(events: List[dict]) -> str:
    return "".join(e["content"] for e in events if e["type"] == "text")


def _errors(events: List[dict]) -> List[dict]:
    return [e for e in events if e["type"] == "error"]


# Two entity keywords (truck → fleet, consumption → fuel): two buffered targets.
TWO_TARGETS = "Show truck fuel consumption"


# ---------------------------------------------------------------------------
# Typed partial event
# ---------------------------------------------------------------------------


async def test_failure_after_streamed_text_is_one_typed_partial_event():
    log = _activity_log()
    fleet = StreamingSpecialist((["12 trucks"], _rate_limit()))
    orch = _orchestrator({"fleet": fleet}, activity_log=log)

    events = await _events(orch, "Show trucks")

    assert _text(events) == "12 trucks"
    errors = _errors(events)
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["specialist"] == "fleet"
    assert errors[0]["code"] == "AI_RATE_LIMITED"
    assert errors[0]["request_id"] == "req-123"
    assert errors[0]["message"].startswith("The fleet assistant couldn't answer this part.")
    # The partial event comes after the text it qualifies, before done.
    kinds = [e["type"] for e in events]
    assert kinds.index("error") > kinds.index("text")
    assert events[-1] == done_event()
    _assert_no_leak(events)
    assert _routing_completed(log)["outcome"] == "partial"

    # route() keeps the answer, adds a note, and does not raise.
    orch2 = _orchestrator({"fleet": StreamingSpecialist((["12 trucks"], _rate_limit()))})
    result = await orch2.route("Show trucks", "tenant-1")
    assert result == "12 trucks\n\n_The fleet assistant couldn't answer this part right now._"


async def test_buffered_specialist_is_retried_after_partial_text_and_it_never_reaches_client():
    fleet = FakeSpecialist(answer="12 trucks")
    fuel = StreamingSpecialist((["partial…"], _Unavailable()), (["fuel ok"], None))
    orch = _orchestrator({"fleet": fleet, "fuel": fuel})

    events = await _events(orch, TWO_TARGETS)

    assert fuel.calls == 2
    text = _text(events)
    assert text.count("fuel ok") == 1
    assert "partial…" not in text
    assert text == "**Fleet**\n\n12 trucks\n\n**Fuel**\n\nfuel ok"
    assert not _errors(events)
    assert status_event("retrying", specialist="fuel", attempt=2) in events


async def test_failure_before_any_text_is_retried_with_other_text_present():
    # Pin: the old code already retried a specialist that had emitted no text.
    fleet = FakeSpecialist(answer="12 trucks")
    fuel = FakeSpecialist(_Unavailable(), answer="fuel ok")
    orch = _orchestrator({"fleet": fleet, "fuel": fuel})

    events = await _events(orch, TWO_TARGETS)

    assert fuel.calls == 2
    assert "fuel ok" in _text(events)
    assert not _errors(events)


async def test_long_retry_after_is_not_retried_and_reports_retry_after():
    # Buffered, so "own text already streamed" cannot be the reason: only the
    # 64 400 s Retry-After (staging's free-tier daily quota) stops the retry.
    log = _activity_log()
    fleet = FakeSpecialist(answer="12 trucks")
    fuel = StreamingSpecialist((["partial…"], _rate_limit()))
    orch = _orchestrator({"fleet": fleet, "fuel": fuel}, activity_log=log)

    events = await _events(orch, TWO_TARGETS)

    assert fuel.calls == 1
    errors = _errors(events)
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["specialist"] == "fuel"
    assert errors[0]["retry_after_seconds"] == 64400
    # The failed specialist's incomplete buffered text is not sent.
    assert _text(events) == "12 trucks"
    _assert_no_leak(events)
    entry = _routing_completed(log)
    assert entry["outcome"] == "partial"
    assert entry["details"]["failed_targets"] == ["fuel"]


async def test_every_target_failing_is_still_one_terminal_error():
    log = _activity_log()
    fleet = StreamingSpecialist(([], _rate_limit()))
    fuel = StreamingSpecialist((["partial…"], _rate_limit()))
    orch = _orchestrator({"fleet": fleet, "fuel": fuel}, activity_log=log)

    events = await _events(orch, TWO_TARGETS)

    errors = _errors(events)
    assert len(errors) == 1
    assert "partial" not in errors[0]
    assert errors[0]["code"] == "AI_RATE_LIMITED"
    assert not _text(events)
    assert _routing_completed(log)["outcome"] == "failure"


async def test_complex_plan_with_one_failed_read_step_gets_one_partial_event():
    log = _activity_log()
    planner = ExecutionPlanner(activity_log_service=_activity_log())
    orch = _orchestrator(
        {"fleet": FakeSpecialist(answer="12 trucks"), "fuel": FakeSpecialist(_rate_limit())},
        planner=planner,
        activity_log=log,
    )

    events = await _events(orch, "Check truck status and show fuel levels")

    assert "12 trucks" in _text(events)
    errors = _errors(events)
    assert len(errors) == 1
    assert errors[0]["partial"] is True
    assert errors[0]["specialist"] == "fuel"
    assert errors[0]["code"] == "AI_RATE_LIMITED"
    _assert_no_leak(events)
    assert _routing_completed(log)["outcome"] == "partial"


# ---------------------------------------------------------------------------
# /api/chat and LogisticsAgent
# ---------------------------------------------------------------------------


class _PartialOrchestrator:
    """Yields a partial error event in the wire shape (built by hand so this
    test exercises ``chat_streaming`` alone)."""

    async def route_stream(
        self, user_message, tenant_id, session_id=None, request_id=None, user_id=None,
        history=None,
    ):
        yield status_event("routing", targets=["fleet", "fuel"])
        yield text_event("12 trucks")
        yield ChatEvent(
            type="error", code="AI_RATE_LIMITED", message="The fuel assistant couldn't "
            "answer this part.", request_id=request_id, specialist="fuel", partial=True,
        )
        yield done_event()


def test_chat_endpoint_sends_the_partial_event_then_done(mainagent, monkeypatch):
    orch = _orchestrator({"fleet": StreamingSpecialist((["12 trucks"], _rate_limit()))})
    client = _client(mainagent, monkeypatch, orch)

    resp = client.post(
        "/api/chat", json={"message": "Show trucks"}, headers={"X-Request-ID": "req-abc"}
    )

    payloads = _sse_payloads(resp.text)
    assert [p["type"] for p in payloads] == ["status", "status", "text", "error", "done"]
    assert payloads[2]["content"] == "12 trucks"
    error = payloads[3]
    assert error == {
        "type": "error",
        "code": "AI_RATE_LIMITED",
        "message": error["message"],
        "request_id": "req-abc",
        "retry_after_seconds": 64400,
        "specialist": "fleet",
        "partial": True,
    }
    assert error["message"].startswith("The fleet assistant couldn't answer this part.")
    for needle in ("VertexAI", "quota", "/opt/venv", "Traceback"):
        assert needle not in resp.text


async def test_partial_only_stream_is_recorded_as_a_successful_response(mainagent, monkeypatch):
    monkeypatch.setattr(mainagent, "_orchestrator", _PartialOrchestrator())
    agent = mainagent.LogisticsAgent()
    recorded = MagicMock()
    monkeypatch.setattr(agent, "_record_response_metric", recorded)

    events = [
        e async for e in agent.chat_streaming("Show trucks", tenant_id="tenant-1", request_id="r")
    ]

    assert [e["type"] for e in events] == ["status", "text", "error", "done"]
    assert recorded.call_count == 1
    assert recorded.call_args.kwargs["success"] is True
    assert json.dumps(events)  # still plain JSON for the SSE layer
