"""
Unit tests for :mod:`services.external_call_tracing`.

Covers Task 12.9 / Requirements 10.4.1, 10.4.3, 10.4.4, 10.4.5 of the
``fuel-ops-hardening`` spec:

* Every external-service call emits a structured log event carrying
  ``tenant_id``, ``provider``, ``operation``, ``duration_ms``,
  ``status``, and (on failure) ``error_code`` (Req 10.4.1).
* A circuit breaker opens after 5 consecutive failures, returns to
  half-open after 60 seconds, and closes on the next half-open success
  (Req 10.4.3).
* Calls during the open state raise :class:`CircuitOpenError` without
  invoking the wrapped function (Req 10.4.4).
* The wrapper increments the injected Prometheus metric with the
  canonical ``(tenant_id, provider, status)`` label triple (Req 10.4.5).

Validates: Requirements 10.4.1, 10.4.3, 10.4.4, 10.4.5.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import pytest

from services.external_call_tracing import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_RESET_TIMEOUT_SECONDS,
    CircuitBreaker,
    CircuitBreakerState,
    CircuitOpenError,
    make_breaker_key,
    trace_external_call,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock stub controlled by the test."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class _RecordingMetric:
    """Stand-in Prometheus counter that records label-sets and inc() calls."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def labels(self, **label_values: Any) -> "_RecordingMetric._Labelled":
        return _RecordingMetric._Labelled(self, label_values)

    class _Labelled:
        def __init__(self, parent: "_RecordingMetric", labels: Dict[str, Any]) -> None:
            self._parent = parent
            self._labels = labels

        def inc(self, amount: float = 1.0) -> None:
            self._parent.calls.append({**self._labels, "amount": amount})


# ---------------------------------------------------------------------------
# make_breaker_key
# ---------------------------------------------------------------------------


class TestMakeBreakerKey:
    def test_joins_tenant_and_provider(self) -> None:
        assert make_breaker_key("tenant-1", "noaa") == "tenant-1:noaa"

    def test_strips_whitespace(self) -> None:
        assert make_breaker_key(" tenant-1 ", "  noaa ") == "tenant-1:noaa"

    def test_rejects_blank_tenant(self) -> None:
        with pytest.raises(ValueError):
            make_breaker_key("   ", "noaa")

    def test_rejects_blank_provider(self) -> None:
        with pytest.raises(ValueError):
            make_breaker_key("tenant-1", "")


# ---------------------------------------------------------------------------
# CircuitBreaker state machine
# ---------------------------------------------------------------------------


