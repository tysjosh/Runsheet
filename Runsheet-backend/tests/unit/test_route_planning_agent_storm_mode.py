"""
Unit tests for Route_Planning_Agent Storm_Mode guard-rails
(Task 10.7 — Req 9.2.4, 9.2.5, 9.3.1, 9.3.2).

When Storm_Mode is active for the tenant, the agent:

1. Caps per-truck stops at ``storm_mode_max_stops_per_truck`` (default 10)
   — surplus stops move to ``deferred_stops`` with cause
   ``over_max_stops_per_truck`` (Req 9.2.4).
2. Enforces the tenant-configured ``storm_mode_delivery_window`` — stops
   whose ETA falls outside the window move to ``deferred_stops`` with
   cause ``outside_delivery_window`` and carry a
   ``next_eligible_window_*`` pair (Req 9.3.1, 9.3.2).
3. Tags every deferred stop with the spec-mandated reason
   ``deferred_storm_mode`` (Req 9.3.2).
4. Routes the Route_Plan action through ConfirmationProtocol at
   ``RiskClass.HIGH`` using the ``apply_route_plan_storm_mode`` tool
   name (Req 9.2.5).

When Storm_Mode is inactive or no evaluator is wired, the agent
retains its pre-Phase-10 behaviour (``apply_route_plan`` + LOW risk).

Validates: Requirements 9.2.4, 9.2.5, 9.3.1, 9.3.2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    APPLY_ROUTE_PLAN_STORM_MODE_TOOL,
    APPLY_ROUTE_PLAN_TOOL,
    CAUSE_OUTSIDE_WINDOW,
    CAUSE_OVER_MAX_STOPS,
    DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR,
    DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR,
    DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK,
    REASON_DEFERRED_STORM_MODE,
    RoutePlanningAgent,
    StormModeRouteSettings,
)
from Agents.support.fuel_distribution_models import (
    DeferredRouteStop,
    RoutePlan,
    RouteStop,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the shape of test_route_planning_agent._make_* helpers
# ---------------------------------------------------------------------------


def _make_loading_proposal(
    *,
    truck_id: str = "truck-1",
    plan_id: str = "plan-1",
    tenant_id: str = "tenant-1",
    station_ids=None,
) -> InterventionProposal:
    if station_ids is None:
        station_ids = [f"station-{i}" for i in range(1, 4)]
    assignments = [
        {
            "compartment_id": f"comp-{i}",
            "station_id": sid,
            "fuel_grade": "AGO",
            "quantity_liters": 5000.0,
            "compartment_capacity_liters": 10000.0,
        }
        for i, sid in enumerate(station_ids)
    ]
    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "assignments": assignments,
                    "total_utilization_pct": 75.0,
                    "unserved_demand_liters": 0.0,
                    "total_weight_kg": 8500.0,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 75.0},
        risk_class=RiskClass.LOW,
        confidence=0.85,
        priority=1,
        tenant_id=tenant_id,
    )


def _make_station_location_hit(station_id: str, lat: float, lon: float) -> dict:
    return {
        "_source": {
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
        }
    }


def _make_deps():
    from Agents.overlay.signal_bus import SignalBus  # noqa: F401

    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
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


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return RoutePlanningAgent(**deps), deps


class _FakeStormModeEvaluator:
    """Minimal StormModeEvaluator stub mirroring
    :meth:`StormModeEvaluator.get_state`."""

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


async def _settings_loader_factory(settings: Optional[StormModeRouteSettings]):
    async def _loader(tenant_id: str):
        return settings

    return _loader


# ---------------------------------------------------------------------------
# Tests: _maybe_apply_storm_mode direct helper behaviour
# ---------------------------------------------------------------------------


class TestStormModeGatingDirect:
    """Unit tests exercising the guard-rail helper directly so failure
    modes are isolated from the full evaluate() path."""

    @pytest.mark.asyncio
    async def test_no_evaluator_is_noop(self):
        agent, _ = _make_agent()
        route = _make_route_plan(num_stops=15)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        assert route.storm_mode_active is False
        assert route.deferred_stops == []
        assert len(route.stops) == 15

    @pytest.mark.asyncio
    async def test_inactive_state_is_noop(self):
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="inactive"),
        )
        route = _make_route_plan(num_stops=15)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        assert route.storm_mode_active is False
        assert route.deferred_stops == []
        assert len(route.stops) == 15

    @pytest.mark.asyncio
    async def test_caps_stops_at_max_per_truck(self):
        """Req 9.2.4 — surplus stops move to deferred_stops with cause
        ``over_max_stops_per_truck``."""
        # Give every stop an ETA that sits inside the default window so
        # only the cap filter fires.
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                max_stops_per_truck=10,
                delivery_window_start_hour=0.0,
                delivery_window_end_hour=24.0,
            )
        )
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=loader,
        )
        route = _make_route_plan(num_stops=15)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        assert route.storm_mode_active is True
        assert route.storm_mode_max_stops_per_truck == 10
        assert len(route.stops) == 10
        assert len(route.deferred_stops) == 5
        for deferred in route.deferred_stops:
            assert deferred.reason == REASON_DEFERRED_STORM_MODE
            assert deferred.deferral_cause == CAUSE_OVER_MAX_STOPS

    @pytest.mark.asyncio
    async def test_default_max_stops_when_loader_absent(self):
        """Tenants without a settings loader use the spec-default cap of
        10 stops (Task 10.7)."""
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
        )
        # 24-hour window via a partial loader so only the default cap is
        # exercised. Install a loader that supplies only window overrides
        # but leaves max_stops at the default.
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                delivery_window_start_hour=0.0,
                delivery_window_end_hour=24.0,
            )
        )
        agent.set_storm_mode_settings_loader(loader)
        route = _make_route_plan(num_stops=DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK + 3)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        assert (
            route.storm_mode_max_stops_per_truck
            == DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK
        )
        assert len(route.stops) == DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK
        assert len(route.deferred_stops) == 3

    @pytest.mark.asyncio
    async def test_enforces_delivery_window(self):
        """Req 9.3.1, 9.3.2 — out-of-window stops are deferred with cause
        ``outside_delivery_window`` and tagged ``deferred_storm_mode``."""
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                max_stops_per_truck=100,
                delivery_window_start_hour=8.0,
                delivery_window_end_hour=16.0,
            )
        )
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=loader,
        )
        # Build three stops: one at 10:00 UTC (in window), one at 18:00
        # UTC (out), one at 04:00 UTC (out). Using UTC keeps the test
        # timezone-independent.
        route = RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            stops=[
                _stop("station-in", "2030-01-01T10:00:00+00:00", sequence=0),
                _stop("station-late", "2030-01-01T18:00:00+00:00", sequence=1),
                _stop("station-early", "2030-01-01T04:00:00+00:00", sequence=2),
            ],
            distance_km=100.0,
            eta_confidence=0.8,
            tenant_id="tenant-1",
        )
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        kept_ids = [s.station_id for s in route.stops]
        deferred_ids = [d.station_id for d in route.deferred_stops]
        assert kept_ids == ["station-in"]
        assert set(deferred_ids) == {"station-late", "station-early"}
        for deferred in route.deferred_stops:
            assert deferred.reason == REASON_DEFERRED_STORM_MODE
            assert deferred.deferral_cause == CAUSE_OUTSIDE_WINDOW
            assert deferred.next_eligible_window_start is not None
            assert deferred.next_eligible_window_end is not None

    @pytest.mark.asyncio
    async def test_window_filter_runs_before_cap(self):
        """With the cap at 5 and 3 window-eligible stops, nothing is
        cap-deferred even though more than 5 total stops were supplied."""
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                max_stops_per_truck=5,
                delivery_window_start_hour=8.0,
                delivery_window_end_hour=16.0,
            )
        )
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=loader,
        )
        route = RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            stops=[
                _stop("s-1", "2030-01-01T09:00:00+00:00", sequence=0),
                _stop("s-2", "2030-01-01T10:00:00+00:00", sequence=1),
                _stop("s-3", "2030-01-01T15:00:00+00:00", sequence=2),
                _stop("s-4", "2030-01-01T18:00:00+00:00", sequence=3),
                _stop("s-5", "2030-01-01T19:00:00+00:00", sequence=4),
                _stop("s-6", "2030-01-01T20:00:00+00:00", sequence=5),
                _stop("s-7", "2030-01-01T03:00:00+00:00", sequence=6),
            ],
            distance_km=100.0,
            eta_confidence=0.8,
            tenant_id="tenant-1",
        )
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        kept_ids = [s.station_id for s in route.stops]
        assert kept_ids == ["s-1", "s-2", "s-3"]
        causes = {d.deferral_cause for d in route.deferred_stops}
        assert causes == {CAUSE_OUTSIDE_WINDOW}

    @pytest.mark.asyncio
    async def test_stops_are_renumbered_after_filter(self):
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                max_stops_per_truck=2,
                delivery_window_start_hour=0.0,
                delivery_window_end_hour=24.0,
            )
        )
        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=loader,
        )
        route = _make_route_plan(num_stops=5)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        assert [s.sequence for s in route.stops] == [0, 1]

    @pytest.mark.asyncio
    async def test_loader_failure_falls_back_to_defaults(self):
        async def _broken_loader(tenant_id: str):
            raise RuntimeError("redis is down")

        agent, _ = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=_broken_loader,
        )
        route = _make_route_plan(num_stops=15)
        await agent._maybe_apply_storm_mode(
            route_plan=route, tenant_id="tenant-1", truck_id="truck-1"
        )
        # Even with a broken loader the guard-rails still apply via the
        # defaults.
        assert route.storm_mode_active is True
        assert (
            route.storm_mode_max_stops_per_truck
            == DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK
        )
        assert (
            route.storm_mode_delivery_window_start_hour
            == DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR
        )
        assert (
            route.storm_mode_delivery_window_end_hour
            == DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR
        )


# ---------------------------------------------------------------------------
# Tests: ConfirmationProtocol routing with HIGH risk (Req 9.2.5)
# ---------------------------------------------------------------------------


class TestStormModeConfirmationRouting:
    """Req 9.2.5 — Storm_Mode Route_Plans route through
    ConfirmationProtocol with ``RiskClass.HIGH`` via the storm-specific
    tool name."""

    def test_non_storm_proposal_uses_legacy_tool(self):
        agent, _ = _make_agent()
        route = _make_route_plan(num_stops=3)
        # storm_mode_active defaults to False so this is the legacy path.
        proposal = agent._build_route_proposal(
            route_plan=route, tenant_id="tenant-1"
        )
        assert proposal.risk_class == RiskClass.LOW
        assert proposal.actions[0]["tool_name"] == APPLY_ROUTE_PLAN_TOOL
        assert "storm_mode_active" not in proposal.actions[0]["parameters"]

    def test_storm_mode_proposal_uses_high_risk_tool(self):
        agent, _ = _make_agent()
        route = _make_route_plan(num_stops=3)
        route.storm_mode_active = True
        route.storm_mode_max_stops_per_truck = 10
        route.storm_mode_delivery_window_start_hour = 8.0
        route.storm_mode_delivery_window_end_hour = 16.0
        route.deferred_stops = [
            DeferredRouteStop(
                station_id="deferred-1",
                reason=REASON_DEFERRED_STORM_MODE,
                deferral_cause=CAUSE_OVER_MAX_STOPS,
                original_sequence=10,
                original_eta="2030-01-01T12:00:00+00:00",
            )
        ]
        proposal = agent._build_route_proposal(
            route_plan=route, tenant_id="tenant-1"
        )
        assert proposal.risk_class == RiskClass.HIGH
        action = proposal.actions[0]
        assert action["tool_name"] == APPLY_ROUTE_PLAN_STORM_MODE_TOOL
        params = action["parameters"]
        assert params["storm_mode_active"] is True
        assert params["storm_mode_max_stops_per_truck"] == 10
        assert params["storm_mode_delivery_window_start_hour"] == 8.0
        assert params["storm_mode_delivery_window_end_hour"] == 16.0
        assert len(params["deferred_stops"]) == 1
        assert params["deferred_stops"][0]["reason"] == REASON_DEFERRED_STORM_MODE


# ---------------------------------------------------------------------------
# Tests: End-to-end evaluate() flow persists Storm_Mode annotations
# ---------------------------------------------------------------------------


class TestStormModeEndToEnd:
    @pytest.mark.asyncio
    async def test_storm_mode_plan_persists_with_annotations(self):
        """Run the full evaluate() path and confirm the persisted
        Route_Plan carries the storm_mode fields + deferred stops."""
        # Window 08:00-16:00 UTC — every station will resolve to an ETA
        # inside the window because _build_route_plan() uses ``now`` as
        # the start and adds 30min per stop. We override the cap to 2
        # so the third station is cap-deferred.
        loader = await _settings_loader_factory(
            StormModeRouteSettings(
                max_stops_per_truck=2,
                delivery_window_start_hour=0.0,
                delivery_window_end_hour=24.0,
            )
        )
        agent, deps = _make_agent(
            storm_mode_evaluator=_FakeStormModeEvaluator(state="active"),
            storm_mode_settings_loader=loader,
        )

        agent._proposal_buffer.append(
            _make_loading_proposal(
                station_ids=["station-1", "station-2", "station-3"]
            )
        )
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_station_location_hit("station-1", 6.45, 3.40),
                        _make_station_location_hit("station-2", 6.50, 3.35),
                        _make_station_location_hit("station-3", 6.48, 3.38),
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert len(result) == 1
        proposal = result[0]
        assert proposal.risk_class == RiskClass.HIGH
        action = proposal.actions[0]
        assert action["tool_name"] == APPLY_ROUTE_PLAN_STORM_MODE_TOOL

        # Persisted document must carry the storm annotations.
        persisted_doc = deps["es_service"].index_document.call_args[0][2]
        assert persisted_doc["storm_mode_active"] is True
        assert persisted_doc["storm_mode_max_stops_per_truck"] == 2
        assert len(persisted_doc["stops"]) == 2
        assert len(persisted_doc["deferred_stops"]) == 1
        deferred = persisted_doc["deferred_stops"][0]
        assert deferred["reason"] == REASON_DEFERRED_STORM_MODE
        assert deferred["deferral_cause"] == CAUSE_OVER_MAX_STOPS


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _stop(
    station_id: str,
    eta: str,
    *,
    sequence: int,
    drop=None,
) -> RouteStop:
    return RouteStop(
        station_id=station_id,
        eta=eta,
        drop=drop or {"AGO": 1000.0},
        sequence=sequence,
    )


def _make_route_plan(*, num_stops: int) -> RoutePlan:
    """Build a RoutePlan with ``num_stops`` synthetic in-window stops."""
    base = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)
    stops = [
        _stop(
            f"station-{i}",
            (base + timedelta(minutes=30 * i)).isoformat(),
            sequence=i,
        )
        for i in range(num_stops)
    ]
    return RoutePlan(
        truck_id="truck-1",
        plan_id="plan-1",
        stops=stops,
        distance_km=100.0,
        eta_confidence=0.8,
        tenant_id="tenant-1",
    )
