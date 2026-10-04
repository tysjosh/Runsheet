"""
LLM failure classification, bounded retry, and normalized chat events.

Staging findings F3/F4: a Vertex ``RateLimitError`` escaped the specialist,
the orchestrator wrapped it as ``f"[{target}] Error processing request: {e}"``
and ``/api/chat`` returned it as a normal 200 answer, provider JSON, quota ids
and traceback fragments included. Nothing retried it: Strands only retries its
own ``ModelThrottledException``, which the LiteLLM model never raises.

This module is the one home for that policy, shared by the orchestrator, the
legacy streaming path and ``/api/chat/fallback``:

* :func:`classify_llm_exception` maps any exception to an error code and says
  whether it is worth retrying.
* :func:`retry_delay` applies the backoff policy (3 attempts, 1 s doubling,
  8 s cap, equal jitter) and refuses to wait out a long ``Retry-After``: a
  free-tier daily quota said 64 400 s, so retrying would only spend calls.
* :class:`AgentServiceError` carries a vetted, user-safe message. Its ``str()``
  is that message, so it can never leak provider text.
* :class:`ChatEvent` marks an event as already normalized. Strands 1.24 events
  can carry a ``"type"`` key of their own, so the class, not the key, is the
  marker.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

AI_RATE_LIMITED = "AI_RATE_LIMITED"
AI_SERVICE_UNAVAILABLE = "AI_SERVICE_UNAVAILABLE"

#: One try plus two retries.
MAX_ATTEMPTS = 3
BASE_DELAY_S = 1.0
MAX_DELAY_S = 8.0
#: A longer ``Retry-After`` is not waited out: the request fails at once and
#: reports ``retry_after_seconds`` instead.
MAX_HONOURED_RETRY_AFTER_S = 10.0


def _litellm_classes(*names: str) -> tuple:
    """Resolve litellm exception classes by name; absent ones are skipped so
    this works on both the pinned 1.55 and newer local installs."""
    try:
        import litellm
    except Exception:  # pragma: no cover - litellm is a hard dependency
        return ()
    return tuple(
        cls for cls in (getattr(litellm, n, None) for n in names)
        if isinstance(cls, type)
    )


_RATE_LIMIT_CLASSES = _litellm_classes("RateLimitError")
_UNAVAILABLE_CLASSES = _litellm_classes(
    "ServiceUnavailableError", "InternalServerError", "Timeout", "APIConnectionError"
) + (asyncio.TimeoutError, TimeoutError)

_RETRYABLE_STATUS = {500, 502, 503, 504}
_RATE_LIMIT_TEXT = re.compile(r"rate.?limit|quota|resource_exhausted|\b429\b", re.I)
_UNAVAILABLE_TEXT = re.compile(
    r"overloaded|high demand|unavailable|time[d ]?\s?out|connection", re.I
)
_RETRY_DELAY_TEXT = re.compile(r"retryDelay\"?\s*[:=]\s*\"?(\d+(?:\.\d+)?)s", re.I)


@dataclass(frozen=True)
class LLMFailure:
    """Outcome of :func:`classify_llm_exception`."""

    code: str
    retryable: bool
    retry_after_seconds: Optional[int] = None


class AgentServiceError(Exception):
    """An AI-service failure with a user-safe message.

    ``recoverable = False`` tells the execution planner not to re-run the step:
    the bounded retry already happened.
    """

    recoverable = False

    def __init__(
        self,
        code: str,
        safe_message: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
        domain: Optional[str] = None,
    ) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.domain = domain
        self.safe_message = safe_message or safe_message_for(code, retry_after_seconds)
        super().__init__(self.safe_message)

    def __str__(self) -> str:
        return self.safe_message


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` plus what it wraps (Strands ``EventLoopException`` keeps the
    provider error in ``original_exception`` and ``__cause__``)."""
    seen = set()
    stack = [exc]
    while stack and len(seen) < 8:
        cur = stack.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack.extend(
            [getattr(cur, "original_exception", None), cur.__cause__, cur.__context__]
        )


