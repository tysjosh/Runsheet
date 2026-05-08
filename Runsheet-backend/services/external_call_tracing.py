"""
Structured-log + circuit-breaker wrapper for every outbound external service call.

Task 12.9 / Requirements 10.4.1, 10.4.3, 10.4.4, 10.4.5 of the
``fuel-ops-hardening`` spec:

    > THE Platform SHALL emit a structured log event for every
    > Weather_Provider, Traffic_Provider, OCR, Integration_Connector,
    > and Rack_Price_Provider call with fields tenant_id, provider,
    > operation, duration_ms, status, and error_code (on failure).
    >
    > THE Platform SHALL implement circuit breakers on every external-
    > service client with a failure threshold of 5 consecutive failures
    > and a reset timeout of 60 seconds, returning fallback behavior as
    > specified in the capability requirements during the open state.

This module provides two building blocks that sit between the fuel-ops
services and the upstream APIs:

* :class:`CircuitBreaker` — a lightweight per-``(tenant_id, provider)``
  state machine with the three-state classical pattern:

    - ``CLOSED``  → ``OPEN`` once the ``failure_threshold`` consecutive
      failures are recorded (default 5).
    - ``OPEN`` → ``HALF_OPEN`` once the ``reset_timeout`` has elapsed
      since the transition into ``OPEN`` (default 60 seconds).
    - ``HALF_OPEN`` → ``CLOSED`` on the next success; any failure while
      ``HALF_OPEN`` snaps the key straight back to ``OPEN``.

  The breaker is keyed by a tuple so the same provider adapter can
  serve many tenants without one tenant's outage tripping another
  tenant's quota. Keys are opaque strings — callers typically build
  them as ``"{tenant_id}:{provider}"`` via :func:`make_breaker_key`,
  but any hashable identifier works.

* :func:`trace_external_call` — the async context manager wrapper used
  at every external call site. It:

    1. Consults the circuit breaker for the ``(tenant_id, provider)``
       pair (if one is supplied) and raises :class:`CircuitOpenError`
       before the wrapped call is ever invoked when the breaker is
       open, logging a ``status="circuit_open"`` event.
    2. Stamps an entry log event with ``tenant_id``, ``provider``,
       ``operation``, plus the caller-supplied ``extra`` key/value
       pairs.
    3. Times the call via :func:`time.monotonic` so the measurement is
       immune to wall-clock jumps.
    4. On clean exit, records a success on the breaker and stamps an
       exit log event with ``duration_ms`` and ``status="success"``
       (or whatever status the caller set via ``set_status()``).
    5. On :class:`asyncio.TimeoutError`, stamps ``status="timeout"``
       with ``error_code="timeout"`` and records a failure on the
       breaker.
    6. On any other exception, stamps ``status="error"`` with
       ``error_code`` derived from the exception type, records a
       failure on the breaker, and re-raises.
    7. If the caller supplied a Prometheus ``metric`` (typically from
       :mod:`services.metrics`), the wrapper increments it with the
       same ``(tenant_id, provider, status)`` label triple — matching
       the metric surface defined in Task 12.8 exactly.

The context object :class:`ExternalCallContext` also exposes
``set_status(...)``, ``set_error_code(...)``, and ``add_extra(...)`` so
callers can override the default behaviour for cases the base logic
cannot observe on its own (e.g. a cache hit that should be logged as
``status="cache_hit"`` even though the wrapped call succeeded, or a
fallback path that should be logged as ``status="fallback"``).

Structured logging compatibility
--------------------------------

The wrapper uses ``logger.info`` / ``logger.warning`` with the ``extra=``
kwarg so any structured-log handler (``python-json-logger``, Datadog
log shipper, CloudWatch logger, …) can promote the fields to indexed
attributes. A plain ``logging.Formatter`` still sees the rendered
message, which embeds the key fields, so text grep remains useful in
development.

Tenant isolation
----------------

The wrapper itself does not enforce tenant isolation — that is the
caller's responsibility (the upstream services already scope every
request by ``tenant_id``). What the wrapper *does* guarantee is that
every emitted log line and every circuit-breaker update carries the
correct ``tenant_id``, so a downstream operator can grep failures
per tenant and the breaker state for tenant A never influences the
breaker state for tenant B.

Validates: Requirements 10.4.1, 10.4.3, 10.4.4, 10.4.5.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, Hashable, Optional

logger = logging.getLogger(__name__)


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerState",
    "CircuitOpenError",
    "ExternalCallContext",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_RESET_TIMEOUT_SECONDS",
    "default_circuit_breaker",
    "make_breaker_key",
    "trace_external_call",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Consecutive-failure threshold at which a ``CLOSED`` breaker snaps to
#: ``OPEN``. Matches Requirement 10.4.3 (5 failures).
DEFAULT_FAILURE_THRESHOLD: int = 5

#: Seconds to wait before an ``OPEN`` breaker transitions to
#: ``HALF_OPEN``. Matches Requirement 10.4.3 (60s).
DEFAULT_RESET_TIMEOUT_SECONDS: float = 60.0

#: Type alias for a breaker key. We expose the alias so callers that
#: want to type-hint their own wrappers get the right shape. Any
#: hashable value works; the convention used across the fuel-ops
#: services is ``f"{tenant_id}:{provider}"``.
BreakerKey = Hashable


# ---------------------------------------------------------------------------
# Circuit-breaker state machine
# ---------------------------------------------------------------------------


class CircuitBreakerState(str, Enum):
    """Discrete states of the per-key circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _BreakerEntry:
    """Mutable state tracked per breaker key.

    The breaker uses monotonic seconds (via an injectable clock) for
    the reset timer so wall-clock jumps — e.g. NTP resync during a
    long ``OPEN`` window — never cause the breaker to reset early or
    stay open forever.
    """

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    opened_at: Optional[float] = None


