"""Staging finding F10 reproduced through ``CompartmentLoadingAgent.evaluate``.

What staging showed with two trucks (QA-TRUCK-01/02) and two orders
(``ord_A`` DIESEL_2 300 gal, ``ord_B`` HEATING_OIL 200 gal into a 500 gal tank
already at 380 gal):

* both loading plans carried both orders, and each was queued as its own
  ``apply_loading_plan`` approval;
* ``ord_B`` was planned at 832.79 L (200 gal x 1.10 buffer) into a tank with
  120 gal (454.25 L) of room;
* ``ord_B`` was labelled ``AGO`` in the approval payload and ``DIESEL_2`` in
  ``/plans``; heating-oil forecasts persisted as ``KEROSENE``;
* priorities (keyed ``customer_tank_id or order_id``) were matched to orders
  by ``customer_id``, matched nothing, and the agent silently fell back to
  5 000 L legacy station demands.

Every test here fails on the pre-fix code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.base_overlay_agent import CYCLE_METRIC_DEGRADATION_REASONS
from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.data_contracts import RiskSignal
from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
from Agents.overlay.tank_forecasting_agent import TankForecastingAgent
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)
from fuel.customer_tank_models import CustomerTank
from services.unit_conversion import GAL_TO_L

TENANT = "tenant-1"
ORD_A_LITERS = round(300 * GAL_TO_L, 2)  # 1135.62
ORD_B_LITERS = round(120 * GAL_TO_L, 2)  # 454.25 — the tank's ullage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _deps() -> Dict[str, Any]:
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.index_document = AsyncMock()
    activity_log = MagicMock()
    activity_log.log = AsyncMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    ws = MagicMock()
    ws.broadcast_activity = AsyncMock()
    confirmation = MagicMock()
    confirmation.process_mutation = AsyncMock()
    flags = MagicMock()
    flags.is_enabled = AsyncMock(return_value=True)
    return {
        "signal_bus": signal_bus,
        "es_service": es,
        "activity_log_service": activity_log,
        "ws_manager": ws,
        "confirmation_protocol": confirmation,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": flags,
    }


def _order(
    order_id: str,
    product_code: str,
    gallons: Optional[float],
    *,
    customer_id: str,
    customer_tank_id: Optional[str] = None,
    fill_to_full: bool = False,
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "customer_tank_id": customer_tank_id,
        "product_code": product_code,
        "gallons_requested": gallons,
        "fill_to_full": fill_to_full,
        "status": "placed",
        "tenant_id": TENANT,
    }


STAGING_ORDERS = [
    _order("ord_A", "DIESEL_2", 300.0, customer_id="cust-A"),
    _order(
        "ord_B", "HEATING_OIL", 200.0,
        customer_id="cust-B", customer_tank_id="tank-B",
    ),
]


def _compartment(truck_id: str, idx: int, capacity: float, grades: List[str]) -> Dict[str, Any]:
    return {
        "_source": {
            "compartment_id": f"c{idx}",
            "truck_id": truck_id,
            "capacity_liters": capacity,
            "allowed_grades": grades,
            "position_index": idx,
            "tenant_id": TENANT,
        }
    }


def _fleet(per_truck: int = 2, capacity: float = 1500.0, grades=("AGO",)) -> List[Dict[str, Any]]:
    return [
        _compartment(truck, i, capacity, list(grades))
        for truck in ("QA-TRUCK-01", "QA-TRUCK-02")
        for i in range(per_truck)
    ]


def _priority(station_id: str, score: float, *, order_id: Optional[str] = None,
              bucket: PriorityBucket = PriorityBucket.CRITICAL) -> DeliveryPriority:
    return DeliveryPriority(
        station_id=station_id,
        order_id=order_id,
        fuel_grade=FuelGrade.AGO,
        priority_score=score,
        priority_bucket=bucket,
    )


def _priority_list(*priorities: DeliveryPriority) -> DeliveryPriorityList:
    if not priorities:
        # As the prioritization agent keys them: customer_tank_id or order_id.
        priorities = (_priority("ord_A", 0.9), _priority("tank-B", 0.7))
    return DeliveryPriorityList(
        priorities=list(priorities), tenant_id=TENANT, run_id="run-staging",
    )


def _tank(capacity: float, level: float) -> MagicMock:
    tank = MagicMock(spec=CustomerTank)
    tank.capacity_gallons = capacity
    tank.current_level_gallons = level
    return tank


def _loading_agent(
    orders: List[Dict[str, Any]],
    compartments: List[Dict[str, Any]],
    tanks: Optional[Dict[str, Any]] = None,
):
    deps = _deps()

    async def _search(index, query=None, size=None):
        if index == "fuel_orders_current":
            return {"hits": {"hits": [{"_source": o} for o in orders]}}
        if index == "truck_compartments":
            return {"hits": {"hits": list(compartments)}}
        return {"hits": {"hits": []}}

    deps["es_service"].search_documents = AsyncMock(side_effect=_search)
    agent = CompartmentLoadingAgent(**deps)
    tanks = {"tank-B": _tank(500.0, 380.0)} if tanks is None else tanks
    repo = MagicMock()

    async def _get(*, tenant_id, customer_tank_id):
        return tanks.get(customer_tank_id)

    repo.get = AsyncMock(side_effect=_get)
    agent._customer_tank_repo = repo
    state_repo = MagicMock()
    state_repo.mark_loaded = AsyncMock()
    agent._compartment_state_repo = state_repo
    return agent, deps


def _persisted_plans(es) -> List[Dict[str, Any]]:
    return [
        c.args[2] for c in es.index_document.await_args_list
        if c.args and c.args[0] == "mvp_load_plans"
    ]


def _orders_in(assignments) -> set:
    return {a["order_id"] for a in assignments}


# ---------------------------------------------------------------------------
# The staging repro
# ---------------------------------------------------------------------------


class TestStagingRepro:
    @pytest.mark.asyncio
    async def test_each_order_in_exactly_one_persisted_plan_and_proposal(self):
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet())
        agent._priority_buffer.append(_priority_list())

        proposals = await agent.evaluate([])

        plans = _persisted_plans(deps["es_service"])
        plan_orders = [o for p in plans for o in _orders_in(p["assignments"])]
        assert sorted(plan_orders) == ["ord_A", "ord_B"]  # old: each twice

        params = [p.actions[0]["parameters"] for p in proposals]
        proposal_orders = [o for p in params for o in _orders_in(p["assignments"])]
        assert sorted(proposal_orders) == ["ord_A", "ord_B"]
        assert sorted(o for p in params for o in p["order_ids"]) == ["ord_A", "ord_B"]
        assert {p["run_id"] for p in params} == {"run-staging"}

    @pytest.mark.asyncio
    async def test_split_fleet_puts_one_order_on_each_truck(self):
        """One compartment per truck: the two products cannot share a truck."""
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet(per_truck=1))
        agent._priority_buffer.append(_priority_list())

        proposals = await agent.evaluate([])

        by_truck = {
            p.actions[0]["parameters"]["truck_id"]: p.actions[0]["parameters"]["order_ids"]
            for p in proposals
        }
        # ord_A ranks first (0.9 > 0.7), so it gets the first truck.
        assert by_truck == {"QA-TRUCK-01": ["ord_A"], "QA-TRUCK-02": ["ord_B"]}

    @pytest.mark.asyncio
    async def test_heating_oil_is_labelled_heating_oil_everywhere(self):
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet())
        agent._priority_buffer.append(_priority_list())

        proposals = await agent.evaluate([])

        persisted = [
            a for p in _persisted_plans(deps["es_service"])
            for a in p["assignments"] if a["order_id"] == "ord_B"
        ]
        proposed = [
            a for p in proposals for a in p.actions[0]["parameters"]["assignments"]
            if a["order_id"] == "ord_B"
        ]
        assert persisted and proposed
        assert {a["fuel_grade"] for a in persisted} == {"HEATING_OIL"}  # old DIESEL_2
        assert {a["fuel_grade"] for a in proposed} == {"HEATING_OIL"}  # old AGO
        assert {
            a["fuel_grade"] for p in proposals
            for a in p.actions[0]["parameters"]["assignments"]
        } & {"AGO", "PMS", "ATK", "LPG"} == set()

    @pytest.mark.asyncio
    async def test_order_into_a_partly_full_tank_is_capped_at_ullage(self):
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet())
        agent._priority_buffer.append(_priority_list())

        await agent.evaluate([])

        loaded = {"ord_A": 0.0, "ord_B": 0.0}
        for plan in _persisted_plans(deps["es_service"]):
            for a in plan["assignments"]:
                loaded[a["order_id"]] += a["quantity_liters"]
        assert loaded["ord_B"] == pytest.approx(ORD_B_LITERS)  # 454.25, old 832.79
        # Customer orders are not inflated by the 10 % buffer.
        assert loaded["ord_A"] == pytest.approx(ORD_A_LITERS)  # old 1249.18


# ---------------------------------------------------------------------------
# Request building: matching, caps, no invented demand
# ---------------------------------------------------------------------------


class TestDeliveryRequests:
    @pytest.mark.asyncio
    async def test_priorities_match_by_order_and_tank_id(self):
        agent, _ = _loading_agent(STAGING_ORDERS, [])
        requests = await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list()
        )
        assert [r.order_id for r in requests] == ["ord_A", "ord_B"]
        b = requests[1]
        assert b.product_code == "HEATING_OIL"
        assert b.fuel_grade == FuelGrade.AGO  # family, eligibility only
        assert b.hard_cap_liters == ORD_B_LITERS
        assert b.quantity_liters == ORD_B_LITERS

    @pytest.mark.asyncio
    async def test_explicit_priority_order_id_wins(self):
        agent, _ = _loading_agent(STAGING_ORDERS[:1], [])
        requests = await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list(_priority("some-tank", 0.9, order_id="ord_A"))
        )
        assert [r.order_id for r in requests] == ["ord_A"]

    @pytest.mark.asyncio
    async def test_requests_come_back_in_priority_order(self):
        agent, _ = _loading_agent(STAGING_ORDERS, [])
        requests = await agent._build_delivery_requests_from_orders(
            TENANT,
            _priority_list(_priority("ord_A", 0.6, bucket=PriorityBucket.MEDIUM),
                           _priority("tank-B", 0.95)),
        )
        assert [r.order_id for r in requests] == ["ord_B", "ord_A"]

    @pytest.mark.asyncio
    async def test_fill_to_full_on_a_full_tank_builds_no_request(self):
        orders = [_order(
            "ord_F", "HEATING_OIL", None, customer_id="cust-F",
            customer_tank_id="tank-F", fill_to_full=True,
        )]
        agent, deps = _loading_agent(orders, [], tanks={"tank-F": _tank(500.0, 500.0)})
        requests = await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list(_priority("tank-F", 0.9))
        )
        assert requests == []
        # Skipped as full, not failed as unresolvable.
        deps["signal_bus"].publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fill_to_full_asks_for_exactly_the_ullage(self):
        orders = [_order(
            "ord_F", "HEATING_OIL", 999.0, customer_id="cust-F",
            customer_tank_id="tank-F", fill_to_full=True,
        )]
        agent, _ = _loading_agent(orders, [], tanks={"tank-F": _tank(500.0, 380.0)})
        (req,) = await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list(_priority("tank-F", 0.9))
        )
        assert req.quantity_liters == ORD_B_LITERS

    @pytest.mark.asyncio
    async def test_unknown_tank_loads_as_requested(self):
        orders = [_order(
            "ord_U", "HEATING_OIL", 200.0, customer_id="cust-U",
            customer_tank_id="tank-U",
        )]
        agent, _ = _loading_agent(orders, [], tanks={})
        (req,) = await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list(_priority("tank-U", 0.9))
        )
        assert req.quantity_liters == round(200 * GAL_TO_L, 2)
        assert req.hard_cap_liters == req.quantity_liters

    @pytest.mark.asyncio
    async def test_tank_is_read_once_per_evaluate(self):
        orders = [
            _order("o1", "HEATING_OIL", 50.0, customer_id="c", customer_tank_id="tank-B"),
            _order("o2", "HEATING_OIL", 50.0, customer_id="c", customer_tank_id="tank-B"),
        ]
        agent, _ = _loading_agent(orders, [])
        await agent._build_delivery_requests_from_orders(
            TENANT, _priority_list(_priority("tank-B", 0.9))
        )
        assert agent._customer_tank_repo.get.await_count == 1

    @pytest.mark.asyncio
    async def test_unmatched_priorities_never_invent_legacy_demand(self):
        """Orders exist but match no priority: no 5 000 L-based station request."""
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet())
        unrelated = _priority_list(_priority("station-nobody", 0.9))

        requests = await agent._build_delivery_requests_from_orders(TENANT, unrelated)
        assert requests == []  # old: one 4 750 L request for station-nobody

        agent._priority_buffer.append(unrelated)
        assert await agent.evaluate([]) == []
        assert _persisted_plans(deps["es_service"]) == []


# ---------------------------------------------------------------------------
# Orders no truck can take
# ---------------------------------------------------------------------------


class TestUnassignedOrders:
    @pytest.mark.asyncio
    async def test_unassigned_order_is_reported_no_truck_capacity(self):
        # Gasoline-only fleet: neither diesel nor heating oil fits anywhere.
        agent, deps = _loading_agent(STAGING_ORDERS, _fleet(grades=("PMS",)))
        agent._priority_buffer.append(_priority_list())

        assert await agent.evaluate([]) == []

        reasons = {
            c.args[0].entity_id: c.args[0].context.get("reason")
            for c in deps["signal_bus"].publish.await_args_list
            if isinstance(c.args[0], RiskSignal)
        }
        assert reasons == {"ord_A": "no_truck_capacity", "ord_B": "no_truck_capacity"}
        reasons_reported = agent._cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
        assert reasons_reported[-1]["reason_code"] == "no_feasible_loading_plan"


# ---------------------------------------------------------------------------
# Forecasts and priorities carry the real product
# ---------------------------------------------------------------------------


def _heating_oil_tank() -> CustomerTank:
    return CustomerTank(
        customer_tank_id="tank-B",
        tenant_id=TENANT,
        customer_id="cust-B",
        customer_type="residential",
        fuel_type="heating_oil",
        fuel_product_code="HEATING_OIL",
        capacity_gallons=500.0,
        current_level_gallons=380.0,
        location_lat=42.31,
        location_lon=-72.63,
        zip_code="01060",
        status="active",
    )


class _TankRepo:
    def __init__(self, tanks):
        self.tanks = tanks

    async def list_for_tenant(self, tenant_id, **kwargs):
        return list(self.tanks)


class TestGradeVocabulary:
    @pytest.mark.asyncio
    async def test_heating_oil_forecast_persists_heating_oil(self):
        deps = _deps()
        agent = TankForecastingAgent(**deps)
        agent.set_customer_tank_repository(_TankRepo([_heating_oil_tank()]))

        await agent.evaluate([RiskSignal(
            source_agent="fuel_management_agent", entity_id="station-1",
            entity_type="fuel_station", severity="high", confidence=0.9,
            ttl_seconds=300, tenant_id=TENANT,
        )])

        docs = [
            c.args[2] for c in deps["es_service"].index_document.await_args_list
            if c.args[0] == "mvp_tank_forecasts" and c.args[2].get("customer_tank_id") == "tank-B"
        ]
        assert docs, "no forecast persisted for the heating-oil tank"
        assert {d["fuel_grade"] for d in docs} == {"HEATING_OIL"}  # old KEROSENE
        assert {d["product_code"] for d in docs} == {"HEATING_OIL"}

    @pytest.mark.asyncio
    async def test_heating_oil_priority_persists_heating_oil(self):
        deps = _deps()
        agent = DeliveryPrioritizationAgent(**deps)
        priority = agent._score_order(
            STAGING_ORDERS[1], forecasts={}, customer_tanks={},
            storm_active=False, now=datetime.now(timezone.utc),
        )
        assert priority.order_id == "ord_B"
        assert priority.product_code == "HEATING_OIL"

        await agent._persist_priority_list(DeliveryPriorityList(
            priorities=[priority], tenant_id=TENANT, run_id="run-staging",
        ))

        (call,) = deps["es_service"].index_document.await_args_list
        (entry,) = call.args[2]["priorities"]
        assert entry["fuel_grade"] == "HEATING_OIL"  # old DIESEL_2
        assert entry["order_id"] == "ord_B"