def _status_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _header(headers: Any, name: str) -> Optional[str]:
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None and hasattr(headers, "items"):
            value = next(
                (v for k, v in headers.items() if str(k).lower() == name.lower()), None
            )
    except Exception:
        return None
    return value


def retry_after_seconds(exc: BaseException) -> Optional[int]:
    """Server-requested wait, from a ``Retry-After`` header or Google's
    ``RetryInfo.retryDelay`` (``"64400s"``) in the message; else ``None``."""
    for cur in _exception_chain(exc):
        response = getattr(cur, "response", None)
        for headers in (
            getattr(response, "headers", None),
            getattr(cur, "litellm_response_headers", None),
            getattr(cur, "headers", None),
        ):
            value = _header(headers, "retry-after")
            if value is None:
                continue
            try:
                return max(0, math.ceil(float(value)))
            except (TypeError, ValueError):
                continue  # HTTP-date form: fall back to the message
    for cur in _exception_chain(exc):
        match = _RETRY_DELAY_TEXT.search(str(cur))
        if match:
            return math.ceil(float(match.group(1)))
    return None


def classify_llm_exception(exc: BaseException) -> LLMFailure:
    """Map an exception to an AI error code and whether a retry may help."""
    if isinstance(exc, AgentServiceError):
        return LLMFailure(exc.code, False, exc.retry_after_seconds)

    retry_after = retry_after_seconds(exc)
    chain = list(_exception_chain(exc))

    for cur in chain:
        if (_RATE_LIMIT_CLASSES and isinstance(cur, _RATE_LIMIT_CLASSES)) or _status_code(cur) == 429:
            return LLMFailure(AI_RATE_LIMITED, True, retry_after)
    for cur in chain:
        if isinstance(cur, _UNAVAILABLE_CLASSES) or _status_code(cur) in _RETRYABLE_STATUS:
            return LLMFailure(AI_SERVICE_UNAVAILABLE, True, retry_after)

    text = " ".join(str(cur) for cur in chain)
    if _RATE_LIMIT_TEXT.search(text):
        return LLMFailure(AI_RATE_LIMITED, True, retry_after)
    if _UNAVAILABLE_TEXT.search(text):
        return LLMFailure(AI_SERVICE_UNAVAILABLE, True, retry_after)
    return LLMFailure(AI_SERVICE_UNAVAILABLE, False, retry_after)


def backoff_delay(attempt: int, rand: Callable[[float, float], float] = random.uniform) -> float:
    """Equal-jitter exponential backoff after failed ``attempt`` (1-based)."""
    base = min(MAX_DELAY_S, BASE_DELAY_S * (2 ** max(0, attempt - 1)))
    return base / 2 + rand(0, base / 2)


def backoff_delays(rand: Callable[[float, float], float] = random.uniform) -> list:
    """The waits between the :data:`MAX_ATTEMPTS` attempts."""
    return [backoff_delay(a, rand) for a in range(1, MAX_ATTEMPTS)]


def retry_delay(
    failure: LLMFailure,
    attempt: int,
    rand: Callable[[float, float], float] = random.uniform,
) -> Optional[float]:
    """Seconds to wait before the next attempt, or ``None`` to stop now."""
    if not failure.retryable or attempt >= MAX_ATTEMPTS:
        return None
    delay = backoff_delay(attempt, rand)
    if failure.retry_after_seconds is not None:
        if failure.retry_after_seconds > MAX_HONOURED_RETRY_AFTER_S:
            return None
        delay = max(float(failure.retry_after_seconds), delay)
    return delay


def safe_message_for(code: str, retry_after: Optional[int] = None) -> str:
    """The user-facing text for an AI error code. Never includes provider text."""
    if code == AI_RATE_LIMITED:
        if retry_after:
            minutes = max(1, math.ceil(retry_after / 60))
            if minutes >= 120:
                hours = math.ceil(minutes / 60)
                wait = f"about {hours} hours"
            else:
                wait = f"about {minutes} minute{'s' if minutes != 1 else ''}"
            return (
                "The AI assistant is receiving too many requests right now. "
                f"Please try again in {wait}."
            )
        return (
            "The AI assistant is receiving too many requests right now. "
            "Please try again in a few minutes."
        )
    return "The AI assistant is temporarily unavailable. Please try again shortly."


