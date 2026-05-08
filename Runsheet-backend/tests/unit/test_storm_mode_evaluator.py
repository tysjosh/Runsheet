"""
Unit tests for :mod:`fuel.services.storm_mode_evaluator`.

Covers Task 10.3 of the fuel-ops hardening spec (Requirements 9.1.3,
9.1.4, 9.1.5):

* Default 5-minute poll interval, ``severe`` activation threshold, 48-hour
  lookahead window.
* Alert qualification — severity threshold + 48h window + tenant match +
  activation_status gating.
* State transitions ``inactive → active`` publish ``storm_mode_activated``;
  ``active → inactive`` publish ``storm_mode_cleared`` (Req 9.1.4, 9.1.5).
* Idempotence — re-running the evaluator with the same alerts does not
  re-publish a signal (Req 9.4.5 prep).
* Override precedence — ``activate`` forces active, ``deactivate`` / ``snooze``
  force inactive, ``clear`` falls back to computed state; expired overrides
  are ignored.
* Redis persistence — state is round-tripped through the Redis client; the
  in-memory fallback is used when no Redis client is wired.
* Graceful degradation — ES errors / malformed docs / broken SignalBus /
  broken Redis never raise out of the evaluator.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from fuel.services.fuel_ops_es_mappings import (
    STORM_MODE_OVERRIDES_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.services.storm_mode_evaluator import (
    ACTIVE,
    DEFAULT_ACTIVATION_SEVERITY,
    DEFAULT_ACTIVATION_WINDOW_HOURS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    INACTIVE,
    PersistedState,
    SIGNAL_TYPE_ACTIVATED,
    SIGNAL_TYPE_CLEARED,
    STATE_KEY_PATTERN,
    STORM_MODE_ENTITY_TYPE,
    STORM_MODE_EVALUATOR_AGENT_ID,
    StormModeEvaluator,
    severity_meets_threshold,
)
from fuel.storm_mode_models import StormModeOverride, WeatherAlert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _alert_payload(
    *,
    alert_id: str = "alert-001",
    tenant_id: str = "tenant-A",
    severity: str = "severe",
    start_offset_hours: float = 1.0,
    end_offset_hours: Optional[float] = 6.0,
    status: str = "forecast",
    affected_zip_codes: Optional[List[str]] = None,
    alert_type: str = "winter_storm_warning",
    region_code: str = "NY",
) -> Dict[str, Any]:
    """Build a WeatherAlert-shaped ES ``_source`` for tests."""
    start = _now() + timedelta(hours=start_offset_hours)
    payload: Dict[str, Any] = {
        "alert_id": alert_id,
        "tenant_id": tenant_id,
        "region_code": region_code,
        "alert_type": alert_type,
        "severity": severity,
        "expected_start_at": start.isoformat(),
        "affected_zip_codes": affected_zip_codes or ["14202"],
        "source": "nws",
        "ingested_at": _now().isoformat(),
        "activation_status": status,
    }
    if end_offset_hours is not None:
        end = _now() + timedelta(hours=end_offset_hours)
        payload["expected_end_at"] = end.isoformat()
    return payload


def _override_payload(
    *,
    override_id: str = "ov-001",
    tenant_id: str = "tenant-A",
    action: str = "activate",
    reason: str = "manual op decision",
    actor_id: str = "dispatcher-1",
    expires_offset_hours: Optional[float] = 4.0,
    created_offset_hours: float = -0.1,
) -> Dict[str, Any]:
    created = _now() + timedelta(hours=created_offset_hours)
    payload: Dict[str, Any] = {
        "override_id": override_id,
        "tenant_id": tenant_id,
        "action": action,
        "reason": reason,
        "actor_id": actor_id,
        "created_at": created.isoformat(),
        "updated_at": created.isoformat(),
    }
    if expires_offset_hours is not None:
        payload["expires_at"] = (
            _now() + timedelta(hours=expires_offset_hours)
        ).isoformat()
    return payload


def _hits(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "hits": {
            "hits": [{"_source": s} for s in sources],
            "total": {"value": len(sources)},
        }
    }


class _FakeES:
    """Lightweight ES stub that serves per-index canned responses.

    Each value is either a fixed dict (returned on every call) or a
    callable that receives ``(query, size)`` and returns the response —
    enough flexibility to simulate the override / alert split without
    pulling in a full mock.
    """

    def __init__(
        self,
        responses: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._responses: Dict[str, Any] = responses or {}
        self.calls: List[Dict[str, Any]] = []

    def set(self, index: str, response: Any) -> None:
        self._responses[index] = response

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.calls.append({"index": index, "query": query, "size": size})
        handler = self._responses.get(index)
        if handler is None:
            return _hits([])
        if callable(handler):
            result = handler(query, size)
        else:
            result = handler
        if isinstance(result, Exception):
            raise result
        return result


class _FakeRedis:
    """Minimal async Redis stub supporting ``get`` / ``set``."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.get_error: Optional[Exception] = None
        self.set_error: Optional[Exception] = None

    async def get(self, key: str) -> Optional[bytes]:
        if self.get_error is not None:
            raise self.get_error
        raw = self.store.get(key)
        if raw is None:
            return None
        return raw.encode("utf-8")

    async def set(self, key: str, value: str) -> None:
        if self.set_error is not None:
            raise self.set_error
        self.store[key] = value


