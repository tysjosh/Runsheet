"""A plan with every stop deferred is not a route, and its mileage is not real.

``_maybe_apply_storm_mode`` moves stops into ``deferred_stops`` and shortens
``route_plan.stops``. Two things did not follow.

**The run still claimed success.** With Storm_Mode active and an 08:00–16:00
delivery window, an evening run defers every stop of every truck. The agent
persisted four plans with ``stops: []``, reported the run ``complete`` and
``degraded: false``, and queued four HIGH-risk ``apply_route_plan_storm_mode``
approvals — approving one would dispatch a truck to visit nothing. The deferrals
are recorded per stop, so this was not silent at the document level; what was
wrong is the run asserting it produced something dispatchable.

**The distance described a route that no longer existed.** ``distance_km`` came
from the solve over the full stop set and nothing recomputed it, so the empty
plans reported ``distance_km: 1633.99``. Distance feeds ``objective_value`` and
the cost-analysis endpoint, so a stale figure propagates.

The fix is deliberately narrow: an *all*-deferred plan is refused as a route
(``all_stops_deferred``), and a *partially* deferred plan keeps its surviving
stops with the distance recomputed over them. A partial deferral is still a real
route and must still be dispatchable.

Requirements: 4.1 (skip reasons on the run result), 9.2.4, 9.3.1, 9.3.2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    FUEL_STATIONS_INDEX,
    RoutePlanningAgent,
    StormModeRouteSettings,
)
from Agents.support.fuel_distribution_models import RoutePlan, RouteStop
from fuel.services.truck_start_position import SOURCE_DEPOT, TruckStartPosition

TENANT = "tenant-storm-1"

# Houston-ish coordinates, ~11 km apart, so a recomputed leg is comfortably
# distinguishable from zero and from the stale value under test.
_COORDS = {
    "station-1": (29.7604, -95.3698),
    "station-2": (29.8604, -95.3698),
    "station-3": (29.9604, -95.3698),
}
_START = TruckStartPosition(lat=29.6604, lon=-95.3698, source=SOURCE_DEPOT)


class _ActiveStormMode:
    async def get_state(self, tenant_id: str):
        from fuel.services.storm_mode_evaluator import PersistedState

        return PersistedState(
            state="active",
            updated_at=None,
            triggering_alert_ids=[],
            expected_end_at=None,
        )


def _loader(settings: StormModeRouteSettings):
    async def _load(tenant_id: str):
        return settings

    return _load


def _make_agent(*, window: tuple, cap: int = 10, storm: bool = True):
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.index_document = AsyncMock()

    async def _search(index_name, query, size):
        if index_name == FUEL_ORDERS_CURRENT_INDEX:
            return {"hits": {"hits": []}}
        if index_name == FUEL_STATIONS_INDEX:
            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "station_id": sid,
                                "latitude": lat,
                                "longitude": lon,
                            }
                        }
                        for sid, (lat, lon) in _COORDS.items()
                    ]
                }
            }
        return {"hits": {"hits": []}}

    es_service.search_documents = AsyncMock(side_effect=_search)

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)
    feature_flags.get_overlay_state = AsyncMock(return_value="disabled")

    agent = RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=MagicMock(),
        feature_flag_service=feature_flags,
        storm_mode_evaluator=_ActiveStormMode() if storm else None,
        storm_mode_settings_loader=_loader(
            StormModeRouteSettings(
                max_stops_per_truck=cap,
                delivery_window_start_hour=window[0],
                delivery_window_end_hour=window[1],
            )
        ),
    )

    async def _resolve(**kwargs):
        return _START

    agent._resolve_start_position = _resolve  # type: ignore[assignment]
    return agent, es_service


def _proposal(station_ids: List[str], truck_id="truck-1", plan_id="plan-1"):
    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "assignments": [
                        {
                            "compartment_id": f"comp-{i}",
                            "station_id": sid,
                            "fuel_grade": "DIESEL_2",
                            "quantity_liters": 5000.0,
                            "compartment_capacity_liters": 10000.0,
                        }
                        for i, sid in enumerate(station_ids)
                    ],
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
        tenant_id=TENANT,
    )


# A zero-length window rejects every stop whatever its ETA, which is the
# deterministic stand-in for "the run happened outside the delivery window".
_CLOSED_WINDOW = (8.0, 8.0)
_OPEN_WINDOW = (0.0, 24.0)


class TestAllStopsDeferred:
    @pytest.mark.asyncio
    async def test_no_proposal_is_emitted(self):
        agent, _ = _make_agent(window=_CLOSED_WINDOW)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        result = await agent.evaluate([])

        assert result == [], (
            "a plan with no stops must not reach the dispatcher as an "
            f"approvable route: {result}"
        )

    @pytest.mark.asyncio
    async def test_the_skip_names_the_cause_and_the_deferred_stations(self):
        agent, _ = _make_agent(window=_CLOSED_WINDOW)
        agent._proposal_buffer.append(
            _proposal(list(_COORDS), truck_id="truck-7", plan_id="plan-7")
        )

        await agent.evaluate([])

        assert [s.reason_code for s in agent.last_route_skips] == [
            "all_stops_deferred"
        ]
        skip = agent.last_route_skips[0]
        assert skip.truck_id == "truck-7"
        assert skip.plan_id == "plan-7"
        assert sorted(skip.missing) == sorted(_COORDS)
        assert "outside_delivery_window" in (skip.detail or "")

    @pytest.mark.asyncio
    async def test_the_run_is_degraded(self):
        agent, _ = _make_agent(window=_CLOSED_WINDOW)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        await agent.evaluate([])

        metrics = agent.cycle_metrics
        assert metrics["degraded"] is True
        assert metrics["trucks_routed"] == 0
        assert metrics["trucks_skipped"] == 1

    @pytest.mark.asyncio
    async def test_nothing_is_written_to_elasticsearch(self):
        """A refused route must not be persisted either.

        Otherwise ``GET /plan/{id}`` would serve a stopless route the run
        already declined to produce.
        """
        agent, es_service = _make_agent(window=_CLOSED_WINDOW)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        await agent.evaluate([])

        written = [c.args[0] for c in es_service.index_document.await_args_list]
        assert "mvp_routes" not in written, f"persisted anyway: {written}"


class TestPartialDeferralStaysARoute:
    """The counterweight. Deferring some stops must not refuse the route."""

    @pytest.mark.asyncio
    async def test_a_capped_route_is_still_proposed(self):
        agent, _ = _make_agent(window=_OPEN_WINDOW, cap=2)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        result = await agent.evaluate([])

        assert len(result) == 1, f"partial deferral refused the route: {result}"
        params = result[0].actions[0]["parameters"]
        assert len(params["stops"]) == 2
        assert agent.last_route_skips == []
        assert agent.cycle_metrics["degraded"] is False

    @pytest.mark.asyncio
    async def test_distance_is_recomputed_over_the_surviving_stops(self):
        """Two stops must not carry the mileage of a three-stop solve."""
        agent, es_service = _make_agent(window=_OPEN_WINDOW, cap=2)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        await agent.evaluate([])

        docs = [
            c.args[2]
            for c in es_service.index_document.await_args_list
            if c.args[0] == "mvp_routes"
        ]
        assert len(docs) == 1
        doc = docs[0]
        assert len(doc["stops"]) == 2
        assert len(doc["deferred_stops"]) == 1

        # start -> station-1 -> station-2, each leg ~11.1 km at this latitude.
        from Agents.support.route_solver import compute_distance

        kept = [s["station_id"] for s in doc["stops"]]
        points = [(_START.lat, _START.lon)] + [_COORDS[s] for s in kept]
        expected = sum(
            compute_distance(a[0], a[1], b[0], b[1])
            for a, b in zip(points, points[1:])
        )
        assert doc["distance_km"] == pytest.approx(round(expected, 2), abs=0.01)


class TestDistanceRecomputationHelper:
    """Direct cases the evaluate() path cannot reach cleanly."""

    def _plan(self, station_ids, distance_km=1633.99):
        return RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            tenant_id=TENANT,
            distance_km=distance_km,
            eta_confidence=0.75,
            stops=[
                RouteStop(
                    station_id=sid,
                    eta=(
                        datetime.now(timezone.utc) + timedelta(hours=i + 1)
                    ).isoformat(),
                    drop={"DIESEL_2": 100.0},
                    sequence=i,
                )
                for i, sid in enumerate(station_ids)
            ],
        )

    def test_zero_stops_means_zero_distance(self):
        agent, _ = _make_agent(window=_OPEN_WINDOW)
        plan = self._plan([])
        agent._recompute_distance_km(
            route_plan=plan,
            station_locations={k: {"lat": v[0], "lon": v[1]} for k, v in _COORDS.items()},
            start_position=_START,
        )
        assert plan.distance_km == 0.0, (
            "an empty route reported 1633.99 km in production"
        )

    def test_missing_coordinates_leave_the_value_alone(self):
        """Inventing a number would be worse than carrying a stale one.

        The warning names the route and the stations, so the stale value is at
        least attributable rather than silent.
        """
        agent, _ = _make_agent(window=_OPEN_WINDOW)
        plan = self._plan(["station-1", "station-2"])
        agent._recompute_distance_km(
            route_plan=plan,
            station_locations={"station-1": {"lat": 29.76, "lon": -95.37}},
            start_position=_START,
        )
        assert plan.distance_km == 1633.99

    def test_no_locations_at_all_leaves_the_value_alone(self):
        agent, _ = _make_agent(window=_OPEN_WINDOW)
        plan = self._plan(["station-1"])
        agent._recompute_distance_km(
            route_plan=plan, station_locations=None, start_position=_START
        )
        assert plan.distance_km == 1633.99


class TestStormModeInactiveIsUnchanged:
    """No evaluator wired: neither guard may fire."""

    @pytest.mark.asyncio
    async def test_route_is_proposed_with_the_solver_distance(self):
        agent, es_service = _make_agent(window=_CLOSED_WINDOW, storm=False)
        agent._proposal_buffer.append(_proposal(list(_COORDS)))

        result = await agent.evaluate([])

        assert len(result) == 1, (
            "a closed storm window must not affect a tenant with no evaluator"
        )
        assert agent.last_route_skips == []
        docs = [
            c.args[2]
            for c in es_service.index_document.await_args_list
            if c.args[0] == "mvp_routes"
        ]
        assert len(docs[0]["stops"]) == len(_COORDS)
        assert docs[0]["deferred_stops"] == []
