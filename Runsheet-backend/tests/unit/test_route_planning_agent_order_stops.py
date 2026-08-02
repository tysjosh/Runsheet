"""
Unit tests for order-driven stop resolution in the Route_Planning_Agent.

The agent used to resolve every stop coordinate and SLA window from the
``fuel_stations`` index alone. For a US customer whose demand lives in
``customer_tanks``, the loading agent stamps ``station_id`` with the
``customer_id`` (CompartmentLoadingAgent._build_delivery_requests_from_orders)
and no ``fuel_stations`` document exists — so ``_query_station_locations``
returned ``{}``, the per-truck loop hit a bare ``continue``, and the run
reported success having produced zero routes.

These tests pin the fix:

- a customer_tanks tenant with zero ``fuel_stations`` documents routes,
  because coordinates come from the order's ship-to (join:
  ``CompartmentAssignment.order_id`` → ``fuel_orders_current.order_id``)
- a legacy retail tenant with ``fuel_stations`` and no order coordinates
  still routes via the station fallback
- every exit from the per-truck loop records a structured skip reason on
  the run result, so routing zero of N trucks is not a clean success
- an order's ``delivery_window_*`` beats ``fuel_stations.sla_delivery_window_*``

Requirements: 4.1, 4.3, 4.4, 5.2.2, 5.2.3
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    FUEL_STATIONS_INDEX,
    RoutePlanningAgent,
)

TENANT_ID = "tenant-us-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps():
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

    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": feature_flags,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return RoutePlanningAgent(**deps), deps


def _make_loading_proposal(
    *,
    truck_id: str = "truck-1",
    plan_id: str = "plan-1",
    tenant_id: str = TENANT_ID,
    stops: Optional[List[Dict[str, Any]]] = None,
) -> InterventionProposal:
    """Build the ``apply_loading_plan`` proposal the agent consumes.

    ``stops`` is a list of ``{"station_id": ..., "order_id": ...}`` dicts
    mirroring what the compartment solver emits. For customer-tank demand
    ``station_id`` carries the ``customer_id`` and ``order_id`` is set;
    for legacy retail demand ``order_id`` is absent.
    """
    if stops is None:
        stops = [{"station_id": "cust-1", "order_id": "ord-1"}]

    assignments = []
    for i, stop in enumerate(stops):
        assignment = {
            "compartment_id": f"comp-{i}",
            "station_id": stop["station_id"],
            "fuel_grade": "AGO",
            "quantity_liters": 5000.0,
            "compartment_capacity_liters": 10000.0,
        }
        if stop.get("order_id"):
            assignment["order_id"] = stop["order_id"]
        assignments.append(assignment)

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


def _order(
    order_id: str,
    *,
    lat: Optional[float] = 32.7767,
    lon: Optional[float] = -96.7970,
    **extra: Any,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": TENANT_ID,
        "status": "confirmed",
        "ship_to_lat": lat,
        "ship_to_lon": lon,
        "ship_to_address": "123 Main St, Dallas, TX",
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
    }
    doc.update(extra)
    return doc


def _station(station_id: str, lat: float, lon: float, **extra: Any):
    source = {"station_id": station_id, "latitude": lat, "longitude": lon}
    source.update(extra)
    return {"_source": source}


def _es_dispatcher(
    *,
    orders: Optional[List[Dict[str, Any]]] = None,
    station_hits: Optional[List[Dict[str, Any]]] = None,
):
    """Route ES searches by index name so ordering stays robust."""
    orders = orders or []
    station_hits = station_hits or []

    async def _side_effect(index_name, query, size):
        if index_name == FUEL_ORDERS_CURRENT_INDEX:
            return {"hits": {"hits": [{"_source": o} for o in orders]}}
        if index_name == FUEL_STATIONS_INDEX:
            return {"hits": {"hits": station_hits}}
        return {"hits": {"hits": []}}

    return _side_effect


# ---------------------------------------------------------------------------
# Customer-tank tenant with zero fuel_stations documents (the live defect)
# ---------------------------------------------------------------------------


class TestCustomerTankTenantRoutes:
    """Req 4.3, 5.2.2 — order ship-to drives the stop coordinate."""

    @pytest.mark.asyncio
    async def test_routes_with_zero_fuel_stations_documents(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[
                    _order("ord-1", lat=32.7767, lon=-96.7970),
                    _order("ord-2", lat=32.8000, lon=-96.8500),
                ],
                station_hits=[],  # no fuel_stations documents at all
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[
                    {"station_id": "cust-1", "order_id": "ord-1"},
                    {"station_id": "cust-2", "order_id": "ord-2"},
                ]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1, (
            "customer_tanks demand must route from order ship-to even with "
            "zero fuel_stations documents"
        )
        params = result[0].actions[0]["parameters"]
        assert len(params["stops"]) == 2
        assert {s["station_id"] for s in params["stops"]} == {
            "cust-1",
            "cust-2",
        }
        assert params["distance_km"] > 0.0
        assert agent.last_route_skips == []
        assert agent.cycle_metrics["routing_degraded"] is False

    @pytest.mark.asyncio
    async def test_geocoded_ship_to_address_resolves_stop(self):
        """Orders with no lat/lon fall back to geocoding the ship-to."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[
                    _order("ord-1", lat=None, lon=None),
                    _order("ord-2", lat=32.9, lon=-96.9),
                ],
                station_hits=[],
            )
        )
        deps["es_service"].geocode_address = AsyncMock(
            return_value={"lat": 29.7604, "lon": -95.3698}
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[
                    {"station_id": "cust-1", "order_id": "ord-1"},
                    {"station_id": "cust-2", "order_id": "ord-2"},
                ]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1
        assert len(result[0].actions[0]["parameters"]["stops"]) == 2


# ---------------------------------------------------------------------------
# Legacy retail tenant — station fallback must survive
# ---------------------------------------------------------------------------


class TestLegacyStationFallback:
    """Req 4.3 — fuel_stations remains the fallback for retail tenants."""

    @pytest.mark.asyncio
    async def test_routes_via_station_lookup_without_order_coordinates(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[],  # no routable orders at all
                station_hits=[
                    _station("station-1", 6.45, 3.40),
                    _station("station-2", 6.50, 3.35),
                ],
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[
                    {"station_id": "station-1"},
                    {"station_id": "station-2"},
                ]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert {s["station_id"] for s in params["stops"]} == {
            "station-1",
            "station-2",
        }
        assert agent.last_route_skips == []

    @pytest.mark.asyncio
    async def test_order_without_coordinates_falls_back_to_station(self):
        """An order that resolves to nothing still uses its station doc."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[_order("ord-1", lat=None, lon=None)],
                station_hits=[_station("station-1", 6.45, 3.40)],
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[{"station_id": "station-1", "order_id": "ord-1"}]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1
        stops = result[0].actions[0]["parameters"]["stops"]
        assert [s["station_id"] for s in stops] == ["station-1"]


# ---------------------------------------------------------------------------
# Structured skip reasons — no more silent continues
# ---------------------------------------------------------------------------


class TestSkipReasons:
    """Req 4.1 — a run that routes zero of N trucks says why."""

    @pytest.mark.asyncio
    async def test_unresolvable_stops_populate_skip_reasons(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(orders=[], station_hits=[])
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                truck_id="truck-9",
                plan_id="plan-9",
                stops=[{"station_id": "cust-1", "order_id": "ord-1"}],
            )
        )

        result = await agent.evaluate([])

        assert result == []
        skips = agent.last_route_skips
        assert len(skips) == 1
        assert skips[0].reason_code == "unresolvable_stop_locations"
        assert skips[0].truck_id == "truck-9"
        assert skips[0].plan_id == "plan-9"
        assert skips[0].missing == ["cust-1"]

        metrics = agent.cycle_metrics
        assert metrics["routing_degraded"] is True
        assert metrics["trucks_routed"] == 0
        assert metrics["loading_plans_considered"] == 1
        assert [s["reason_code"] for s in metrics["route_skips"]] == [
            "unresolvable_stop_locations"
        ]

    @pytest.mark.asyncio
    async def test_assignments_without_station_id_record_reason(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(orders=[], station_hits=[])
        )
        proposal = _make_loading_proposal(truck_id="truck-3")
        proposal.actions[0]["parameters"]["assignments"] = [
            {
                "compartment_id": "comp-0",
                "station_id": "",
                "fuel_grade": "AGO",
                "quantity_liters": 100.0,
                "compartment_capacity_liters": 1000.0,
            }
        ]
        agent._proposal_buffer.append(proposal)

        result = await agent.evaluate([])

        assert result == []
        assert [s.reason_code for s in agent.last_route_skips] == [
            "no_demand_identifiers"
        ]
        assert agent.last_route_skips[0].missing == ["station_id"]
        assert agent.cycle_metrics["routing_degraded"] is True

    @pytest.mark.asyncio
    async def test_business_exclusion_records_its_own_reason_code(self):
        """An expired cargo tank certification is a dispatcher-visible fact."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[_order("ord-1")],
                station_hits=[],
            )
        )
        cert_service = MagicMock()
        cert_service.is_dispatch_eligible = AsyncMock(
            return_value=MagicMock(eligible=False, reasons=["expired_V"])
        )
        agent.set_asset_certification_service(cert_service)
        agent._proposal_buffer.append(
            _make_loading_proposal(truck_id="truck-7", plan_id="plan-7")
        )

        result = await agent.evaluate([])

        assert result == []
        assert [s.reason_code for s in agent.last_route_skips] == [
            "asset_certification_expired"
        ]
        assert agent.last_route_skips[0].truck_id == "truck-7"

    @pytest.mark.asyncio
    async def test_skips_ride_alongside_produced_plans(self):
        """Mixed run: the produced plan carries the exclusions."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[_order("ord-1")],
                station_hits=[],
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                truck_id="truck-bad",
                plan_id="plan-bad",
                stops=[{"station_id": "cust-missing", "order_id": "ord-none"}],
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                truck_id="truck-good",
                plan_id="plan-good",
                stops=[{"station_id": "cust-1", "order_id": "ord-1"}],
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1
        recorded = result[-1].actions[-1]["parameters"]["route_skips"]
        assert [r["reason_code"] for r in recorded] == [
            "unresolvable_stop_locations"
        ]
        assert recorded[0]["truck_id"] == "truck-bad"
        assert agent.cycle_metrics["routing_degraded"] is True
        assert agent.cycle_metrics["trucks_routed"] == 1


# ---------------------------------------------------------------------------
# Order delivery window beats station SLA window
# ---------------------------------------------------------------------------


class TestWindowPrecedence:
    """Req 4.4, 5.2.3 — a concrete order commitment beats a static default."""

    def test_order_window_wins_when_both_present(self):
        agent, _ = _make_agent()
        now = datetime.now(timezone.utc)
        order = _order(
            "ord-1",
            delivery_window_start=(now + timedelta(hours=1)).isoformat(),
            delivery_window_end=(now + timedelta(hours=3)).isoformat(),
        )

        resolved = agent._resolve_sla_windows(
            station_ids=["cust-1"],
            station_sla_windows={"cust-1": (6.0, 18.0)},
            order_ids_by_station={"cust-1": ["ord-1"]},
            orders_by_id={"ord-1": order},
        )

        start_h, end_h = resolved["cust-1"]
        assert start_h == pytest.approx(1.0, abs=0.05)
        assert end_h == pytest.approx(3.0, abs=0.05)

    def test_station_window_used_when_order_has_none(self):
        agent, _ = _make_agent()
        resolved = agent._resolve_sla_windows(
            station_ids=["station-1"],
            station_sla_windows={"station-1": (6.0, 18.0)},
            order_ids_by_station={"station-1": ["ord-1"]},
            orders_by_id={"ord-1": _order("ord-1")},
        )
        assert resolved["station-1"] == (6.0, 18.0)

    def test_tightest_order_window_wins_for_multi_order_stop(self):
        agent, _ = _make_agent()
        now = datetime.now(timezone.utc)
        orders_by_id = {
            "ord-wide": _order(
                "ord-wide",
                delivery_window_start=(now + timedelta(hours=1)).isoformat(),
                delivery_window_end=(now + timedelta(hours=8)).isoformat(),
            ),
            "ord-tight": _order(
                "ord-tight",
                delivery_window_start=(now + timedelta(hours=1)).isoformat(),
                delivery_window_end=(now + timedelta(hours=2)).isoformat(),
            ),
        }
        resolved = agent._resolve_sla_windows(
            station_ids=["cust-1"],
            station_sla_windows={},
            order_ids_by_station={"cust-1": ["ord-wide", "ord-tight"]},
            orders_by_id=orders_by_id,
        )
        assert resolved["cust-1"][1] == pytest.approx(2.0, abs=0.05)

    @pytest.mark.asyncio
    async def test_order_window_drives_sla_violation_end_to_end(self):
        """A past-due order window degrades eta_confidence; the station
        default (wide open) would not have."""
        agent, deps = _make_agent()
        now = datetime.now(timezone.utc)
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_es_dispatcher(
                orders=[
                    _order(
                        "ord-1",
                        lat=6.45,
                        lon=3.40,
                        delivery_window_start=now.isoformat(),
                        delivery_window_end=now.isoformat(),
                    )
                ],
                station_hits=[
                    _station(
                        "station-1",
                        6.45,
                        3.40,
                        sla_delivery_window_start=0.0,
                        sla_delivery_window_end=999.0,
                    )
                ],
            )
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[{"station_id": "station-1", "order_id": "ord-1"}]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1
        # eta_confidence drops to 0.4 when a stop is SLA at-risk; the
        # station's 0..999h default could never trigger that.
        assert result[0].confidence == pytest.approx(0.4)