class TestCircuitBreakerConstruction:
    def test_defaults_match_requirement_10_4_3(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.failure_threshold == 5 == DEFAULT_FAILURE_THRESHOLD
        assert breaker.reset_timeout_seconds == 60.0 == DEFAULT_RESET_TIMEOUT_SECONDS

    def test_rejects_zero_threshold(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0)

    def test_rejects_zero_reset_timeout(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(reset_timeout_seconds=0)


class TestCircuitBreakerStateTransitions:
    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.state_for("any") == CircuitBreakerState.CLOSED
        assert await breaker.is_open("any") is False

    @pytest.mark.asyncio
    async def test_five_failures_open_breaker(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(5):
            await breaker.record_failure(key)
        assert breaker.state_for(key) == CircuitBreakerState.OPEN
        assert await breaker.is_open(key) is True

    @pytest.mark.asyncio
    async def test_four_failures_do_not_open_breaker(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(4):
            await breaker.record_failure(key)
        assert breaker.state_for(key) == CircuitBreakerState.CLOSED
        assert await breaker.is_open(key) is False

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(4):
            await breaker.record_failure(key)
        await breaker.record_success(key)
        # Four more failures after reset should not trip.
        for _ in range(4):
            await breaker.record_failure(key)
        assert breaker.state_for(key) == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_after_60_seconds(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=5, reset_timeout_seconds=60, clock=clock
        )
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(5):
            await breaker.record_failure(key)
        assert breaker.state_for(key) == CircuitBreakerState.OPEN

        # 59 seconds is still open.
        clock.advance(59)
        assert await breaker.is_open(key) is True
        assert breaker.state_for(key) == CircuitBreakerState.OPEN

        # Cross the 60s boundary and the next is_open() check flips to
        # half-open (a probe is permitted).
        clock.advance(2)
        assert await breaker.is_open(key) is False
        assert breaker.state_for(key) == CircuitBreakerState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=5, reset_timeout_seconds=60, clock=clock
        )
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(5):
            await breaker.record_failure(key)
        clock.advance(60)
        await breaker.is_open(key)  # promote OPEN → HALF_OPEN
        assert breaker.state_for(key) == CircuitBreakerState.HALF_OPEN

        await breaker.record_success(key)
        assert breaker.state_for(key) == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_snaps_back_to_open(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=5, reset_timeout_seconds=60, clock=clock
        )
        key = make_breaker_key("tenant-1", "noaa")
        for _ in range(5):
            await breaker.record_failure(key)
        clock.advance(60)
        await breaker.is_open(key)  # OPEN → HALF_OPEN
        await breaker.record_failure(key)
        assert breaker.state_for(key) == CircuitBreakerState.OPEN


# ---------------------------------------------------------------------------
# trace_external_call context manager
# ---------------------------------------------------------------------------


class TestTraceExternalCallSuccess:
    @pytest.mark.asyncio
    async def test_emits_success_log_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="services.external_call_tracing")
        metric = _RecordingMetric()

        async with trace_external_call(
            tenant_id="tenant-1",
            provider="noaa",
            operation="get_hdd",
            metric=metric,
        ) as ctx:
            ctx.add_extra(zip_code="02108")

        # Structured-log event set: started + finished.
        records = [
            r for r in caplog.records if r.name == "services.external_call_tracing"
        ]
        events = {getattr(r, "event", None) for r in records}
        assert "external_call_started" in events
        assert "external_call_finished" in events

        finished = [r for r in records if getattr(r, "event", None) == "external_call_finished"][0]
        assert finished.tenant_id == "tenant-1"
        assert finished.provider == "noaa"
        assert finished.operation == "get_hdd"
        assert finished.status == "success"
        assert isinstance(finished.duration_ms, int)
        assert finished.duration_ms >= 0
        # Extra fields are passed through.
        assert finished.zip_code == "02108"

        # Metric was incremented with the canonical label triple.
        assert metric.calls == [
            {
                "tenant_id": "tenant-1",
                "provider": "noaa",
                "status": "success",
                "amount": 1.0,
            }
        ]


class TestTraceExternalCallFailure:
    @pytest.mark.asyncio
    async def test_five_consecutive_failures_open_breaker(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        metric = _RecordingMetric()

        async def _failing_call() -> None:
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="mapbox",
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                raise RuntimeError("boom")

        # Five failures should trip the breaker.
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await _failing_call()

        key = make_breaker_key("tenant-1", "mapbox")
        assert await breaker.is_open(key) is True

    @pytest.mark.asyncio
    async def test_call_during_open_state_raises_circuit_open_error(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        metric = _RecordingMetric()
        invocations = {"count": 0}

        async def _failing_call() -> None:
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="mapbox",
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                invocations["count"] += 1
                raise RuntimeError("boom")

        for _ in range(5):
            with pytest.raises(RuntimeError):
                await _failing_call()

        # Fresh call — the body MUST NOT be entered.
        async def _probe() -> None:
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="mapbox",
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                invocations["count"] += 1

        with pytest.raises(CircuitOpenError) as exc_info:
            await _probe()

        # The wrapped function was NOT invoked during the open state.
        assert invocations["count"] == 5
        # A retry-after hint is attached.
        assert exc_info.value.retry_after_seconds is not None

        # Metric increment for the rejected call uses ``status="circuit_open"``.
        assert any(
            call["status"] == "circuit_open" for call in metric.calls
        )

    @pytest.mark.asyncio
    async def test_timeout_classified_with_error_code(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="services.external_call_tracing")
        metric = _RecordingMetric()

        with pytest.raises(asyncio.TimeoutError):
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="textract",
                operation="analyze_document",
                metric=metric,
            ):
                raise asyncio.TimeoutError()

        records = [
            r for r in caplog.records
            if getattr(r, "event", None) == "external_call_failed"
        ]
        assert records, "expected an external_call_failed structured log event"
        rec = records[0]
        assert rec.status == "timeout"
        assert rec.error_code == "timeout"
        assert isinstance(rec.duration_ms, int)

        # Metric increment uses ``status="timeout"``.
        assert metric.calls[-1]["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_generic_exception_maps_to_snake_case_error_code(self) -> None:
        metric = _RecordingMetric()

        class HTTPError(Exception):
            pass

        with pytest.raises(HTTPError):
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="opis",
                operation="fetch_prices",
                metric=metric,
            ):
                raise HTTPError("upstream 500")

        # Snake_case conversion keeps dashboards readable.
        assert metric.calls[-1]["status"] == "error"


class TestTraceExternalCallRecovery:
    @pytest.mark.asyncio
    async def test_after_60s_half_open_success_closes_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=5, reset_timeout_seconds=60, clock=clock
        )
        metric = _RecordingMetric()
        key = make_breaker_key("tenant-1", "mapbox")

        async def _failing_call() -> None:
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="mapbox",
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                raise RuntimeError("boom")

        for _ in range(5):
            with pytest.raises(RuntimeError):
                await _failing_call()

        assert await breaker.is_open(key) is True

        # Advance past the 60-second reset window.
        clock.advance(61)

        # Next call sees HALF_OPEN and is allowed through; it succeeds
        # and should close the breaker.
        async with trace_external_call(
            tenant_id="tenant-1",
            provider="mapbox",
            operation="get_matrix",
            circuit_breaker=breaker,
            metric=metric,
        ):
            pass  # success

        assert breaker.state_for(key) == CircuitBreakerState.CLOSED


class TestTraceExternalCallValidation:
    @pytest.mark.asyncio
    async def test_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError):
            async with trace_external_call(
                tenant_id="",
                provider="noaa",
                operation="get_hdd",
            ):
                pass

    @pytest.mark.asyncio
    async def test_rejects_blank_provider(self) -> None:
        with pytest.raises(ValueError):
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="",
                operation="get_hdd",
            ):
                pass

    @pytest.mark.asyncio
    async def test_rejects_blank_operation(self) -> None:
        with pytest.raises(ValueError):
            async with trace_external_call(
                tenant_id="tenant-1",
                provider="noaa",
                operation="",
            ):
                pass