class _RecordingSignalBus:
    def __init__(self) -> None:
        self.published: List[Any] = []
        self.error: Optional[Exception] = None

    async def publish(self, message: Any) -> int:
        if self.error is not None:
            raise self.error
        self.published.append(message)
        return 1


def _build_evaluator(
    *,
    es_service: Optional[Any] = None,
    signal_bus: Optional[Any] = None,
    redis_client: Optional[Any] = None,
    tenant_discovery=None,
    severity_threshold_loader=None,
    activation_window_hours: int = DEFAULT_ACTIVATION_WINDOW_HOURS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> StormModeEvaluator:
    return StormModeEvaluator(
        es_service=es_service or _FakeES(),
        signal_bus=signal_bus,
        redis_client=redis_client,
        tenant_discovery=tenant_discovery,
        severity_threshold_loader=severity_threshold_loader,
        activation_window_hours=activation_window_hours,
        poll_interval_seconds=poll_interval_seconds,
    )


# ---------------------------------------------------------------------------
# Constructor + constants
# ---------------------------------------------------------------------------


class TestConstructor:
    """Requirement 9.1.3 — default 5-minute poll and `severe` threshold."""

    def test_default_poll_interval_is_five_minutes(self):
        evaluator = _build_evaluator()
        assert evaluator._poll_interval == DEFAULT_POLL_INTERVAL_SECONDS == 300

    def test_default_activation_severity_is_severe(self):
        assert DEFAULT_ACTIVATION_SEVERITY == "severe"

    def test_default_window_is_48_hours(self):
        assert DEFAULT_ACTIVATION_WINDOW_HOURS == 48

    def test_es_service_is_required(self):
        with pytest.raises(ValueError):
            StormModeEvaluator(es_service=None)

    def test_invalid_window_rejected(self):
        with pytest.raises(ValueError):
            _build_evaluator(activation_window_hours=0)

    def test_invalid_poll_interval_rejected(self):
        with pytest.raises(ValueError):
            _build_evaluator(poll_interval_seconds=0)


# ---------------------------------------------------------------------------
# Severity threshold helper
# ---------------------------------------------------------------------------


class TestSeverityMeetsThreshold:
    """Requirement 9.1.3 — numeric severity rank, not string ordering."""

    def test_severe_meets_severe(self):
        assert severity_meets_threshold("severe", "severe") is True

    def test_extreme_meets_severe(self):
        assert severity_meets_threshold("extreme", "severe") is True

    def test_moderate_does_not_meet_severe(self):
        assert severity_meets_threshold("moderate", "severe") is False

    def test_minor_does_not_meet_severe(self):
        assert severity_meets_threshold("minor", "severe") is False

    def test_case_insensitive(self):
        assert severity_meets_threshold("SEVERE", "Severe") is True

    def test_unknown_severity_is_minor(self):
        assert severity_meets_threshold("?", "severe") is False

    def test_unknown_threshold_defaults_to_severe(self):
        assert severity_meets_threshold("severe", "wat") is True
        assert severity_meets_threshold("moderate", "wat") is False


# ---------------------------------------------------------------------------
# Alert qualification
# ---------------------------------------------------------------------------


class TestAlertQualification:
    """Requirement 9.1.3 — alerts filtered by severity, window, and tenant."""

    @pytest.mark.asyncio
    async def test_severe_alert_in_window_qualifies(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [_alert_payload(start_offset_hours=1.0, severity="severe")]
                )
            }
        )
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert len(alerts) == 1
        assert alerts[0].alert_id == "alert-001"

    @pytest.mark.asyncio
    async def test_moderate_alert_does_not_qualify(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [_alert_payload(severity="moderate")]
                )
            }
        )
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert alerts == []

    @pytest.mark.asyncio
    async def test_alert_beyond_48h_window_ignored_client_side(self):
        """Alerts returned by ES but outside the 48h window are filtered out."""
        # The ES query does range filter, but we also let the evaluator
        # ignore an alert whose expected_end_at has already passed. A stale
        # alert with expired end time should be dropped.
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [
                        _alert_payload(
                            alert_id="stale",
                            start_offset_hours=-10.0,
                            end_offset_hours=-1.0,  # already ended
                            status="active",
                        )
                    ]
                )
            }
        )
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert alerts == []

    @pytest.mark.asyncio
    async def test_foreign_tenant_alerts_rejected_defense_in_depth(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [
                        _alert_payload(
                            alert_id="foreign",
                            tenant_id="tenant-B",  # cross-tenant smuggler
                        )
                    ]
                )
            }
        )
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert alerts == []

    @pytest.mark.asyncio
    async def test_es_error_returns_empty(self):
        es = _FakeES({WEATHER_ALERTS_INDEX: RuntimeError("es down")})
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert alerts == []

    @pytest.mark.asyncio
    async def test_malformed_alert_sources_skipped(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [
                        {"not": "valid"},  # malformed
                        _alert_payload(alert_id="good"),
                    ]
                )
            }
        )
        evaluator = _build_evaluator(es_service=es)

        alerts = await evaluator._fetch_qualifying_alerts(
            "tenant-A",
            severity_threshold="severe",
            now=_now(),
        )

        assert [a.alert_id for a in alerts] == ["good"]


