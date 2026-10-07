"""
LLM failures reach the chat client as typed error events, never as answer text.

Staging findings:

* F3 — a specialist's ``litellm.RateLimitError`` (Vertex free-tier quota) was
  returned as ``"[fleet] Error processing request: litellm.RateLimitError:
  VertexAIException - {...quota id...}"`` in a 200 answer, nothing retried
  it, and ``/api/chat/fallback`` answered 200 with "trouble connecting" text.
* F4 — ``routing_completed`` was logged with ``outcome="success"`` for every
  request, including ones where every specialist failed.

Specialists here are fakes whose ``handle`` raises; the orchestrator, planner,
``LogisticsAgent`` and the ``/api/chat`` handlers are real. No LLM is called.

Validates: staging findings F3, F4.
"""
from __future__ import annotations

import importlib
import json
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands.models import Model

from Agents.execution_planner import ExecutionPlanner, PlanStep, StepStatus
from Agents.llm_errors import (
    AgentServiceError,
    ChatEvent,
    done_event,
    error_event,
    status_event,
    text_event,
)
from Agents.orchestrator import AgentOrchestrator

# What the staging Vertex rate limit looked like once it reached the answer:
# provider name, quota metric / id, RetryInfo and a traceback fragment.
LEAKY_MESSAGE = (
    'VertexAIException - {"error": {"code": 429, "message": "Quota exceeded for '
    'metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, '
    'limit: 250", "status": "RESOURCE_EXHAUSTED", "details": [{"@type": '
    '"type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": '
    '"GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaDimensions": '
    '{"location": "global", "model": "gemini-2.5-flash"}, "quotaValue": "250", '
    '"projectId": "projects/987654321"}]}, {"@type": '
    '"type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "64400s"}]}}\n'
    "Traceback (most recent call last):\n"
    '  File "/opt/venv/lib/python3.11/site-packages/litellm/main.py", line 2350'
)
FORBIDDEN = ("VertexAI", "quota", "Quota", "/opt/venv", "Traceback",
             "Error processing request", "projects/987654321", "RESOURCE_EXHAUSTED")


def _rate_limit(message: str = LEAKY_MESSAGE) -> litellm.RateLimitError:
    return litellm.RateLimitError(
        message=message, llm_provider="vertex_ai", model="gemini-2.5-flash"
    )


class _Unavailable(Exception):
    status_code = 503

    def __init__(self) -> None:
        super().__init__("503 The model is overloaded. Please try again later.")


class FakeSpecialist:
    """``handle`` raises the queued exceptions in order, then answers."""

    def __init__(self, *errors: BaseException, answer: str = "answer") -> None:
        self.errors = list(errors)
        self.answer = answer
        self.calls = 0

    async def handle(self, task: str, context: dict = None) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.answer


async def _no_sleep(_seconds: float) -> None:
    return None


def _activity_log() -> MagicMock:
    log = MagicMock()
    log.log = AsyncMock(return_value="log-id")
    return log


def _orchestrator(specialists: dict, planner=None, activity_log=None) -> AgentOrchestrator:
    return AgentOrchestrator(
        specialists=specialists,
        execution_planner=planner or MagicMock(),
        activity_log_service=activity_log or _activity_log(),
        sleep=_no_sleep,
        rand=lambda lo, hi: lo,
    )


async def _events(orch: AgentOrchestrator, message: str, request_id="req-123") -> List[dict]:
    return [e async for e in orch.route_stream(message, "tenant-1", request_id=request_id)]


def _routing_completed(activity_log: MagicMock) -> dict:
    entries = [c.args[0] for c in activity_log.log.call_args_list]
    done = [e for e in entries if e["details"]["event"] == "routing_completed"]
    assert len(done) == 1
    return done[0]


def _assert_no_leak(events: List[dict]) -> None:
    blob = json.dumps(events)
    for needle in FORBIDDEN:
        assert needle not in blob, f"{needle!r} leaked into chat events"


# ---------------------------------------------------------------------------
# route_stream: error events
# ---------------------------------------------------------------------------


async def test_rate_limit_becomes_one_safe_error_event():
    fleet = FakeSpecialist(_rate_limit())
    orch = _orchestrator({"fleet": fleet})

    events = await _events(orch, "Show trucks")

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "AI_RATE_LIMITED"
    assert errors[0]["request_id"] == "req-123"
    assert errors[0]["retry_after_seconds"] == 64400
    assert not [e for e in events if e["type"] == "text"]
    assert events[-1] == {"type": "done"}
    assert all(isinstance(e, ChatEvent) for e in events)
    _assert_no_leak(events)
    # 64 400 s Retry-After: not worth waiting, so exactly one model call.
    assert fleet.calls == 1