def to_agent_service_error(
    exc: BaseException, domain: Optional[str] = None
) -> AgentServiceError:
    """Wrap ``exc`` as an :class:`AgentServiceError` (identity if it is one)."""
    if isinstance(exc, AgentServiceError):
        return exc
    failure = classify_llm_exception(exc)
    return AgentServiceError(
        failure.code, retry_after_seconds=failure.retry_after_seconds, domain=domain
    )


async def call_with_llm_retry(
    call: Callable[[], Awaitable[T]],
    *,
    describe: str = "LLM call",
    domain: Optional[str] = None,
    request_id: Optional[str] = None,
    sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    rand: Callable[[float, float], float] = random.uniform,
) -> T:
    """Await ``call()`` with the bounded retry policy.

    Logs every failure server-side with full detail and raises
    :class:`AgentServiceError` (safe text only) once retries are exhausted or
    not worth it.
    """
    sleep = sleep or asyncio.sleep
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call()
        except AgentServiceError:
            raise
        except Exception as exc:
            failure = classify_llm_exception(exc)
            logger.warning(
                "%s failed (attempt %d/%d, code=%s, request_id=%s)",
                describe, attempt, MAX_ATTEMPTS, failure.code, request_id,
                exc_info=exc,
            )
            delay = retry_delay(failure, attempt, rand)
            if delay is None:
                raise AgentServiceError(
                    failure.code,
                    retry_after_seconds=failure.retry_after_seconds,
                    domain=domain,
                ) from exc
            await sleep(delay)


# ---------------------------------------------------------------------------
# Normalized chat events
# ---------------------------------------------------------------------------


class ChatEvent(dict):
    """A chat stream event already in the wire shape ``/api/chat`` sends.

    The endpoint forwards instances verbatim; plain dicts are raw Strands
    events and still go through its legacy translation.
    """


def text_event(content: str) -> ChatEvent:
    return ChatEvent(type="text", content=content)


def tool_event(tool_name: str, tool_input: Any = None) -> ChatEvent:
    return ChatEvent(type="tool", tool_name=tool_name, tool_input=tool_input or {})


def tool_result_event(tool_name: str, tool_output: str) -> ChatEvent:
    return ChatEvent(type="tool_result", tool_name=tool_name, tool_output=tool_output)


def status_event(stage: str, **details: Any) -> ChatEvent:
    return ChatEvent(type="status", stage=stage, **details)


def error_event(
    code: str,
    message: str,
    request_id: Optional[str],
    retry_after_seconds: Optional[int] = None,
) -> ChatEvent:
    event = ChatEvent(type="error", code=code, message=message, request_id=request_id or None)
    if retry_after_seconds is not None:
        event["retry_after_seconds"] = retry_after_seconds
    return event


def error_event_for(err: AgentServiceError, request_id: Optional[str]) -> ChatEvent:
    return error_event(err.code, err.safe_message, request_id, err.retry_after_seconds)


def done_event() -> ChatEvent:
    return ChatEvent(type="done")


__all__ = [
    "AI_RATE_LIMITED",
    "AI_SERVICE_UNAVAILABLE",
    "AgentServiceError",
    "BASE_DELAY_S",
    "ChatEvent",
    "LLMFailure",
    "MAX_ATTEMPTS",
    "MAX_DELAY_S",
    "MAX_HONOURED_RETRY_AFTER_S",
    "backoff_delay",
    "backoff_delays",
    "call_with_llm_retry",
    "classify_llm_exception",
    "done_event",
    "error_event",
    "error_event_for",
    "retry_after_seconds",
    "retry_delay",
    "safe_message_for",
    "status_event",
    "text_event",
    "to_agent_service_error",
    "tool_event",
    "tool_result_event",
]