# ---------------------------------------------------------------------------
# Override precedence
# ---------------------------------------------------------------------------


class TestOverridePrecedence:
    """Design-doc requirement — manual overrides supersede computed state."""

    @pytest.mark.asyncio
    async def test_activate_override_forces_active_without_alerts(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [_override_payload(action="activate")]
                ),
            }
        )
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == ACTIVE
        assert result.computed_state == INACTIVE
        assert result.override is not None
        assert result.override.action == "activate"
        assert len(bus.published) == 1
        assert bus.published[0].context["signal_type"] == SIGNAL_TYPE_ACTIVATED

    @pytest.mark.asyncio
    async def test_deactivate_override_forces_inactive_despite_alerts(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="extreme")]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [_override_payload(action="deactivate")]
                ),
            }
        )
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == INACTIVE
        assert result.computed_state == ACTIVE
        # No transition because the persisted start state is inactive;
        # the override keeps us at inactive.
        assert result.transitioned is False
        assert bus.published == []

    @pytest.mark.asyncio
    async def test_snooze_override_forces_inactive(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [_override_payload(action="snooze")]
                ),
            }
        )
        evaluator = _build_evaluator(es_service=es)

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == INACTIVE

    @pytest.mark.asyncio
    async def test_clear_override_falls_back_to_computed(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [_override_payload(action="clear", expires_offset_hours=1.0)]
                ),
            }
        )
        evaluator = _build_evaluator(es_service=es)

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == ACTIVE
        assert result.computed_state == ACTIVE

    @pytest.mark.asyncio
    async def test_expired_override_ignored(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [
                        _override_payload(
                            action="activate", expires_offset_hours=-1.0
                        )
                    ]
                ),
            }
        )
        evaluator = _build_evaluator(es_service=es)

        result = await evaluator.evaluate_tenant("tenant-A")

        # Expired override should not force activation.
        assert result.desired_state == INACTIVE
        assert result.override is None