async def test_route_raises_agent_service_error_without_provider_text():
    orch = _orchestrator({"fleet": FakeSpecialist(_rate_limit())})

    with pytest.raises(AgentServiceError) as excinfo:
        await orch.route("Show trucks", "tenant-1")

    assert excinfo.value.code == "AI_RATE_LIMITED"
    for needle in FORBIDDEN:
        assert needle not in str(excinfo.value)


async def test_one_503_is_retried_once_then_answers():
    fleet = FakeSpecialist(_Unavailable(), answer="12 trucks")
    orch = _orchestrator({"fleet": fleet})

    events = await _events(orch, "Show trucks")

    assert fleet.calls == 2
    retrying = [e for e in events if e.get("stage") == "retrying"]
    assert retrying == [{"type": "status", "stage": "retrying", "specialist": "fleet", "attempt": 2}]
    assert [e["content"] for e in events if e["type"] == "text"] == ["12 trucks"]
    assert not [e for e in events if e["type"] == "error"]


async def test_three_503s_give_up_with_an_error():
    fleet = FakeSpecialist(_Unavailable(), _Unavailable(), _Unavailable())
    orch = _orchestrator({"fleet": fleet})

    events = await _events(orch, "Show trucks")

    assert fleet.calls == 3
    errors = [e for e in events if e["type"] == "error"]
    assert [e["code"] for e in errors] == ["AI_SERVICE_UNAVAILABLE"]
    assert "overloaded" not in json.dumps(events)


async def test_event_order_for_a_simple_request():
    orch = _orchestrator({"fleet": FakeSpecialist(answer="hi")})

    events = await _events(orch, "Show trucks")

    assert events == [
        status_event("routing", targets=["fleet"]),
        status_event("specialist_start", specialist="fleet"),
        text_event("hi"),
        done_event(),
    ]


# ---------------------------------------------------------------------------
# F4: routing outcome
# ---------------------------------------------------------------------------


async def test_outcome_success_when_every_target_answers():
    log = _activity_log()
    orch = _orchestrator(
        {"fleet": FakeSpecialist(answer="a"), "fuel": FakeSpecialist(answer="b")},
        activity_log=log,
    )

    events = await _events(orch, "truck fuel consumption")

    entry = _routing_completed(log)
    assert entry["outcome"] == "success"
    assert entry["details"]["failed_targets"] == []
    assert entry["details"]["error_codes"] == []
    # Several answers are labelled by specialist (N3).
    text = "".join(e["content"] for e in events if e["type"] == "text")
    assert text == "**Fleet**\n\na\n\n**Fuel**\n\nb"


async def test_activity_entries_carry_the_callers_user_id():
    """OI-60: both routing entries record who asked, not ``None``."""
    log = _activity_log()
    orch = _orchestrator({"fleet": FakeSpecialist(answer="a")}, activity_log=log)

    [e async for e in orch.route_stream("Show trucks", "tenant-1", user_id="user-7")]
    await orch.route("Show trucks", "tenant-1", user_id="user-8")

    entries = [c.args[0] for c in log.log.call_args_list]
    assert [e["details"]["event"] for e in entries] == [
        "intent_classified", "routing_completed",
        "intent_classified", "routing_completed",
    ]
    assert [e["user_id"] for e in entries] == ["user-7", "user-7", "user-8", "user-8"]


async def test_outcome_partial_keeps_the_answer_and_adds_a_safe_note():
    log = _activity_log()
    orch = _orchestrator(
        {"fleet": FakeSpecialist(answer="12 trucks"), "fuel": FakeSpecialist(_rate_limit())},
        activity_log=log,
    )

    events = await _events(orch, "truck fuel consumption")

    text = "".join(e["content"] for e in events if e["type"] == "text")
    assert "12 trucks" in text
    # The failure is a typed partial error event, not note text (N4).
    assert "couldn't answer" not in text
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert {k: errors[0][k] for k in ("code", "partial", "specialist", "request_id")} == {
        "code": "AI_RATE_LIMITED",
        "partial": True,
        "specialist": "fuel",
        "request_id": "req-123",
    }
    assert errors[0]["message"].startswith("The fuel assistant couldn't answer this part.")
    _assert_no_leak(events)
    entry = _routing_completed(log)
    assert entry["outcome"] == "partial"
    assert entry["details"]["failed_targets"] == ["fuel"]
    assert entry["details"]["error_codes"] == ["AI_RATE_LIMITED"]