class CircuitOpenError(RuntimeError):
    """Raised when a call is refused because the breaker is ``OPEN``.

    Callers catch this to emit a ``traffic_fallback: true`` /
    ``weather_fallback: true`` / ``rack_price_fallback: true`` annotation
    or to surface a graceful degradation path to their own callers. The
    wrapper emits a ``status="circuit_open"`` log event before raising
    so dashboards can track blocked calls without grepping stack
    traces.

    Attributes:
        key: The breaker key that was open (typically
            ``"{tenant_id}:{provider}"``).
        retry_after_seconds: Approximate seconds remaining until the
            breaker transitions to ``HALF_OPEN``. Optional because we
            compute it opportunistically from the stored ``opened_at``
            and the configured ``reset_timeout``.
    """

    def __init__(
        self,
        key: BreakerKey,
        *,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        self.key = key
        self.retry_after_seconds = retry_after_seconds
        parts = [f"circuit breaker is open for key={key!r}"]
        if retry_after_seconds is not None and retry_after_seconds > 0:
            parts.append(f"retry in {retry_after_seconds:.1f}s")
        super().__init__("; ".join(parts))


class CircuitBreaker:
    """Per-key consecutive-failure circuit breaker.

    The breaker is intentionally coarse-grained: it tracks a single
    ``CircuitBreakerState`` per opaque key. Keys are typically the
    ``(tenant_id, provider)`` pair joined by ``:`` (see
    :func:`make_breaker_key`), but any hashable value is accepted so
    callers can push the granularity up (per-endpoint) or down
    (per-provider only) without changing the class.

    Thread safety: every public method grabs the same
    :class:`asyncio.Lock` so concurrent ``record_success`` and
    ``record_failure`` calls from the async scheduler never race. The
    lock is constructed lazily so a breaker built outside of an async
    context (e.g. at module import time) still works when the first
    coroutine hits it.

    Args:
        failure_threshold: Consecutive failures that trip the breaker
            from ``CLOSED`` to ``OPEN``. Must be >= 1. Defaults to
            :data:`DEFAULT_FAILURE_THRESHOLD`.
        reset_timeout_seconds: Seconds an ``OPEN`` breaker waits
            before the next :meth:`is_open` check transitions it to
            ``HALF_OPEN``. Must be > 0. Defaults to
            :data:`DEFAULT_RESET_TIMEOUT_SECONDS`.
        clock: Optional zero-arg callable returning a monotonically
            increasing seconds value. Defaults to
            :func:`time.monotonic`; tests inject a deterministic
            counter to avoid sleeping.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_timeout_seconds: float = DEFAULT_RESET_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        self._failure_threshold = int(failure_threshold)
        self._reset_timeout = float(reset_timeout_seconds)
        self._clock = clock
        self._entries: Dict[BreakerKey, _BreakerEntry] = {}
        # The lock is created lazily so the breaker can be instantiated
        # at module import time (no running loop) but still honour
        # asyncio synchronization once a loop is running.
        self._lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def failure_threshold(self) -> int:
        """Return the configured consecutive-failure threshold."""
        return self._failure_threshold

    @property
    def reset_timeout_seconds(self) -> float:
        """Return the configured ``OPEN`` → ``HALF_OPEN`` reset window."""
        return self._reset_timeout

    def state_for(self, key: BreakerKey) -> CircuitBreakerState:
        """Return the current state for ``key`` without mutating it.

        Useful for introspection / dashboards. A key that has never
        been touched reports :attr:`CircuitBreakerState.CLOSED`.
        """

        entry = self._entries.get(key)
        if entry is None:
            return CircuitBreakerState.CLOSED
        return entry.state

    async def is_open(self, key: BreakerKey) -> bool:
        """Return True if ``key`` is currently rejecting calls.

        The call also promotes ``OPEN`` → ``HALF_OPEN`` when the reset
        window has elapsed, so callers can use this method both as a
        gate and as a trigger for the half-open probe.
        """

        lock = self._get_lock()
        async with lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.state is CircuitBreakerState.CLOSED:
                return False
            if entry.state is CircuitBreakerState.HALF_OPEN:
                # Half-open lets a single probe call through; the
                # ``is_open`` gate therefore returns False so the
                # caller can try it. The success/failure of that probe
                # determines the next transition.
                return False
            # state is OPEN. Check whether the reset window has elapsed.
            if entry.opened_at is None:
                # Defensive: an OPEN entry without opened_at is a bug;
                # treat it as ready for a probe to avoid dead-locking.
                entry.state = CircuitBreakerState.HALF_OPEN
                return False
            elapsed = self._clock() - entry.opened_at
            if elapsed >= self._reset_timeout:
                entry.state = CircuitBreakerState.HALF_OPEN
                return False
            return True

    async def record_success(self, key: BreakerKey) -> None:
        """Record a successful call for ``key``.

        In ``CLOSED`` / ``HALF_OPEN`` this resets the failure counter
        and transitions the entry to ``CLOSED``. A key that has never
        been touched records a no-op success so the dict stays sparse.
        """

        lock = self._get_lock()
        async with lock:
            entry = self._entries.get(key)
            if entry is None:
                # First-ever observation is a success — keep the dict
                # sparse by not recording an entry until the first
                # failure shows up.
                return
            entry.state = CircuitBreakerState.CLOSED
            entry.failure_count = 0
            entry.opened_at = None

    async def record_failure(self, key: BreakerKey) -> None:
        """Record a failed call for ``key``.

        Behaviour by state:
            * ``CLOSED``: increment the counter. If it reaches the
              threshold, transition to ``OPEN`` and stamp ``opened_at``.
            * ``HALF_OPEN``: snap straight back to ``OPEN`` and
              re-stamp ``opened_at``.
            * ``OPEN``: no state change (already open); we still bump
              the counter so dashboards can see how many requests piled
              up while the breaker was open.
        """

        lock = self._get_lock()
        async with lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _BreakerEntry()
                self._entries[key] = entry

            if entry.state is CircuitBreakerState.HALF_OPEN:
                entry.state = CircuitBreakerState.OPEN
                entry.opened_at = self._clock()
                # Preserve the fail count so operator dashboards can
                # see the total-failures-since-last-success metric.
                entry.failure_count += 1
                return

            entry.failure_count += 1

            if entry.state is CircuitBreakerState.CLOSED:
                if entry.failure_count >= self._failure_threshold:
                    entry.state = CircuitBreakerState.OPEN
                    entry.opened_at = self._clock()
                return

            # OPEN — already open, just refresh opened_at so the
            # reset-window never leaks backwards if the clock jumped.
            if entry.opened_at is None:
                entry.opened_at = self._clock()

    async def reset(self, key: Optional[BreakerKey] = None) -> None:
        """Reset ``key`` (or the entire breaker when ``key`` is ``None``).

        Exposed for admin endpoints and tests. Never raises.
        """

        lock = self._get_lock()
        async with lock:
            if key is None:
                self._entries.clear()
                return
            self._entries.pop(key, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        """Return the breaker lock, creating it on first use.

        ``asyncio.Lock()`` binds itself to the currently running event
        loop. Constructing the lock lazily on first use means the
        breaker can be instantiated without an active loop (e.g. in a
        module-level singleton) and still synchronize correctly once
        coroutines start hitting it.
        """

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _retry_after_seconds(self, key: BreakerKey) -> Optional[float]:
        """Return the best-effort retry-after hint for an ``OPEN`` entry.

        Used by :func:`trace_external_call` to attach a retry hint to
        :class:`CircuitOpenError`. No lock is taken — we read the
        mutable ``_BreakerEntry`` fields once and return; the hint is
        advisory so a read-race is acceptable.
        """

        entry = self._entries.get(key)
        if entry is None or entry.opened_at is None:
            return None
        elapsed = self._clock() - entry.opened_at
        remaining = self._reset_timeout - elapsed
        return remaining if remaining > 0 else 0.0


# ---------------------------------------------------------------------------
# Key helper
# ---------------------------------------------------------------------------


def make_breaker_key(tenant_id: str, provider: str) -> str:
    """Return the canonical breaker key for a ``(tenant_id, provider)`` pair.

    Callers MAY build keys however they want (the breaker treats the
    key opaquely), but every fuel-ops service that uses
    :func:`trace_external_call` should funnel through this helper so
    the key shape is consistent across log lines and metric labels.
    """

    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    return f"{tenant_id.strip()}:{provider.strip()}"


#: Process-wide default :class:`CircuitBreaker` used by every fuel-ops
#: external call site. Sharing a single breaker across providers is
#: fine because the keys are ``(tenant_id, provider)`` tuples — one
#: tenant's Mapbox outage never influences the same tenant's NOAA
#: breaker. Tests can inject their own breaker via the
#: ``circuit_breaker=`` kwarg on :func:`trace_external_call` so this
#: module-level singleton never leaks state between test cases.
default_circuit_breaker: CircuitBreaker = CircuitBreaker()


# ---------------------------------------------------------------------------
# Context object surfaced inside the ``async with`` block
# ---------------------------------------------------------------------------


@dataclass
class ExternalCallContext:
    """Mutable context object surfaced inside :func:`trace_external_call`.

    Callers use the object to override the auto-computed status / error
    code or to attach extra log fields. A typical usage::

        async with trace_external_call(
            tenant_id=tid,
            provider="mapbox",
            operation="get_matrix",
            circuit_breaker=breaker,
            metric=traffic_metric,
        ) as call:
            try:
                return await provider.get_matrix(...)
            except TrafficBudgetExceeded:
                call.set_status("budget_exceeded")
                raise
            except httpx.HTTPError as exc:
                call.set_error_code("http_error")
                raise

    The ``set_status`` / ``set_error_code`` calls are additive: the
    wrapper will honour whatever the caller stamped last, falling back
    to its auto-computed values when nothing was set.
    """

    tenant_id: str
    provider: str
    operation: str
    status: Optional[str] = None
    error_code: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def set_status(self, status: str) -> None:
        """Override the auto-computed status (``success`` / ``error`` / …)."""

        if not isinstance(status, str) or not status.strip():
            raise ValueError("status must be a non-empty string")
        self.status = status.strip()

    def set_error_code(self, error_code: Optional[str]) -> None:
        """Override the auto-computed error code.

        ``None`` clears the value — useful when a caller wants to
        convert an exception into a soft-fail and doesn't want an
        error_code in the log line.
        """

        if error_code is None:
            self.error_code = None
            return
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code must be a non-empty string or None")
        self.error_code = error_code.strip()

    def add_extra(self, **kwargs: Any) -> None:
        """Merge additional key/value pairs into the log ``extra`` payload."""

        self.extra.update(kwargs)


# ---------------------------------------------------------------------------
# Wrapper context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def trace_external_call(
    tenant_id: str,
    provider: str,
    operation: str,
    *,
    circuit_breaker: Optional[CircuitBreaker] = None,
    metric: Optional[Any] = None,
    breaker_key: Optional[BreakerKey] = None,
    log: logging.Logger = logger,
    extra: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[ExternalCallContext]:
    """Wrap a single external-service call with logs, metrics, and a breaker.

    Contract:

    * Emits ``external_call_started`` at ``INFO`` before the call.
    * Times the call and emits ``external_call_finished`` at ``INFO``
      on success, ``external_call_failed`` at ``WARNING`` on failure,
      and ``external_call_rejected`` at ``WARNING`` when the breaker
      was already open.
    * The finished/failed log lines always include ``tenant_id``,
      ``provider``, ``operation``, ``duration_ms``, ``status``, and
      (on failure) ``error_code``. Additional keys can be attached via
      :meth:`ExternalCallContext.add_extra`.
    * When ``circuit_breaker`` is supplied, the wrapper:
        - Short-circuits with :class:`CircuitOpenError` when
          :meth:`CircuitBreaker.is_open` returns ``True``.
        - Records a success on clean exit.
        - Records a failure on :class:`asyncio.TimeoutError` or any
          other exception.
    * When ``metric`` is supplied (typically a Prometheus ``Counter``),
      the wrapper calls ``metric.labels(tenant_id=..., provider=...,
      status=...).inc()`` with the final (possibly overridden) status.
      A caller that wants to skip metric recording for a given pass
      can call ``metric=None``.

    Args:
        tenant_id: Owning tenant. Required, non-empty.
        provider: Short provider identifier (``noaa``, ``mapbox``,
            ``textract``, ``quickbooks_online``, ``opis``, ``nws``).
            Required, non-empty.
        operation: Short operation identifier (``get_hdd``,
            ``get_matrix``, ``extract``, ``sync_pull``, …). Required,
            non-empty.
        circuit_breaker: Optional :class:`CircuitBreaker` instance. The
            wrapper uses :func:`make_breaker_key` to build the key
            unless ``breaker_key`` is supplied.
        metric: Optional Prometheus metric exposing a ``.labels(...)``
            chain. Typically a :class:`prometheus_client.Counter`;
            the wrapper only requires ``labels(...).inc()``.
        breaker_key: Optional override for the breaker key. Useful
            when a caller wants to share the breaker across multiple
            operations on the same provider (pass just ``provider``
            as the key, for example).
        log: Optional logger override. Defaults to this module's
            logger so every emitted event carries the
            ``services.external_call_tracing`` name.
        extra: Optional dict of additional log fields merged into the
            entry / exit events.

    Yields:
        :class:`ExternalCallContext` with ``set_status``,
        ``set_error_code``, ``add_extra`` helpers.
    """

    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")

    tenant_id = tenant_id.strip()
    provider = provider.strip()
    operation = operation.strip()

    ctx = ExternalCallContext(
        tenant_id=tenant_id,
        provider=provider,
        operation=operation,
        extra=dict(extra or {}),
    )

    effective_breaker_key: Optional[BreakerKey] = None
    if circuit_breaker is not None:
        effective_breaker_key = (
            breaker_key if breaker_key is not None
            else make_breaker_key(tenant_id, provider)
        )

    # ------------------------------------------------------------------
    # 1) Pre-flight: breaker check.
    # ------------------------------------------------------------------

    if circuit_breaker is not None and effective_breaker_key is not None:
        if await circuit_breaker.is_open(effective_breaker_key):
            retry_after = circuit_breaker._retry_after_seconds(effective_breaker_key)
            ctx.set_status("circuit_open")
            ctx.set_error_code("circuit_open")
            _emit_rejected_event(
                log=log,
                ctx=ctx,
                retry_after_seconds=retry_after,
            )
            _inc_metric(metric, ctx)
            raise CircuitOpenError(
                effective_breaker_key,
                retry_after_seconds=retry_after,
            )

    # ------------------------------------------------------------------
    # 2) Entry log event.
    # ------------------------------------------------------------------

    _emit_started_event(log=log, ctx=ctx)
    started_monotonic = time.monotonic()

    try:
        yield ctx
    except asyncio.TimeoutError:
        duration_ms = _elapsed_ms(started_monotonic)
        if ctx.status is None:
            ctx.set_status("timeout")
        if ctx.error_code is None:
            ctx.set_error_code("timeout")
        _emit_failed_event(log=log, ctx=ctx, duration_ms=duration_ms)
        _inc_metric(metric, ctx)
        if circuit_breaker is not None and effective_breaker_key is not None:
            await circuit_breaker.record_failure(effective_breaker_key)
        raise
    except CircuitOpenError:
        # A downstream call inside the block may itself have surfaced
        # a CircuitOpenError. Propagate without double-counting: the
        # inner wrapper already emitted its own events and breaker
        # updates.
        raise
    except BaseException as exc:  # noqa: BLE001 — intentional catch-all
        duration_ms = _elapsed_ms(started_monotonic)
        if ctx.status is None:
            ctx.set_status("error")
        if ctx.error_code is None:
            ctx.set_error_code(_error_code_for(exc))
        _emit_failed_event(log=log, ctx=ctx, duration_ms=duration_ms, exc=exc)
        _inc_metric(metric, ctx)
        if circuit_breaker is not None and effective_breaker_key is not None:
            await circuit_breaker.record_failure(effective_breaker_key)
        raise
    else:
        duration_ms = _elapsed_ms(started_monotonic)
        if ctx.status is None:
            ctx.set_status("success")
        _emit_finished_event(log=log, ctx=ctx, duration_ms=duration_ms)
        _inc_metric(metric, ctx)
        if circuit_breaker is not None and effective_breaker_key is not None:
            # A caller-overridden status that represents a soft-fail
            # (e.g. "timeout" returned by the wrapper's own fallback
            # path) should still count as a failure against the
            # breaker. Anything else counts as a success.
            if _status_is_failure(ctx.status):
                await circuit_breaker.record_failure(effective_breaker_key)
            else:
                await circuit_breaker.record_success(effective_breaker_key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Statuses that represent a soft-fail even though the wrapped block
# exited cleanly. A caller that catches a provider timeout internally
# and calls ``ctx.set_status("timeout")`` before returning a fallback
# still counts against the breaker so a sustained outage trips it.
_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"timeout", "error", "circuit_open", "budget_exceeded"}
)


def _status_is_failure(status: Optional[str]) -> bool:
    """Return True when ``status`` represents a failure for breaker purposes."""

    if not status:
        return False
    return status in _FAILURE_STATUSES


def _elapsed_ms(started_monotonic: float) -> int:
    """Return elapsed milliseconds since ``started_monotonic``."""

    return max(0, int((time.monotonic() - started_monotonic) * 1000))


def _error_code_for(exc: BaseException) -> str:
    """Derive a stable error code from an exception instance.

    The code is the exception class name lowercased (``HTTPError`` →
    ``http_error``, ``ValueError`` → ``value_error``). A small set of
    common provider exceptions get friendlier names so dashboards
    aggregate cleanly.
    """

    # Fast-path common exception types so dashboards stay clean.
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, TimeoutError):  # builtin alias
        return "timeout"

    name = type(exc).__name__
    # Convert CamelCase → snake_case. We do this by hand to avoid
    # importing ``re`` for a one-liner transformation.
    out_chars: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out_chars.append("_")
        out_chars.append(ch.lower())
    return "".join(out_chars) or "unknown"


def _base_extra(ctx: ExternalCallContext) -> Dict[str, Any]:
    """Return the base structured-log ``extra`` dict for the context."""

    payload: Dict[str, Any] = {
        "tenant_id": ctx.tenant_id,
        "provider": ctx.provider,
        "operation": ctx.operation,
    }
    if ctx.extra:
        payload.update(ctx.extra)
    return payload


def _emit_started_event(*, log: logging.Logger, ctx: ExternalCallContext) -> None:
    """Emit the ``external_call_started`` log event."""

    extra = _base_extra(ctx)
    extra["event"] = "external_call_started"
    log.info(
        "external_call_started tenant_id=%s provider=%s operation=%s",
        ctx.tenant_id,
        ctx.provider,
        ctx.operation,
        extra=extra,
    )


def _emit_finished_event(
    *, log: logging.Logger, ctx: ExternalCallContext, duration_ms: int
) -> None:
    """Emit the ``external_call_finished`` log event for a clean exit."""

    status = ctx.status or "success"
    extra = _base_extra(ctx)
    extra["event"] = "external_call_finished"
    extra["duration_ms"] = duration_ms
    extra["status"] = status
    if ctx.error_code:
        extra["error_code"] = ctx.error_code
    log.info(
        "external_call_finished tenant_id=%s provider=%s operation=%s "
        "duration_ms=%d status=%s",
        ctx.tenant_id,
        ctx.provider,
        ctx.operation,
        duration_ms,
        status,
        extra=extra,
    )


def _emit_failed_event(
    *,
    log: logging.Logger,
    ctx: ExternalCallContext,
    duration_ms: int,
    exc: Optional[BaseException] = None,
) -> None:
    """Emit the ``external_call_failed`` log event for an exception."""

    status = ctx.status or "error"
    error_code = ctx.error_code or "unknown"
    extra = _base_extra(ctx)
    extra["event"] = "external_call_failed"
    extra["duration_ms"] = duration_ms
    extra["status"] = status
    extra["error_code"] = error_code
    if exc is not None:
        # Keep the repr bounded so a 100k-char stack message doesn't
        # flood the log handler. 500 chars comfortably covers every
        # provider SDK error we've seen in practice.
        extra["error_details"] = repr(exc)[:500]
    log.warning(
        "external_call_failed tenant_id=%s provider=%s operation=%s "
        "duration_ms=%d status=%s error_code=%s",
        ctx.tenant_id,
        ctx.provider,
        ctx.operation,
        duration_ms,
        status,
        error_code,
        extra=extra,
    )


def _emit_rejected_event(
    *,
    log: logging.Logger,
    ctx: ExternalCallContext,
    retry_after_seconds: Optional[float],
) -> None:
    """Emit the ``external_call_rejected`` event when the breaker is open."""

    extra = _base_extra(ctx)
    extra["event"] = "external_call_rejected"
    extra["status"] = ctx.status or "circuit_open"
    extra["error_code"] = ctx.error_code or "circuit_open"
    if retry_after_seconds is not None:
        extra["retry_after_seconds"] = round(retry_after_seconds, 3)
    log.warning(
        "external_call_rejected tenant_id=%s provider=%s operation=%s "
        "status=%s error_code=%s",
        ctx.tenant_id,
        ctx.provider,
        ctx.operation,
        extra["status"],
        extra["error_code"],
        extra=extra,
    )


def _inc_metric(metric: Any, ctx: ExternalCallContext) -> None:
    """Increment ``metric`` with the ``(tenant_id, provider, status)`` labels.

    Swallows every exception so a broken Prometheus registry (which
    should be impossible given the module-level singletons, but we
    code defensively) never takes down a real provider call.
    """

    if metric is None:
        return
    status = ctx.status or "success"
    try:
        metric.labels(
            tenant_id=ctx.tenant_id,
            provider=ctx.provider,
            status=status,
        ).inc()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "external_call_tracing: metric increment failed "
            "(tenant=%s provider=%s operation=%s status=%s): %s",
            ctx.tenant_id,
            ctx.provider,
            ctx.operation,
            status,
            exc,
        )