# ---------------------------------------------------------------------------
# Transitions + signal publication
# ---------------------------------------------------------------------------


class TestTransitions:
    """Req 9.1.4 (activation signal) and 9.1.5 (clearance signal)."""

    @pytest.mark.asyncio
    async def test_inactive_to_active_publishes_activation_signal(self):
        alert = _alert_payload(severity="severe")
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.transitioned is True
        assert result.previous_state == INACTIVE
        assert result.desired_state == ACTIVE
        assert len(bus.published) == 1
        signal = bus.published[0]
        assert isinstance(signal, RiskSignal)
        assert signal.source_agent == STORM_MODE_EVALUATOR_AGENT_ID
        assert signal.entity_id == "tenant-A"
        assert signal.entity_type == STORM_MODE_ENTITY_TYPE
        assert signal.severity == Severity.HIGH
        assert signal.tenant_id == "tenant-A"
        assert signal.context["signal_type"] == SIGNAL_TYPE_ACTIVATED
        assert signal.context["next_state"] == ACTIVE
        assert signal.context["previous_state"] == INACTIVE
        assert signal.context["triggering_alert_ids"] == ["alert-001"]
        assert signal.context["triggering_alert"]["alert_id"] == "alert-001"
        assert signal.context["triggering_alert"]["severity"] == "severe"

    @pytest.mark.asyncio
    async def test_active_to_inactive_publishes_clearance_signal(self):
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([])})
        bus = _RecordingSignalBus()
        redis = _FakeRedis()
        # Seed an "active" persisted state as if the previous tick activated.
        redis.store[STATE_KEY_PATTERN.format(tenant_id="tenant-A")] = (
            PersistedState(
                state=ACTIVE,
                updated_at=_now(),
                triggering_alert_ids=["alert-001"],
                expected_end_at=_now() + timedelta(hours=4),
            ).to_json()
        )
        evaluator = _build_evaluator(
            es_service=es, signal_bus=bus, redis_client=redis
        )

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.transitioned is True
        assert result.previous_state == ACTIVE
        assert result.desired_state == INACTIVE
        assert len(bus.published) == 1
        signal = bus.published[0]
        assert signal.severity == Severity.MEDIUM
        assert signal.context["signal_type"] == SIGNAL_TYPE_CLEARED
        assert signal.context["next_state"] == INACTIVE
        # Redis was updated to reflect the clearance.
        persisted = json.loads(
            redis.store[STATE_KEY_PATTERN.format(tenant_id="tenant-A")]
        )
        assert persisted["state"] == INACTIVE

    @pytest.mark.asyncio
    async def test_idempotent_when_state_unchanged(self):
        """Req 9.4.5 prep — re-running with same alerts is a no-op."""
        alert = _alert_payload(severity="severe")
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        first = await evaluator.evaluate_tenant("tenant-A")
        second = await evaluator.evaluate_tenant("tenant-A")

        assert first.transitioned is True
        assert second.transitioned is False
        assert len(bus.published) == 1  # no re-publish

    @pytest.mark.asyncio
    async def test_activation_signal_includes_expected_end_at(self):
        alert = _alert_payload(
            severity="severe", end_offset_hours=6.0
        )
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        await evaluator.evaluate_tenant("tenant-A")

        assert bus.published[0].context["expected_end_at"] is not None

    @pytest.mark.asyncio
    async def test_longest_expected_end_used_when_multiple_alerts(self):
        a = _alert_payload(alert_id="a", end_offset_hours=4.0)
        b = _alert_payload(alert_id="b", end_offset_hours=12.0)
        c = _alert_payload(alert_id="c", end_offset_hours=8.0)
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([a, b, c])})
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        await evaluator.evaluate_tenant("tenant-A")

        published_end = bus.published[0].context["expected_end_at"]
        expected_end = datetime.fromisoformat(
            published_end.replace("Z", "+00:00")
        )
        # Should be the longest of the three (b, 12h out).
        assert expected_end >= _now() + timedelta(hours=11, minutes=50)

    @pytest.mark.asyncio
    async def test_override_is_included_in_signal_context(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits([]),
                STORM_MODE_OVERRIDES_INDEX: _hits(
                    [_override_payload(action="activate")]
                ),
            }
        )
        bus = _RecordingSignalBus()
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        await evaluator.evaluate_tenant("tenant-A")

        ctx = bus.published[0].context
        assert ctx["override"]["action"] == "activate"
        assert ctx["override"]["actor_id"] == "dispatcher-1"