async def test_outcome_failure_when_every_target_fails():
    log = _activity_log()
    orch = _orchestrator(
        {"fleet": FakeSpecialist(_rate_limit()), "fuel": FakeSpecialist(RuntimeError("boom"))},
        activity_log=log,
    )

    events = await _events(orch, "truck fuel consumption")

    errors = [e for e in events if e["type"] == "error"]
    # Any rate limit among the failures makes the whole request rate-limited.
    assert [e["code"] for e in errors] == ["AI_RATE_LIMITED"]
    assert "boom" not in json.dumps(events)
    entry = _routing_completed(log)
    assert entry["outcome"] == "failure"
    assert sorted(entry["details"]["failed_targets"]) == ["fleet", "fuel"]
    assert entry["details"]["error_codes"] == ["AI_RATE_LIMITED", "AI_SERVICE_UNAVAILABLE"]


# ---------------------------------------------------------------------------
# Complex (planner) requests
# ---------------------------------------------------------------------------


async def test_planner_does_not_rerun_an_agent_service_error_step():
    fleet = FakeSpecialist(_rate_limit())
    fuel = FakeSpecialist(_rate_limit())
    log = _activity_log()
    planner = ExecutionPlanner(activity_log_service=_activity_log())
    orch = _orchestrator({"fleet": fleet, "fuel": fuel}, planner=planner, activity_log=log)

    events = await _events(orch, "Check truck status and show fuel levels")

    # One model call per step: the orchestrator's retry declined the 64 400 s
    # wait and the planner did not re-run the step (it used to, twice).
    assert fleet.calls == 1
    assert fuel.calls == 1
    assert {"type": "status", "stage": "planning"} in events
    errors = [e for e in events if e["type"] == "error"]
    assert [e["code"] for e in errors] == ["AI_RATE_LIMITED"]
    _assert_no_leak(events)
    assert _routing_completed(log)["outcome"] == "failure"


async def test_failed_plan_step_result_has_no_raw_exception_text():
    planner = ExecutionPlanner(activity_log_service=_activity_log())

    async def executor(step, params, tenant_id):
        raise AgentServiceError("AI_RATE_LIMITED", retry_after_seconds=60)

    planner.set_read_step_executor(executor)
    step = PlanStep(
        step_id=1, description="d", agent="fleet", tool_name="fleet_query",
        parameters={}, read_only=True,
    )
    ok = await planner._execute_step_with_recovery(step, {}, "tenant-1")

    assert ok is False
    assert step.status == StepStatus.FAILED
    assert step.recovery_attempts == 1
    assert step.result == AgentServiceError("AI_RATE_LIMITED", retry_after_seconds=60).safe_message


async def test_plan_with_one_failed_read_step_reports_partial_text():
    log = _activity_log()
    planner = ExecutionPlanner(activity_log_service=_activity_log())
    orch = _orchestrator(
        {"fleet": FakeSpecialist(answer="12 trucks"), "fuel": FakeSpecialist(_rate_limit())},
        planner=planner,
        activity_log=log,
    )

    events = await _events(orch, "Check truck status and show fuel levels")

    text = "".join(e["content"] for e in events if e["type"] == "text")
    assert "12 trucks" in text
    assert "receiving too many requests" in text
    _assert_no_leak(events)
    entry = _routing_completed(log)
    assert entry["outcome"] == "partial"
    assert entry["details"]["failed_targets"] == ["fuel"]


# ---------------------------------------------------------------------------
# Legacy direct-agent path and the HTTP endpoints
# ---------------------------------------------------------------------------


class RecordingModel(Model):
    def __init__(self) -> None:
        self.calls: List[list] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(messages)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.fixture
def mainagent(monkeypatch):
    module = importlib.import_module("Agents.mainagent")
    model_provider = importlib.import_module("Agents.model_provider")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder")
    monkeypatch.setattr(model_provider, "build_agent_model", lambda settings: RecordingModel())
    monkeypatch.setattr(module, "_orchestrator", None)
    monkeypatch.setattr(module, "_get_session_store", lambda: None)
    return module


async def test_legacy_stream_yields_safe_error_event(mainagent):
    agent = mainagent.LogisticsAgent()

    async def _raise(_message):
        raise _rate_limit()
        yield  # pragma: no cover - makes this an async generator

    agent.agent.stream_async = _raise

    events = [
        e async for e in agent.chat_streaming("hi", tenant_id="tenant-1", request_id="req-9")
    ]

    assert len(events) == 1
    assert isinstance(events[0], ChatEvent)
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "AI_RATE_LIMITED"
    assert events[0]["request_id"] == "req-9"
    _assert_no_leak(events)


