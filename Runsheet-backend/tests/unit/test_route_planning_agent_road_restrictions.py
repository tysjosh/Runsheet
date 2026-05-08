"""
Unit tests for Route_Planning_Agent road-restriction filter
(Task 10.8 — Req 9.3.3, 9.3.4, 9.3.5).

When Storm_Mode is active for the tenant, the agent runs an ES
``geo_shape`` intersects query per route segment against the
``storm_road_restrictions`` index and defers any stop whose inbound
leg crosses a restriction with severity ``>= severe``, tagging the
deferred entry with reason ``road_restriction`` and cause
``road_segment_restricted``.

The tests stub the Elasticsearch search path so the intersects filter
can be exercised without a live cluster — the stub is invoked once per
segment and returns a configurable set of matches.

Validates: Requirements 9.3.3, 9.3.4, 9.3.5.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.route_planning_agent import (
    CAUSE_OUTSIDE_WINDOW,
    CAUSE_OVER_MAX_STOPS,
    CAUSE_ROAD_RESTRICTION,
    REASON_DEFERRED_STORM_MODE,
    REASON_ROAD_RESTRICTION,
    ROAD_RESTRICTION_BLOCKING_SEVERITIES,
    RoutePlanningAgent,
    StormModeRouteSettings,
)
from Agents.support.fuel_distribution_models import RoutePlan, RouteStop
from fuel.services.truck_start_position import TruckStartPosition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(*, es_search_handler=None):
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    if es_search_handler is None:
        es_service.search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
    else:
        es_service.search_documents = AsyncMock(side_effect=es_search_handler)
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    autonomy_config = MagicMock()
    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)
    feature_flags.get_overlay_state = AsyncMock(return_value="disabled")

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(*, es_search_handler=None, **overrides):
    deps = _make_deps(es_search_handler=es_search_handler)
    deps.update(overrides)
    return RoutePlanningAgent(**deps), deps


class _FakeStormModeEvaluator:
    def __init__(self, state: str = "active"):
        self._state = state

    async def get_state(self, tenant_id: str):
        from fuel.services.storm_mode_evaluator import PersistedState

        return PersistedState(
            state=self._state,
            updated_at=None,
            triggering_alert_ids=[],
            expected_end_at=None,
        )


async def _wide_window_loader(tenant_id: str):
    """Loader that disables the window/cap so road-restriction deferrals
    are the sole source of deferred stops in these tests."""

    return StormModeRouteSettings(
        max_stops_per_truck=100,
        delivery_window_start_hour=0.0,
        delivery_window_end_hour=24.0,
    )


def _stop(station_id: str, sequence: int) -> RouteStop:
    eta = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc) + timedelta(
        minutes=30 * sequence
    )
    return RouteStop(
        station_id=station_id,
        eta=eta.isoformat(),
        drop={"AGO": 1000.0},
        sequence=sequence,
    )


def _make_route_plan(*, station_ids: List[str]) -> RoutePlan:
    stops = [_stop(sid, i) for i, sid in enumerate(station_ids)]
    return RoutePlan(
        truck_id="truck-1",
        plan_id="plan-1",
        stops=stops,
        distance_km=100.0,
        eta_confidence=0.8,
        tenant_id="tenant-1",
    )


def _station_locations(station_ids: List[str]) -> Dict[str, Dict[str, float]]:
    """Return a map of station_id → {lat, lon} on a spaced-out grid."""
    return {
        sid: {"lat": 40.7 + 0.01 * i, "lon": -74.0 + 0.01 * i}
        for i, sid in enumerate(station_ids)
    }


def _start_position() -> TruckStartPosition:
    return TruckStartPosition(lat=40.69, lon=-74.01, source="depot")


def _make_restriction_hit(
    *,
    restriction_id: str,
    tenant_id: str = "tenant-1",
    severity: str = "severe",
) -> Dict[str, Any]:
    return {
        "_source": {
            "restriction_id": restriction_id,
            "tenant_id": tenant_id,
            "severity": severity,
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoadRestrictionFilterDirect:
    @pytest.mark.asyncio
    async def test_no_restrictions_keeps_all_stops(self):
        agent, deps = _make_agent()
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1", "s-2", "s-3"]
        route = _make_route_plan(station_ids=station_ids)

        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )

        assert route.storm_mode_active is True
        assert [s.station_id for s in route.stops] == station_ids
        assert route.deferred_stops == []

    @pytest.mark.asyncio
    async def test_severe_restriction_defers_only_matched_stop(self):
        """Req 9.3.4 — stops whose inbound leg crosses a severe
        restriction are deferred with reason ``road_restriction``."""

        call_counter = {"idx": 0}
        match_index = 1  # The second leg (start → s-2) intersects.

        async def _handler(index: str, query: Dict[str, Any], size: int):
            current = call_counter["idx"]
            call_counter["idx"] += 1
            if current == match_index:
                return {
                    "hits": {
                        "hits": [
                            _make_restriction_hit(restriction_id="srr_abc")
                        ]
                    }
                }
            return {"hits": {"hits": []}}

        agent, deps = _make_agent(es_search_handler=_handler)
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1", "s-2", "s-3"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )

        # s-2 was deferred; s-1 and s-3 survived.
        assert [s.station_id for s in route.stops] == ["s-1", "s-3"]
        assert len(route.deferred_stops) == 1
        deferred = route.deferred_stops[0]
        assert deferred.station_id == "s-2"
        assert deferred.reason == REASON_ROAD_RESTRICTION
        assert deferred.deferral_cause == CAUSE_ROAD_RESTRICTION
        # Sequence is renumbered densely starting at 0.
        assert [s.sequence for s in route.stops] == [0, 1]

    @pytest.mark.asyncio
    async def test_geo_shape_filter_severity_constraint(self):
        """Req 9.3.4 — the ES filter pins severity to the blocking set."""

        captured_queries: List[Dict[str, Any]] = []

        async def _handler(index: str, query: Dict[str, Any], size: int):
            captured_queries.append(query)
            return {"hits": {"hits": []}}

        agent, _ = _make_agent(es_search_handler=_handler)
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1", "s-2"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )

        assert captured_queries, "geo_shape query should run per segment"

        # Inspect the first captured query for the severity + geo_shape
        # filters.
        filters = captured_queries[0]["query"]["bool"]["filter"]
        severity_clause = next(
            f for f in filters if "terms" in f and "severity" in f["terms"]
        )
        assert set(severity_clause["terms"]["severity"]) == set(
            ROAD_RESTRICTION_BLOCKING_SEVERITIES
        )

        geo_clause = next(f for f in filters if "geo_shape" in f)
        shape = geo_clause["geo_shape"]["polygon"]["shape"]
        assert shape["type"] == "LineString"
        assert len(shape["coordinates"]) == 2
        # Coordinates are [lon, lat] per GeoJSON.
        lon_start, lat_start = shape["coordinates"][0]
        assert -180 <= lon_start <= 180
        assert -90 <= lat_start <= 90

        # Each clause pins the tenant.
        tenant_clause = next(
            f for f in filters if "term" in f and "tenant_id" in f["term"]
        )
        assert tenant_clause["term"]["tenant_id"] == "tenant-1"

    @pytest.mark.asyncio
    async def test_cross_tenant_row_is_dropped(self):
        """Defensive tenant re-check drops a row whose tenant_id does
        not match the caller even if the ES layer somehow returned it."""

        async def _handler(index: str, query: Dict[str, Any], size: int):
            return {
                "hits": {
                    "hits": [
                        _make_restriction_hit(
                            restriction_id="srr_foreign",
                            tenant_id="tenant-OTHER",
                        )
                    ]
                }
            }

        agent, _ = _make_agent(es_search_handler=_handler)
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )
        # Cross-tenant row was ignored — nothing deferred.
        assert route.deferred_stops == []
        assert [s.station_id for s in route.stops] == ["s-1"]

    @pytest.mark.asyncio
    async def test_es_failure_defaults_to_keeping_stops(self):
        """ES transport errors on the intersects query must not fail
        closed — the per-truck cap and window filters still enforce
        Storm_Mode safety."""

        async def _handler(index: str, query: Dict[str, Any], size: int):
            raise RuntimeError("boom")

        agent, _ = _make_agent(es_search_handler=_handler)
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1", "s-2"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )
        assert [s.station_id for s in route.stops] == station_ids
        assert route.deferred_stops == []

    @pytest.mark.asyncio
    async def test_missing_station_locations_is_noop(self):
        """Legacy callers that do not thread ``station_locations`` through
        receive the pre-Phase-10.8 behaviour (no road-restriction pass)."""

        agent, deps = _make_agent()
        agent.set_storm_mode_evaluator(_FakeStormModeEvaluator(state="active"))
        agent.set_storm_mode_settings_loader(_wide_window_loader)

        station_ids = ["s-1", "s-2"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            # station_locations=None by default
        )

        # No geo_shape queries were issued.
        deps["es_service"].search_documents.assert_not_called()
        assert [s.station_id for s in route.stops] == station_ids

    @pytest.mark.asyncio
    async def test_inactive_storm_mode_skips_restriction_query(self):
        agent, deps = _make_agent()
        agent.set_storm_mode_evaluator(
            _FakeStormModeEvaluator(state="inactive")
        )

        station_ids = ["s-1", "s-2"]
        route = _make_route_plan(station_ids=station_ids)
        await agent._maybe_apply_storm_mode(
            route_plan=route,
            tenant_id="tenant-1",
            truck_id="truck-1",
            station_locations=_station_locations(station_ids),
            start_position=_start_position(),
        )
        deps["es_service"].search_documents.assert_not_called()
        assert route.storm_mode_active is False
        assert route.deferred_stops == []