# ---------------------------------------------------------------------------
# Redis persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    @pytest.mark.asyncio
    async def test_state_round_trips_through_redis(self):
        alert = _alert_payload(severity="severe")
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        redis = _FakeRedis()
        evaluator = _build_evaluator(
            es_service=es,
            signal_bus=_RecordingSignalBus(),
            redis_client=redis,
        )

        await evaluator.evaluate_tenant("tenant-A")

        raw = redis.store.get(STATE_KEY_PATTERN.format(tenant_id="tenant-A"))
        assert raw is not None
        payload = json.loads(raw)
        assert payload["state"] == ACTIVE
        assert payload["triggering_alert_ids"] == ["alert-001"]

    @pytest.mark.asyncio
    async def test_in_memory_fallback_when_no_redis(self):
        alert = _alert_payload(severity="severe")
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        evaluator = _build_evaluator(
            es_service=es, signal_bus=_RecordingSignalBus()
        )

        first = await evaluator.evaluate_tenant("tenant-A")
        second = await evaluator.evaluate_tenant("tenant-A")

        assert first.transitioned is True
        assert second.transitioned is False  # stored in-process

    @pytest.mark.asyncio
    async def test_broken_redis_does_not_raise(self):
        alert = _alert_payload(severity="severe")
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([alert])})
        redis = _FakeRedis()
        redis.set_error = RuntimeError("redis down")
        evaluator = _build_evaluator(
            es_service=es,
            signal_bus=_RecordingSignalBus(),
            redis_client=redis,
        )

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == ACTIVE
        # Graceful — no exception raised.

    @pytest.mark.asyncio
    async def test_malformed_state_falls_back_to_inactive(self):
        redis = _FakeRedis()
        redis.store[STATE_KEY_PATTERN.format(tenant_id="tenant-A")] = "not json"
        es = _FakeES({WEATHER_ALERTS_INDEX: _hits([])})
        evaluator = _build_evaluator(es_service=es, redis_client=redis)

        state = await evaluator.get_state("tenant-A")

        assert state.state == INACTIVE

    @pytest.mark.asyncio
    async def test_get_state_returns_inactive_when_unknown(self):
        evaluator = _build_evaluator()
        state = await evaluator.get_state("tenant-unknown")
        assert state.state == INACTIVE
        assert state.triggering_alert_ids == []


# ---------------------------------------------------------------------------
# Tenant discovery
# ---------------------------------------------------------------------------