async def test_chat_fallback_raises_agent_service_error(mainagent):
    agent = mainagent.LogisticsAgent()
    agent.agent.invoke_async = AsyncMock(side_effect=_rate_limit())

    with pytest.raises(AgentServiceError) as excinfo:
        await agent.chat_fallback("hi", tenant_id="tenant-1")

    assert excinfo.value.code == "AI_RATE_LIMITED"
    assert agent.agent.invoke_async.await_count == 1  # 64 400 s: no retry


def _client(mainagent_module, monkeypatch, orchestrator=None) -> TestClient:
    import inline_endpoints
    from errors.handlers import register_exception_handlers
    from middleware.request_id import RequestIDMiddleware
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    if orchestrator is not None:
        monkeypatch.setattr(mainagent_module, "_orchestrator", orchestrator)

    async def _tenant() -> TenantContext:
        return TenantContext(
            tenant_id="tenant-1", user_id="user-1", has_pii_access=False, roles=["dispatcher"]
        )

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    app.include_router(inline_endpoints.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_tenant_context] = _tenant
    return TestClient(app)


def _sse_payloads(body: str) -> List[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


class _ErrorOrchestrator:
    def __init__(self) -> None:
        self.user_ids: List[Optional[str]] = []

    async def route_stream(
        self, user_message, tenant_id, session_id=None, request_id=None, user_id=None
    ):
        self.user_ids.append(user_id)
        yield status_event("routing", targets=["fleet"])
        yield error_event("AI_RATE_LIMITED", "safe text", request_id, 64400)
        yield done_event()


def test_chat_endpoint_sends_the_error_event(mainagent, monkeypatch):
    orchestrator = _ErrorOrchestrator()
    client = _client(mainagent, monkeypatch, orchestrator)

    resp = client.post(
        "/api/chat", json={"message": "Show trucks"}, headers={"X-Request-ID": "req-abc"}
    )

    payloads = _sse_payloads(resp.text)
    errors = [p for p in payloads if p.get("type") == "error"]
    assert errors == [{
        "type": "error",
        "code": "AI_RATE_LIMITED",
        "message": "safe text",
        "request_id": "req-abc",
        "retry_after_seconds": 64400,
    }]
    assert [p for p in payloads if p.get("type") == "done"] == [{"type": "done"}]
    # /api/chat passes the verified caller to the orchestrator (OI-60).
    assert orchestrator.user_ids == ["user-1"]


def test_chat_endpoint_unexpected_failure_is_a_safe_error(mainagent, monkeypatch):
    class _Broken:
        async def route_stream(self, *args, **kwargs):
            yield status_event("routing", targets=["fleet"])
            raise RuntimeError("secret /opt/venv/lib detail")

    client = _client(mainagent, monkeypatch, _Broken())

    resp = client.post("/api/chat", json={"message": "Show trucks"})

    payloads = _sse_payloads(resp.text)
    assert [p["code"] for p in payloads if p.get("type") == "error"] == ["AI_SERVICE_UNAVAILABLE"]
    assert [p for p in payloads if p.get("type") == "done"] == [{"type": "done"}]
    assert "/opt/venv" not in resp.text


def test_chat_fallback_endpoint_answers_429_with_retry_after(mainagent, monkeypatch):
    async def _fail(self, *args, **kwargs):
        raise AgentServiceError("AI_RATE_LIMITED", retry_after_seconds=30)

    monkeypatch.setattr(mainagent.LogisticsAgent, "chat_fallback", _fail)
    client = _client(mainagent, monkeypatch)

    resp = client.post("/api/chat/fallback", json={"message": "hi"})

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "30"
    body = resp.json()
    assert body["error_code"] == "AI_RATE_LIMITED"
    assert body["details"] == {"retry_after_seconds": 30}
    assert "too many requests" in body["message"]


def test_chat_fallback_endpoint_answers_503_when_unavailable(mainagent, monkeypatch):
    async def _fail(self, *args, **kwargs):
        raise AgentServiceError("AI_SERVICE_UNAVAILABLE")

    monkeypatch.setattr(mainagent.LogisticsAgent, "chat_fallback", _fail)
    client = _client(mainagent, monkeypatch)

    resp = client.post("/api/chat/fallback", json={"message": "hi"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "AI_SERVICE_UNAVAILABLE"