class TestCircuitBreakerIsolation:
    @pytest.mark.asyncio
    async def test_per_tenant_isolation(self) -> None:
        """Tenant A's outage must not trip tenant B's breaker."""
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        metric = _RecordingMetric()

        async def _failing_call_for(tenant_id: str) -> None:
            async with trace_external_call(
                tenant_id=tenant_id,
                provider="mapbox",
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                raise RuntimeError("boom")

        for _ in range(5):
            with pytest.raises(RuntimeError):
                await _failing_call_for("tenant-A")

        key_a = make_breaker_key("tenant-A", "mapbox")
        key_b = make_breaker_key("tenant-B", "mapbox")
        assert await breaker.is_open(key_a) is True
        assert await breaker.is_open(key_b) is False

    @pytest.mark.asyncio
    async def test_per_provider_isolation(self) -> None:
        """One provider failing should not trip another provider."""
        breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=60)
        metric = _RecordingMetric()

        async def _failing_call_for(provider: str) -> None:
            async with trace_external_call(
                tenant_id="tenant-1",
                provider=provider,
                operation="get_matrix",
                circuit_breaker=breaker,
                metric=metric,
            ):
                raise RuntimeError("boom")

        for _ in range(5):
            with pytest.raises(RuntimeError):
                await _failing_call_for("mapbox")

        assert await breaker.is_open(make_breaker_key("tenant-1", "mapbox")) is True
        assert await breaker.is_open(make_breaker_key("tenant-1", "here")) is False