class TestTenantDiscovery:
    @pytest.mark.asyncio
    async def test_tick_processes_all_discovered_tenants(self):
        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: _hits(
                    [_alert_payload(severity="severe", tenant_id="tenant-A")]
                ),
            }
        )
        bus = _RecordingSignalBus()

        async def _discover():
            return ["tenant-A", "tenant-B", ""]

        evaluator = _build_evaluator(
            es_service=es, signal_bus=bus, tenant_discovery=_discover
        )

        results = await evaluator.tick()

        tenant_ids = {r.tenant_id for r in results}
        assert tenant_ids == {"tenant-A", "tenant-B"}

    @pytest.mark.asyncio
    async def test_discovery_failure_does_not_raise(self):
        async def _discover():
            raise RuntimeError("discovery down")

        evaluator = _build_evaluator(tenant_discovery=_discover)
        results = await evaluator.tick()
        assert results == []

    @pytest.mark.asyncio
    async def test_default_discovery_aggregates_alerts_and_overrides(self):
        def alerts_agg(query, size):
            return {
                "aggregations": {
                    "tenants": {
                        "buckets": [
                            {"key": "tenant-A", "doc_count": 3},
                            {"key": "tenant-B", "doc_count": 1},
                        ]
                    }
                }
            }

        def overrides_agg(query, size):
            return {
                "aggregations": {
                    "tenants": {
                        "buckets": [
                            {"key": "tenant-B", "doc_count": 1},
                            {"key": "tenant-C", "doc_count": 2},
                        ]
                    }
                }
            }

        es = _FakeES(
            {
                WEATHER_ALERTS_INDEX: alerts_agg,
                STORM_MODE_OVERRIDES_INDEX: overrides_agg,
            }
        )
        evaluator = _build_evaluator(es_service=es)

        tenants = await evaluator._default_tenant_discovery()

        assert tenants == ["tenant-A", "tenant-B", "tenant-C"]


# ---------------------------------------------------------------------------
# Severity threshold loader
# ---------------------------------------------------------------------------


class TestSeverityLoader:
    @pytest.mark.asyncio
    async def test_tenant_configured_threshold_is_honoured(self):
        async def _load(tenant_id: str) -> str:
            assert tenant_id == "tenant-A"
            return "extreme"

        es = _FakeES(
            {WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")])}
        )
        evaluator = _build_evaluator(
            es_service=es, severity_threshold_loader=_load
        )

        result = await evaluator.evaluate_tenant("tenant-A")

        # Threshold is extreme, alert is severe → does not qualify.
        assert result.desired_state == INACTIVE

    @pytest.mark.asyncio
    async def test_loader_failure_falls_back_to_severe(self):
        async def _load(tenant_id: str) -> str:
            raise RuntimeError("threshold service down")

        es = _FakeES(
            {WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")])}
        )
        evaluator = _build_evaluator(
            es_service=es,
            signal_bus=_RecordingSignalBus(),
            severity_threshold_loader=_load,
        )

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == ACTIVE

    @pytest.mark.asyncio
    async def test_unknown_threshold_falls_back_to_severe(self):
        async def _load(tenant_id: str) -> str:
            return "wat"

        es = _FakeES(
            {WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")])}
        )
        evaluator = _build_evaluator(
            es_service=es,
            signal_bus=_RecordingSignalBus(),
            severity_threshold_loader=_load,
        )

        result = await evaluator.evaluate_tenant("tenant-A")

        assert result.desired_state == ACTIVE


# ---------------------------------------------------------------------------
# Signal-bus failures + no-bus operation
# ---------------------------------------------------------------------------


class TestSignalBusResilience:
    @pytest.mark.asyncio
    async def test_missing_signal_bus_is_ok(self):
        es = _FakeES(
            {WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")])}
        )
        evaluator = _build_evaluator(es_service=es, signal_bus=None)

        # Must not raise even though a transition occurred.
        result = await evaluator.evaluate_tenant("tenant-A")
        assert result.transitioned is True

    @pytest.mark.asyncio
    async def test_bus_publish_error_does_not_raise(self):
        es = _FakeES(
            {WEATHER_ALERTS_INDEX: _hits([_alert_payload(severity="severe")])}
        )
        bus = _RecordingSignalBus()
        bus.error = RuntimeError("bus down")
        evaluator = _build_evaluator(es_service=es, signal_bus=bus)

        result = await evaluator.evaluate_tenant("tenant-A")
        assert result.transitioned is True


# ---------------------------------------------------------------------------
# No-op tenant id
# ---------------------------------------------------------------------------


class TestBlankTenant:
    @pytest.mark.asyncio
    async def test_blank_tenant_id_returns_inactive_noop(self):
        evaluator = _build_evaluator()
        result = await evaluator.evaluate_tenant("")
        assert result.desired_state == INACTIVE
        assert result.transitioned is False
