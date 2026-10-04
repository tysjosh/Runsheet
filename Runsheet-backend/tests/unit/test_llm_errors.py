"""
LLM failure classification and the bounded retry policy.

Staging finding F3: a Vertex rate limit escaped as answer text and nothing
retried it. ``Agents.llm_errors`` is the single policy: classify, decide whether
to wait, and produce a safe message.

Validates: staging finding F3.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from Agents import llm_errors
from Agents.llm_errors import (
    AI_RATE_LIMITED,
    AI_SERVICE_UNAVAILABLE,
    MAX_ATTEMPTS,
    MAX_DELAY_S,
    AgentServiceError,
    ChatEvent,
    LLMFailure,
    backoff_delay,
    backoff_delays,
    call_with_llm_retry,
    classify_llm_exception,
    error_event,
    retry_after_seconds,
    retry_delay,
    safe_message_for,
)


def _rate_limit(message: str = "quota exceeded") -> Exception:
    return litellm.RateLimitError(
        message=message, llm_provider="vertex_ai", model="gemini-2.5-flash"
    )


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str = "upstream said no") -> None:
        super().__init__(message)
        self.status_code = status_code


class _HeaderError(Exception):
    def __init__(self, headers: dict) -> None:
        super().__init__("slow down")
        self.status_code = 429
        self.litellm_response_headers = headers


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, code, retryable",
    [
        (_rate_limit(), AI_RATE_LIMITED, True),
        (_StatusError(429), AI_RATE_LIMITED, True),
        (_StatusError(503), AI_SERVICE_UNAVAILABLE, True),
        (_StatusError(500), AI_SERVICE_UNAVAILABLE, True),
        (asyncio.TimeoutError(), AI_SERVICE_UNAVAILABLE, True),
        (
            litellm.ServiceUnavailableError(
                message="overloaded", llm_provider="vertex_ai", model="m"
            ),
            AI_SERVICE_UNAVAILABLE,
            True,
        ),
        (litellm.Timeout(message="t", model="m", llm_provider="p"), AI_SERVICE_UNAVAILABLE, True),
        (RuntimeError("RESOURCE_EXHAUSTED: try later"), AI_RATE_LIMITED, True),
        (RuntimeError("The model is overloaded"), AI_SERVICE_UNAVAILABLE, True),
        (ValueError("bad input"), AI_SERVICE_UNAVAILABLE, False),
        (_StatusError(400), AI_SERVICE_UNAVAILABLE, False),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
def test_classification(exc, code, retryable):
    failure = classify_llm_exception(exc)
    assert failure.code == code
    assert failure.retryable is retryable


def test_wrapped_provider_error_is_classified_by_its_cause():
    """Strands wraps model errors in ``EventLoopException``."""
    from strands.types.exceptions import EventLoopException

    inner = _rate_limit()
    try:
        raise EventLoopException(inner) from inner
    except EventLoopException as wrapped:
        assert classify_llm_exception(wrapped).code == AI_RATE_LIMITED


def test_agent_service_error_is_never_retried():
    failure = classify_llm_exception(AgentServiceError(AI_RATE_LIMITED, retry_after_seconds=5))
    assert failure == LLMFailure(AI_RATE_LIMITED, False, 5)


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def test_retry_after_from_header():
    assert retry_after_seconds(_HeaderError({"Retry-After": "7"})) == 7


def test_retry_after_from_google_retry_delay_text():
    exc = _rate_limit(
        'VertexAIException - {"error": {"code": 429, "details": [{"@type": '
        '"type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "64400s"}]}}'
    )
    assert retry_after_seconds(exc) == 64400
    assert classify_llm_exception(exc).retry_after_seconds == 64400


def test_no_retry_after_is_none():
    assert retry_after_seconds(_rate_limit("no hint")) is None


# ---------------------------------------------------------------------------
# Backoff policy
# ---------------------------------------------------------------------------


def test_backoff_never_exceeds_cap():
    worst = lambda lo, hi: hi  # noqa: E731 - maximum jitter
    for attempt in range(1, 12):
        assert backoff_delay(attempt, worst) <= MAX_DELAY_S
    assert backoff_delay(1, worst) == 1.0
    assert backoff_delay(2, worst) == 2.0
    assert backoff_delay(10, worst) == MAX_DELAY_S


def test_equal_jitter_keeps_at_least_half_the_base():
    least = lambda lo, hi: lo  # noqa: E731
    assert backoff_delay(1, least) == 0.5
    assert backoff_delay(3, least) == 2.0


def test_backoff_delays_cover_the_retries():
    assert len(backoff_delays()) == MAX_ATTEMPTS - 1 == 2


def test_long_retry_after_is_not_waited_out():
    failure = LLMFailure(AI_RATE_LIMITED, True, 64400)
    assert retry_delay(failure, 1) is None


def test_short_retry_after_is_honoured():
    failure = LLMFailure(AI_RATE_LIMITED, True, 9)
    assert retry_delay(failure, 1, rand=lambda lo, hi: lo) == 9.0


def test_no_retry_after_the_last_attempt():
    failure = LLMFailure(AI_SERVICE_UNAVAILABLE, True, None)
    assert retry_delay(failure, MAX_ATTEMPTS - 1) is not None
    assert retry_delay(failure, MAX_ATTEMPTS) is None


def test_non_retryable_failure_stops():
    assert retry_delay(LLMFailure(AI_SERVICE_UNAVAILABLE, False), 1) is None


# ---------------------------------------------------------------------------
# call_with_llm_retry
# ---------------------------------------------------------------------------


async def _no_sleep(_seconds):
    return None


async def test_retry_helper_retries_then_succeeds():
    calls = []

    async def call():
        calls.append(1)
        if len(calls) == 1:
            raise _StatusError(503)
        return "ok"

    assert await call_with_llm_retry(call, sleep=_no_sleep) == "ok"
    assert len(calls) == 2


async def test_retry_helper_gives_up_with_safe_error():
    calls = []

    async def call():
        calls.append(1)
        raise _rate_limit('secret "quota_id": "projects/123/quota" /opt/venv/lib')

    with pytest.raises(AgentServiceError) as excinfo:
        await call_with_llm_retry(call, sleep=_no_sleep)

    assert len(calls) == MAX_ATTEMPTS
    assert excinfo.value.code == AI_RATE_LIMITED
    assert "quota_id" not in str(excinfo.value)
    assert "/opt/venv" not in str(excinfo.value)


async def test_retry_helper_makes_one_call_when_quota_is_gone():
    calls = []

    async def call():
        calls.append(1)
        raise _rate_limit('"retryDelay": "64400s"')

    with pytest.raises(AgentServiceError) as excinfo:
        await call_with_llm_retry(call, sleep=_no_sleep)

    assert len(calls) == 1
    assert excinfo.value.retry_after_seconds == 64400


# ---------------------------------------------------------------------------
# Messages and events
# ---------------------------------------------------------------------------


def test_safe_messages():
    assert "too many requests" in safe_message_for(AI_RATE_LIMITED)
    assert "about 2 minutes" in safe_message_for(AI_RATE_LIMITED, 90)
    assert "about 1 minute." in safe_message_for(AI_RATE_LIMITED, 30)
    assert "temporarily unavailable" in safe_message_for(AI_SERVICE_UNAVAILABLE)


def test_agent_service_error_str_is_the_safe_message():
    err = AgentServiceError(AI_RATE_LIMITED, retry_after_seconds=30)
    assert str(err) == err.safe_message == safe_message_for(AI_RATE_LIMITED, 30)
    assert err.recoverable is False


def test_error_event_shape():
    event = error_event(AI_RATE_LIMITED, "msg", "req-1", retry_after_seconds=30)
    assert isinstance(event, ChatEvent)
    assert event == {
        "type": "error",
        "code": AI_RATE_LIMITED,
        "message": "msg",
        "request_id": "req-1",
        "retry_after_seconds": 30,
    }
    assert "retry_after_seconds" not in error_event(AI_RATE_LIMITED, "m", "r")


def test_litellm_classes_resolve():
    assert llm_errors._RATE_LIMIT_CLASSES, "litellm.RateLimitError not found"
