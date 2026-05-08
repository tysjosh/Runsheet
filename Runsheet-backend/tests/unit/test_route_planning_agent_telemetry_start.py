"""Unit tests for telemetry-first start-position resolution (Task 9.7).

Covers the :func:`fuel.services.truck_start_position.resolve_truck_start_position`
helper and its wiring into
:class:`Agents.overlay.route_planning_agent.RoutePlanningAgent`. The
scenarios mirror the behavioural requirement spelled out in
Requirement 5.4.6:

* Fresh telemetry (<300s old) → start position = telemetry coords,
  ``start_position_source="telemetry"``.
* Stale telemetry (>300s old) → falls back to depot coords,
  ``start_position_source="depot"``.
* No telemetry at all → falls back to depot coords.
* Telemetry that belongs to a different tenant is ignored.
* No depot either → :class:`NoDepotConfiguredError` (helper) / route
  skipped with no HTTP 400 leak into the next loading plan (agent).
* The Route_Plan persisted to ``mvp_routes`` carries the provenance
  annotation on every successful run.

Validates: Requirement 5.4.6.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
)
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from fuel.services.truck_start_position import (
    NoDepotConfiguredError,
    SOURCE_DEPOT,
    SOURCE_TELEMETRY,
    TELEMETRY_FRESHNESS_SECONDS,
    TruckStartPosition,
    resolve_truck_start_position,
)


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
TRUCK_ID = "truck-42"
DEPOT_COORDS = (34.0522, -118.2437)  # Los Angeles — tenant's depot
TELEMETRY_COORDS = (40.7128, -74.0060)  # New York — live truck position
FIXED_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _telemetry_source(
    *,
    tenant_id: str = TENANT_A,
    truck_id: str = TRUCK_ID,
    lat: float = TELEMETRY_COORDS[0],
    lon: float = TELEMETRY_COORDS[1],
    recorded_at: datetime | None = None,
) -> dict:
    """Build a ``truck_telemetry`` ``_source`` dict matching the mapping."""

    recorded_at = recorded_at or (FIXED_NOW - timedelta(seconds=30))
    return {
        "telemetry_id": f"telem_{truck_id}",
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "driver_id": "driver-1",
        "location": {"lat": lat, "lon": lon},
        "location_lat": lat,
        "location_lon": lon,
        "speed_kph": 55.0,
        "engine_on": True,
        "odometer_km": 12345.6,
        "hos_status": "OnDuty",
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
    }


def _telemetry_response(sources: list[dict]) -> dict:
    """Wrap ``_source`` dicts in the canonical ES search envelope."""

    return {"hits": {"hits": [{"_source": s} for s in sources]}}


def _make_es(search_return: dict | None = None):
    """Build a mock ES service whose search returns a fixed envelope."""

    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value=search_return or {"hits": {"hits": []}}
    )
    es.index_document = AsyncMock()
    return es


# ---------------------------------------------------------------------------
# Helper tests — resolve_truck_start_position
# ---------------------------------------------------------------------------


class TestResolveTruckStartPosition:
    @pytest.mark.asyncio
    async def test_fresh_telemetry_returns_telemetry_source(self):
        """Telemetry <300s old → coords come from the telemetry row."""

        es = _make_es(_telemetry_response([_telemetry_source()]))
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert isinstance(result, TruckStartPosition)
        assert result.source == SOURCE_TELEMETRY
        assert result.lat == pytest.approx(TELEMETRY_COORDS[0])
        assert result.lon == pytest.approx(TELEMETRY_COORDS[1])
        depot_resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_telemetry_falls_back_to_depot(self):
        """Telemetry older than 300s is ignored in favour of the depot."""

        stale = FIXED_NOW - timedelta(seconds=TELEMETRY_FRESHNESS_SECONDS + 1)
        es = _make_es(
            _telemetry_response([_telemetry_source(recorded_at=stale)])
        )
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert result.source == SOURCE_DEPOT
        assert result.lat == pytest.approx(DEPOT_COORDS[0])
        assert result.lon == pytest.approx(DEPOT_COORDS[1])
        depot_resolver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_telemetry_falls_back_to_depot(self):
        """Empty telemetry hits → depot fallback."""

        es = _make_es({"hits": {"hits": []}})
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert result.source == SOURCE_DEPOT

    @pytest.mark.asyncio
    async def test_cross_tenant_telemetry_is_ignored(self):
        """A telemetry row belonging to another tenant must not leak in.

        The helper queries with ``term tenant_id`` so this scenario
        should not happen at the storage layer, but we still validate
        the defense-in-depth check on the returned ``_source`` so a
        drifted document cannot drive another tenant's route plan.
        """

        cross_tenant = _telemetry_source(tenant_id=TENANT_B)
        es = _make_es(_telemetry_response([cross_tenant]))
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert result.source == SOURCE_DEPOT
        depot_resolver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_depot_either_raises(self):
        """Neither telemetry nor depot coords → HTTP-400-equivalent error."""

        es = _make_es({"hits": {"hits": []}})
        depot_resolver = AsyncMock(return_value=None)

        with pytest.raises(NoDepotConfiguredError) as excinfo:
            await resolve_truck_start_position(
                tenant_id=TENANT_A,
                truck=_truck(),
                depot_resolver=depot_resolver,
                es_service=es,
                now=FIXED_NOW,
            )
        assert excinfo.value.tenant_id == TENANT_A
        assert excinfo.value.truck_id == TRUCK_ID
        assert excinfo.value.reason_code == "no_depot_configured"

    @pytest.mark.asyncio
    async def test_sync_depot_resolver_is_supported(self):
        """The resolver may be sync or async — helper handles both."""

        es = _make_es({"hits": {"hits": []}})

        def _sync_resolver(tenant_id, truck):
            assert tenant_id == TENANT_A
            assert truck["truck_id"] == TRUCK_ID
            return DEPOT_COORDS

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=_sync_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert result.source == SOURCE_DEPOT
        assert result.lat == pytest.approx(DEPOT_COORDS[0])

    @pytest.mark.asyncio
    async def test_telemetry_without_recorded_at_is_treated_as_stale(self):
        """A row missing ``recorded_at`` cannot be proven fresh — fall back."""

        source = _telemetry_source()
        source.pop("recorded_at")
        es = _make_es(_telemetry_response([source]))
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        result = await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        assert result.source == SOURCE_DEPOT

    @pytest.mark.asyncio
    async def test_telemetry_query_filters_by_tenant_and_truck(self):
        """The ES query must AND tenant_id AND truck_id."""

        es = _make_es({"hits": {"hits": []}})
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)

        await resolve_truck_start_position(
            tenant_id=TENANT_A,
            truck=_truck(),
            depot_resolver=depot_resolver,
            es_service=es,
            now=FIXED_NOW,
        )

        es.search_documents.assert_awaited_once()
        args, _ = es.search_documents.call_args
        index_name, query, size = args
        assert index_name == "truck_telemetry"
        assert size == 1
        must = query["query"]["bool"]["must"]
        assert {"term": {"tenant_id": TENANT_A}} in must
        assert {"term": {"truck_id": TRUCK_ID}} in must
        # Newest-first ordering so size=1 returns the latest reading.
        assert query["sort"] == [{"recorded_at": {"order": "desc"}}]


def _truck() -> dict:
    return {"truck_id": TRUCK_ID, "assigned_depot_id": "depot_abc"}


# ---------------------------------------------------------------------------
# Agent wiring tests — RoutePlanningAgent.evaluate()
# ---------------------------------------------------------------------------


def _make_loading_proposal(
    *,
    truck_id: str = TRUCK_ID,
    plan_id: str = "plan-1",
    tenant_id: str = TENANT_A,
):
    assignments = [
        {
            "compartment_id": "comp-0",
            "station_id": "station-1",
            "fuel_grade": "AGO",
            "quantity_liters": 5000.0,
            "compartment_capacity_liters": 10000.0,
        }
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


def _station_hit(station_id: str, lat: float, lon: float) -> dict:
    return {
        "_source": {
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
        }
    }


def _live_fresh_telemetry(*, tenant_id: str = TENANT_A) -> dict:
    """Build a telemetry row whose ``recorded_at`` is live-wallclock fresh.

    The agent uses ``datetime.now(timezone.utc)`` internally (no clock
    injection today) so the integration-style tests have to produce
    timestamps relative to the actual current time rather than
    :data:`FIXED_NOW`. Offsetting by 30 seconds keeps the reading well
    inside the 300-second freshness window.
    """

    return _telemetry_source(
        tenant_id=tenant_id,
        recorded_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )


def _live_stale_telemetry(*, tenant_id: str = TENANT_A) -> dict:
    """Build a telemetry row older than :data:`TELEMETRY_FRESHNESS_SECONDS`."""

    return _telemetry_source(
        tenant_id=tenant_id,
        recorded_at=datetime.now(timezone.utc)
        - timedelta(seconds=TELEMETRY_FRESHNESS_SECONDS + 30),
    )


def _build_agent(*, depot_resolver, search_side_effect):
    """Build a RoutePlanningAgent with a depot resolver + ES search stub."""

    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(side_effect=search_side_effect)
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)
    # Disable the traffic-aware path so the agent uses Haversine and
    # does not call out through the provider stack during these tests.
    feature_flags.get_overlay_state = AsyncMock(return_value="disabled")

    agent = RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=MagicMock(),
        feature_flag_service=feature_flags,
        depot_resolver=depot_resolver,
    )
    return agent, es_service


def _search_sequence(
    *,
    telemetry_resp: dict,
    station_lat: float = 40.8,
    station_lon: float = -74.1,
):
    """Build a side-effect that routes queries to the right stub response.

    The Route_Planning_Agent issues three searches per loading plan in
    this order:
        1. ``truck_telemetry`` — handled by the telemetry resolver.
        2. ``fuel_stations`` — station locations (Req 4.3).
        3. ``fuel_stations`` — SLA windows (Req 4.4).

    We dispatch by index name so the ordering stays robust if the
    agent reorders these calls.
    """

    station_locations = {
        "hits": {"hits": [_station_hit("station-1", station_lat, station_lon)]}
    }
    station_slas = {"hits": {"hits": []}}
    station_calls = {"n": 0}

    async def _side_effect(index_name, query, size):
        if index_name == "truck_telemetry":
            return telemetry_resp
        if index_name == "fuel_stations":
            station_calls["n"] += 1
            # First station-index hit is locations; second is SLA windows.
            return station_locations if station_calls["n"] == 1 else station_slas
        return {"hits": {"hits": []}}

    return _side_effect


class TestAgentUsesTelemetryWhenFresh:
    @pytest.mark.asyncio
    async def test_fresh_telemetry_sets_start_position_source_to_telemetry(self):
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)
        side_effect = _search_sequence(
            telemetry_resp=_telemetry_response([_live_fresh_telemetry()])
        )
        agent, es = _build_agent(
            depot_resolver=depot_resolver,
            search_side_effect=side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        assert len(results) == 1
        # The Route_Plan persisted to mvp_routes should carry the
        # telemetry annotation end-to-end.
        assert es.index_document.await_count == 1
        _, _, doc = es.index_document.await_args.args
        assert doc["start_position_source"] == SOURCE_TELEMETRY
        assert doc["start_position_lat"] == pytest.approx(TELEMETRY_COORDS[0])
        assert doc["start_position_lon"] == pytest.approx(TELEMETRY_COORDS[1])
        # Depot resolver MUST NOT be consulted when telemetry is fresh.
        depot_resolver.assert_not_awaited()


class TestAgentFallsBackToDepot:
    @pytest.mark.asyncio
    async def test_stale_telemetry_falls_back_to_depot(self):
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)
        side_effect = _search_sequence(
            telemetry_resp=_telemetry_response([_live_stale_telemetry()])
        )
        agent, es = _build_agent(
            depot_resolver=depot_resolver,
            search_side_effect=side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        assert len(results) == 1
        _, _, doc = es.index_document.await_args.args
        assert doc["start_position_source"] == SOURCE_DEPOT
        assert doc["start_position_lat"] == pytest.approx(DEPOT_COORDS[0])
        assert doc["start_position_lon"] == pytest.approx(DEPOT_COORDS[1])
        depot_resolver.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_telemetry_falls_back_to_depot(self):
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)
        side_effect = _search_sequence(
            telemetry_resp={"hits": {"hits": []}}
        )
        agent, es = _build_agent(
            depot_resolver=depot_resolver,
            search_side_effect=side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        assert len(results) == 1
        _, _, doc = es.index_document.await_args.args
        assert doc["start_position_source"] == SOURCE_DEPOT

    @pytest.mark.asyncio
    async def test_cross_tenant_telemetry_falls_back_to_depot(self):
        """A ``truck_telemetry`` row owned by another tenant is ignored."""

        cross_tenant = _live_fresh_telemetry(tenant_id=TENANT_B)
        depot_resolver = AsyncMock(return_value=DEPOT_COORDS)
        side_effect = _search_sequence(
            telemetry_resp=_telemetry_response([cross_tenant])
        )
        agent, es = _build_agent(
            depot_resolver=depot_resolver,
            search_side_effect=side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        assert len(results) == 1
        _, _, doc = es.index_document.await_args.args
        assert doc["start_position_source"] == SOURCE_DEPOT


class TestAgentHandlesNoDepotConfigured:
    @pytest.mark.asyncio
    async def test_skips_route_when_no_telemetry_and_no_depot(self):
        """Requirement 2.2.4 is preserved: no telemetry + no depot → skip."""

        depot_resolver = AsyncMock(return_value=None)
        side_effect = _search_sequence(
            telemetry_resp={"hits": {"hits": []}}
        )
        agent, es = _build_agent(
            depot_resolver=depot_resolver,
            search_side_effect=side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        # No Route_Plan is produced when neither a fresh telemetry
        # reading nor a depot is available for the tenant.
        assert results == []
        es.index_document.assert_not_awaited()


class TestAgentWithoutResolverIsLegacy:
    @pytest.mark.asyncio
    async def test_legacy_behaviour_when_no_resolver_injected(self):
        """When no resolver is wired, the agent must not stamp provenance.

        This preserves the pre-Task-9.7 behaviour so the existing test
        suite and any bootstrap that hasn't wired the resolver continues
        to function unchanged.
        """

        # No telemetry query should be issued because the agent skips
        # the resolver entirely. We still wire the station stubs.
        async def _side_effect(index_name, query, size):
            if index_name == "truck_telemetry":  # pragma: no cover - sanity
                raise AssertionError(
                    "truck_telemetry must not be queried without a resolver"
                )
            if index_name == "fuel_stations":
                return {"hits": {"hits": [_station_hit("station-1", 40.8, -74.1)]}}
            return {"hits": {"hits": []}}

        agent, es = _build_agent(
            depot_resolver=None,
            search_side_effect=_side_effect,
        )
        agent._proposal_buffer.append(_make_loading_proposal())

        results = await agent.evaluate([])

        assert len(results) == 1
        _, _, doc = es.index_document.await_args.args
        # Legacy path leaves provenance fields unset.
        assert doc["start_position_source"] is None
        assert doc["start_position_lat"] is None
        assert doc["start_position_lon"] is None
