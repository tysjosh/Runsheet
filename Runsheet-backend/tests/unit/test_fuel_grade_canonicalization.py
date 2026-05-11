"""
Regression tests for Capability 6, Requirement 6.1.4 — every fuel_grade /
fuel_type write path MUST canonicalize its input via
``services.fuel_product_catalog.canonicalize`` before persistence so US codes
and NG legacy aliases converge on the same canonical product_code in
Elasticsearch (e.g. ``AGO`` -> ``DIESEL_2``, ``PMS`` -> ``GASOLINE_REG``).

Covers the six write paths listed in task 2.6:

* orders — not yet implemented (no order endpoint exists).
* loading plans — ``CompartmentLoadingAgent._persist_loading_plan``.
* routes — ``RoutePlanningAgent._persist_route_plan``.
* POD — no ``fuel_grade`` field on the POD document today; future PODs
  will inherit canonicalization via the loading plan / route persisted
  here.
* reconciliation — ``mvp_reconciliation`` mapping has no fuel_grade field
  (variance on gallons only); covered at read time by the catalog-scoped
  endpoints.
* inventory.compatible_assets — ``InventoryService.create_item`` /
  ``update_item`` via ``_canonicalize_compatible_assets``.

Additional covered paths (they persist ``fuel_grade`` directly and were
in scope of the task):

* ``FuelService.create_station`` / ``record_consumption`` / ``record_refill``
* ``configure_compartments`` (truck_compartments.allowed_grades)
* ``TankForecastingAgent._persist_forecast``
* ``DeliveryPrioritizationAgent._persist_priority_list``
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.compartment_models import (
    CompartmentAssignment,
    LoadingPlan,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
    RoutePlan,
    RouteStop,
    TankForecast,
)
from errors.exceptions import AppException


TENANT_ID = "tenant-test"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_agent_with_es(agent_cls, **overrides):
    """Construct an overlay agent with a mocked ES service and SignalBus.

    We avoid importing the real dependencies of each agent by
    instantiating via ``__new__`` and setting only the attributes the
    persistence methods under test actually touch.
    """
    agent = agent_cls.__new__(agent_cls)
    agent._es = overrides.pop("es", MagicMock())
    agent._es.index_document = AsyncMock()
    agent.agent_id = overrides.pop("agent_id", agent_cls.__name__)
    for name, value in overrides.items():
        setattr(agent, name, value)
    return agent


# ---------------------------------------------------------------------------
# FuelService — station/consumption/refill
# ---------------------------------------------------------------------------


class TestFuelServiceCanonicalization:
    """``FuelService`` must canonicalize ``fuel_type`` on every write path."""

    def _make_service(self):
        from fuel.services.fuel_service import FuelService

        es = MagicMock()
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )
        return FuelService(es), es

    @pytest.mark.asyncio
    async def test_create_station_canonicalizes_ng_alias(self):
        from fuel.models import CreateFuelStation

        svc, es = self._make_service()
        # Stub _calculate_days_until_empty / _determine_status to isolate
        # canonicalization behavior.
        payload = CreateFuelStation(
            station_id="ST-AGO",
            name="AGO Station",
            fuel_type="AGO",  # Legacy NG alias.
            capacity_liters=10_000.0,
            initial_stock_liters=5_000.0,
        )

        result = await svc.create_station(payload, TENANT_ID)

        es.index_document.assert_awaited_once()
        _, doc_id, doc = es.index_document.await_args.args
        assert doc["fuel_type"] == "DIESEL_2"
        # Composite doc_id uses the canonical code so writes are idempotent
        # across alias/canonical variants of the same station.
        assert doc_id == "ST-AGO::DIESEL_2"
        assert result.fuel_type == "DIESEL_2"

    @pytest.mark.asyncio
    async def test_create_station_rejects_unknown_product(self):
        from fuel.models import CreateFuelStation

        svc, _es = self._make_service()
        payload = CreateFuelStation(
            station_id="ST-BAD",
            name="Bad",
            fuel_type="SPACE_DIESEL",
            capacity_liters=1000.0,
            initial_stock_liters=500.0,
        )
        with pytest.raises(AppException) as exc_info:
            await svc.create_station(payload, TENANT_ID)
        assert exc_info.value.status_code == 400
        assert exc_info.value.details["fuel_type"] == "SPACE_DIESEL"

    @pytest.mark.asyncio
    async def test_record_consumption_canonicalizes_ng_alias(self):
        from fuel.models import ConsumptionEvent
        from fuel.services.fuel_service import FuelService

        es = MagicMock()
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()
        # First search returns the station, second returns the window.
        es.search_documents = AsyncMock(
            side_effect=[
                {
                    "hits": {
                        "hits": [
                            {
                                "_id": "STATION-1::DIESEL_2",
                                "_source": {
                                    "station_id": "STATION-1",
                                    "fuel_type": "DIESEL_2",
                                    "capacity_liters": 10_000.0,
                                    "current_stock_liters": 5_000.0,
                                    "alert_threshold_pct": 20.0,
                                    "tenant_id": TENANT_ID,
                                },
                            }
                        ],
                        "total": {"value": 1},
                    }
                },
                {"hits": {"hits": []}},
            ]
        )
        svc = FuelService(es)
        event = ConsumptionEvent(
            station_id="STATION-1",
            fuel_type="pms",  # lowercased legacy alias.
            quantity_liters=100.0,
            asset_id="TRUCK-1",
            operator_id="OP-1",
        )

        await svc.record_consumption(event, TENANT_ID)

        # First index call is the event — we expect the canonical code.
        es.index_document.assert_awaited_once()
        args = es.index_document.await_args.args
        assert args[0] == "fuel_events"
        assert args[2]["fuel_type"] == "GASOLINE_REG"

    @pytest.mark.asyncio
    async def test_record_refill_canonicalizes_ng_alias(self):
        from fuel.models import RefillEvent
        from fuel.services.fuel_service import FuelService

        es = MagicMock()
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_id": "STATION-1::PROPANE",
                            "_source": {
                                "station_id": "STATION-1",
                                "fuel_type": "PROPANE",
                                "capacity_liters": 10_000.0,
                                "current_stock_liters": 5_000.0,
                                "alert_threshold_pct": 20.0,
                                "daily_consumption_rate": 0.0,
                                "tenant_id": TENANT_ID,
                            },
                        }
                    ],
                    "total": {"value": 1},
                }
            }
        )
        svc = FuelService(es)
        event = RefillEvent(
            station_id="STATION-1",
            fuel_type="LPG",  # Legacy NG alias for propane.
            quantity_liters=1_000.0,
            supplier="ACME",
            operator_id="OP-1",
        )

        await svc.record_refill(event, TENANT_ID)

        es.index_document.assert_awaited_once()
        args = es.index_document.await_args.args
        assert args[0] == "fuel_events"
        assert args[2]["fuel_type"] == "PROPANE"


# ---------------------------------------------------------------------------
# configure_compartments — truck_compartments.allowed_grades
# ---------------------------------------------------------------------------


class TestConfigureCompartmentsCanonicalization:
    """The PUT compartments endpoint must canonicalize allowed_grades."""

    def _make_app(self, es):
        from Agents.support import mvp_endpoints

        app = FastAPI()
        app.include_router(mvp_endpoints.router)
        mvp_endpoints._es_service = es
        mvp_endpoints._fleet_registration_service = None
        return app

    def test_canonicalizes_every_allowed_grade(self):
        es = MagicMock()
        es.index_document = AsyncMock()

        app = self._make_app(es)
        client = TestClient(app)

        resp = client.put(
            "/api/fuel/mvp/compartments/TRUCK-1",
            params={"tenant_id": TENANT_ID},
            json={
                "compartments": [
                    {
                        "compartment_id": "c1",
                        "capacity_liters": 10_000.0,
                        # Mix of NG alias, lowercased alias, and a US code.
                        "allowed_grades": ["AGO", "pms", "PROPANE"],
                        "position_index": 0,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.json()

        # Each persisted document must carry canonical codes only.
        es.index_document.assert_awaited_once()
        _, _, doc = es.index_document.await_args.args
        assert doc["allowed_grades"] == [
            "DIESEL_2",
            "GASOLINE_REG",
            "PROPANE",
        ]

    def test_unknown_allowed_grade_rejects_with_400(self):
        es = MagicMock()
        es.index_document = AsyncMock()

        app = self._make_app(es)
        client = TestClient(app)

        with pytest.raises(AppException) as exc_info:
            client.put(
                "/api/fuel/mvp/compartments/TRUCK-1",
                params={"tenant_id": TENANT_ID},
                json={
                    "compartments": [
                        {
                            "compartment_id": "c1",
                            "capacity_liters": 10_000.0,
                            "allowed_grades": ["AGO", "NONEXISTENT"],
                            "position_index": 0,
                        }
                    ]
                },
            )
        assert exc_info.value.status_code == 400
        assert "VALIDATION_ERROR" in str(exc_info.value.error_code)
        assert exc_info.value.details["fuel_grade"] == "NONEXISTENT"


# ---------------------------------------------------------------------------
# TankForecastingAgent._persist_forecast
# ---------------------------------------------------------------------------


class TestTankForecastingAgentCanonicalization:
    """Forecasts generated from NG-aliased stations must persist canonical codes."""

    @pytest.mark.asyncio
    async def test_persist_forecast_canonicalizes_fuel_grade(self):
        from Agents.overlay.tank_forecasting_agent import TankForecastingAgent

        agent = _make_agent_with_es(TankForecastingAgent)
        forecast = TankForecast(
            station_id="S1",
            fuel_grade=FuelGrade.AGO,
            hours_to_runout_p50=24.0,
            hours_to_runout_p90=12.0,
            runout_risk_24h=0.5,
            confidence=0.8,
            tenant_id=TENANT_ID,
        )

        await agent._persist_forecast(forecast)

        agent._es.index_document.assert_awaited_once()
        _, _, doc = agent._es.index_document.await_args.args
        assert doc["fuel_grade"] == "DIESEL_2"


# ---------------------------------------------------------------------------
# DeliveryPrioritizationAgent._persist_priority_list
# ---------------------------------------------------------------------------


class TestDeliveryPrioritizationAgentCanonicalization:
    """Every priority entry must persist the canonical product code."""

    @pytest.mark.asyncio
    async def test_persist_priority_list_canonicalizes_all_grades(self):
        from Agents.overlay.delivery_prioritization_agent import (
            DeliveryPrioritizationAgent,
        )

        agent = _make_agent_with_es(DeliveryPrioritizationAgent)
        priorities = [
            DeliveryPriority(
                station_id="S1",
                fuel_grade=FuelGrade.AGO,
                priority_score=0.9,
                priority_bucket=PriorityBucket.CRITICAL,
            ),
            DeliveryPriority(
                station_id="S2",
                fuel_grade=FuelGrade.LPG,
                priority_score=0.5,
                priority_bucket=PriorityBucket.MEDIUM,
            ),
        ]
        priority_list = DeliveryPriorityList(
            priorities=priorities,
            tenant_id=TENANT_ID,
        )

        await agent._persist_priority_list(priority_list)

        agent._es.index_document.assert_awaited_once()
        _, _, doc = agent._es.index_document.await_args.args
        persisted_grades = [p["fuel_grade"] for p in doc["priorities"]]
        assert persisted_grades == ["DIESEL_2", "PROPANE"]


# ---------------------------------------------------------------------------
# CompartmentLoadingAgent._persist_loading_plan
# ---------------------------------------------------------------------------


class TestCompartmentLoadingAgentCanonicalization:
    """Loading-plan assignments must persist canonical product codes."""

    @pytest.mark.asyncio
    async def test_persist_loading_plan_canonicalizes_assignments(self):
        from Agents.overlay.compartment_loading_agent import (
            CompartmentLoadingAgent,
        )

        agent = _make_agent_with_es(CompartmentLoadingAgent)
        plan = LoadingPlan(
            plan_id="P1",
            truck_id="T1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="S1",
                    fuel_grade="AGO",  # Legacy NG alias
                    quantity_liters=3_000.0,
                    compartment_capacity_liters=5_000.0,
                ),
                CompartmentAssignment(
                    compartment_id="c2",
                    station_id="S2",
                    fuel_grade="ATK",  # Legacy NG alias
                    quantity_liters=2_000.0,
                    compartment_capacity_liters=5_000.0,
                ),
            ],
            total_utilization_pct=50.0,
            tenant_id=TENANT_ID,
        )

        await agent._persist_loading_plan(plan)

        agent._es.index_document.assert_awaited_once()
        _, _, doc = agent._es.index_document.await_args.args
        persisted = [a["fuel_grade"] for a in doc["assignments"]]
        assert persisted == ["DIESEL_2", "KEROSENE"]


# ---------------------------------------------------------------------------
# RoutePlanningAgent._persist_route_plan
# ---------------------------------------------------------------------------


class TestRoutePlanningAgentCanonicalization:
    """Route-stop drop keys must be rewritten to canonical product codes."""

    @pytest.mark.asyncio
    async def test_persist_route_plan_canonicalizes_drop_keys(self):
        from Agents.overlay.route_planning_agent import RoutePlanningAgent

        agent = _make_agent_with_es(RoutePlanningAgent)
        route = RoutePlan(
            truck_id="T1",
            plan_id="P1",
            stops=[
                RouteStop(
                    station_id="S1",
                    eta="2030-01-01T00:00:00+00:00",
                    drop={"AGO": 1_000.0, "PMS": 500.0},
                    sequence=0,
                )
            ],
            distance_km=10.0,
            eta_confidence=0.8,
            tenant_id=TENANT_ID,
        )

        await agent._persist_route_plan(route)

        agent._es.index_document.assert_awaited_once()
        _, _, doc = agent._es.index_document.await_args.args
        drop = doc["stops"][0]["drop"]
        assert drop == {"DIESEL_2": 1_000.0, "GASOLINE_REG": 500.0}

    @pytest.mark.asyncio
    async def test_persist_route_plan_sums_alias_and_canonical_duplicates(self):
        """If a stop carries both an alias and its canonical code, quantities sum."""
        from Agents.overlay.route_planning_agent import RoutePlanningAgent

        agent = _make_agent_with_es(RoutePlanningAgent)
        route = RoutePlan(
            truck_id="T1",
            plan_id="P1",
            stops=[
                RouteStop(
                    station_id="S1",
                    eta="2030-01-01T00:00:00+00:00",
                    drop={"AGO": 1_000.0, "DIESEL_2": 250.0},
                    sequence=0,
                )
            ],
            distance_km=10.0,
            eta_confidence=0.8,
            tenant_id=TENANT_ID,
        )

        await agent._persist_route_plan(route)

        _, _, doc = agent._es.index_document.await_args.args
        drop = doc["stops"][0]["drop"]
        assert drop == {"DIESEL_2": 1_250.0}


# ---------------------------------------------------------------------------
# InventoryService — compatible_assets
# ---------------------------------------------------------------------------


class TestInventoryCompatibleAssetsCanonicalization:
    """``compatible_assets`` must canonicalize fuel-grade entries but preserve
    asset-type entries unchanged (asset types like ``heavy_truck`` are not
    fuel products)."""

    def _make_service(self):
        from inventory.service import InventoryService

        es = MagicMock()
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )
        return InventoryService(es), es

    @pytest.mark.asyncio
    async def test_create_item_canonicalizes_fuel_grades_in_compatible_assets(self):
        from inventory.models import CreateInventoryItem, InventoryCategory

        svc, es = self._make_service()
        payload = CreateInventoryItem(
            name="Diesel hose",
            category=InventoryCategory.FUEL_EQUIPMENT,
            quantity=10,
            unit="unit",
            min_threshold=2,
            max_capacity=100,
            location="Depot A",
            # Mix fuel-grade aliases with an asset-type value.
            compatible_assets=["AGO", "heavy_truck", "pms"],
        )

        await svc.create_item(payload, TENANT_ID)

        es.index_document.assert_awaited_once()
        _, _, doc = es.index_document.await_args.args
        assert doc["compatible_assets"] == [
            "DIESEL_2",      # canonicalized from AGO
            "heavy_truck",   # preserved (not a fuel product)
            "GASOLINE_REG",  # canonicalized from pms
        ]

    @pytest.mark.asyncio
    async def test_create_item_handles_none_compatible_assets(self):
        from inventory.models import CreateInventoryItem, InventoryCategory

        svc, es = self._make_service()
        payload = CreateInventoryItem(
            name="Generic part",
            category=InventoryCategory.GENERAL,
            quantity=1,
            unit="unit",
            min_threshold=0,
            max_capacity=10,
            location="Depot A",
            compatible_assets=None,
        )

        await svc.create_item(payload, TENANT_ID)

        _, _, doc = es.index_document.await_args.args
        assert doc["compatible_assets"] is None
